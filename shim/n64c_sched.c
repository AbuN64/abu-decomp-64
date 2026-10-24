/* SPDX-License-Identifier: GPL-3.0-or-later */
/* n64c_sched: message queues, guest threads and the virtual clock.
 * Adapter for the sm64-port "produce_one_frame" pattern: every guest thread is a host thread, but exactly one
 * runs at a time. The baton goes to the highest-priority runnable thread (FIFO within a priority) whenever the
 * running one blocks, yields, or wakes a higher-priority thread. The host driver owns the baton when no guest
 * thread is runnable, which is how a frame ends. All state is behind one mutex; there is no timeslicing. */
#include "n64c_internal.h"
#include "n64c_memory.h"
#include "n64c_system.h"
#include "os/n64c_host.h"
#include "n64c_hook.h"
#include <pthread.h>
#include <errno.h>
#include <setjmp.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

typedef struct HostThread {
    OSThread *os;
    pthread_t th;
    pthread_cond_t cv;
    jmp_buf exit;              /* return to the host entry without unwinding guest frames */
    void (*entry)(void *);
    void *arg;
    int created, finished;
    int faulted;                /* stopped by a CPU fault until osStartThread resumes it */
    int idle;                   /* the running thread set its own priority to 0: parked off the run queue */
    int slot;                   /* index in all_threads and of its fixed stack */
    struct HostThread *runnext; /* run queue link */
} HostThread;

static pthread_mutex_t big = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t host_cv = PTHREAD_COND_INITIALIZER;
static HostThread *runq;           /* sorted: priority descending, FIFO within */
static HostThread *baton;          /* NULL: host owns it */
static HostThread *all_threads[64];
static int n_threads;
static int quitting;
static int halted;                 /* a guest thread stopped the run: nothing is dispatched any more */
static pthread_key_t self_key;
static pthread_once_t key_once = PTHREAD_ONCE_INIT;
static struct { OSMesgQueue *mq; OSMesg msg; } events[OS_NUM_EVENTS];
/* One active CPU/RCP mask for the cooperative native machine. Masked hardware
 * events latch once; they are not a backlog of host messages. */
static OSIntMask interrupt_mask = OS_IM_ALL;
static u32 pending_events;
static const OSIntMask event_masks[OS_NUM_EVENTS] = {
    [OS_EVENT_SW1] = 0x101u, [OS_EVENT_SW2] = 0x201u,
    [OS_EVENT_CART] = 0x801u, [OS_EVENT_COUNTER] = 0x8001u,
    [OS_EVENT_SP] = OS_IM_SP, [OS_EVENT_SI] = OS_IM_SI,
    [OS_EVENT_AI] = OS_IM_AI, [OS_EVENT_VI] = OS_IM_VI,
    [OS_EVENT_PI] = OS_IM_PI, [OS_EVENT_DP] = OS_IM_DP,
    [OS_EVENT_SP_BREAK] = OS_IM_SP, [OS_EVENT_PRENMI] = 0x1001u
};
static int interrupt_enabled(u32 event)
{
    return (interrupt_mask & event_masks[event]) == event_masks[event];
}
static OSTimer *timers;            /* sorted by value (absolute due time) */
static OSTime clock_now;
static OSTime compare_deadline;
static OSTime ai_deadline; /* virtual time the audio FIFO's oldest buffer finishes playing */
static int ai_armed;
static void (*ai_due)(void);
static int compare_armed;
/* A guest thread that keeps reading the clock without giving up the CPU is waiting on it (a delay or a fault
 * handler's watchdog loop). On the console Count runs on regardless; here it moves only when the host advances
 * it, and the spinning thread keeps the host from running by holding the baton, so the loop would never end.
 * Past SPIN_POLLS reads in one hold of the baton, every read costs the time one loop pass takes (SPIN_TICKS of
 * Count, about a microsecond at 46.875 MHz) and timers that come due fire as they would on the console. */
enum { SPIN_POLLS = 1024, SPIN_TICKS = 48 };
static unsigned clock_polls;       /* clock reads by the baton holder since the baton last moved */
static void (*trace_sink)(const char *, void *);
static void *trace_user;

static void key_init(void) { pthread_key_create(&self_key, NULL); }
static HostThread *self(void) { pthread_once(&key_once, key_init); return pthread_getspecific(self_key); }
/* The guest-visible context holds slot + 1 (0: never created), never a host pointer, so guest RAM is the same
 * every run. */
static HostThread *hostof(OSThread *t) { u32 slot; memcpy(&slot, t->context, sizeof slot); return slot ? all_threads[slot - 1] : NULL; }
/* Each slot runs on its own stack at a fixed host address where the OS allows (os/n64c_host.h), so host addresses
 * the game stores in guest RAM (pointers to its locals) do not depend on host allocation order or ASLR. */
#define STACK_BYTES (1u << 20)
#define STACK_SLOT (STACK_BYTES + 0x10000u)
static void *slot_stack(int slot) { return n64c_host_guest_stack(slot, STACK_BYTES, STACK_SLOT, 0x10000u); }
static void trace(const char *what, OSThread *t)
{
    if (!trace_sink)
        return;
    char line[96];
    snprintf(line, sizeof line, "%s %d", what, t ? (int)t->id : -1);
    trace_sink(line, trace_user);
}

static void runq_insert(HostThread *h)
{
    HostThread **p = &runq;
    while (*p && (*p)->os->priority >= h->os->priority)
        p = &(*p)->runnext;
    h->runnext = *p;
    *p = h;
    h->os->state = OS_STATE_RUNNABLE;
}
static void runq_remove(HostThread *h)
{
    for (HostThread **p = &runq; *p; p = &(*p)->runnext)
        if (*p == h) {
            *p = h->runnext;
            return;
        }
}
/* Give the baton to the best runnable thread (or the host). Lock held. */
static void dispatch(void)
{
    HostThread *n = runq;
    clock_polls = 0;
    if (n) {
        runq = n->runnext;
        n->os->state = OS_STATE_RUNNING;
        baton = n;
        pthread_cond_signal(&n->cv);
    } else {
        baton = NULL;
        pthread_cond_signal(&host_cv);
    }
}
/* Wait until this host thread owns the baton. Lock held. */
static void wait_baton(HostThread *h)
{
    while (baton != h && !quitting)
        pthread_cond_wait(&h->cv, &big);
    if (quitting) {
        pthread_mutex_unlock(&big);
        longjmp(h->exit, 1);
    }
}
static void *thread_main(void *p)
{
    HostThread *h = p;
    pthread_once(&key_once, key_init);
    pthread_setspecific(self_key, h);
    /* Guest stacks can contain instrumented return trampolines or fault-recovery frames.
     * pthread_exit forces a host unwind through them. End the guest at this boundary instead,
     * then return normally so pthread cleanup runs on the host entry frame. */
    if (setjmp(h->exit))
        return NULL;
    pthread_mutex_lock(&big);
    wait_baton(h);
    pthread_mutex_unlock(&big);
    n64c_fpu_thread_start();
    h->entry(h->arg);
    pthread_mutex_lock(&big);
    h->finished = 1;
    h->os->state = OS_STATE_STOPPED;
    trace("exit", h->os);
    dispatch();
    pthread_mutex_unlock(&big);
    return NULL;
}
/* Called by the running guest thread (lock held) after it queued itself somewhere: switch away and wait. */
static void switch_away(HostThread *h)
{
    dispatch();
    wait_baton(h);
}
/* Make t runnable; if it outranks the caller, the caller yields the baton to it. Lock held. */
static void wake(HostThread *t)
{
    HostThread *cur = self();
    runq_insert(t);
    if (cur && baton == cur && !cur->faulted && t->os->priority > cur->os->priority) {
        runq_insert(cur);
        switch_away(cur);
    }
}
static void thread_enqueue(OSThread **q, OSThread *t)
{
    while (*q && (*q)->priority >= t->priority)
        q = &(*q)->next;
    t->next = *q;
    *q = t;
    t->queue = q;
}
static OSThread *thread_pop(OSThread **q)
{
    OSThread *t = *q;
    if (t) {
        *q = t->next;
        t->next = NULL;
        t->queue = NULL;
    }
    return t;
}
void osCreateMesgQueue(OSMesgQueue *mq, OSMesg *buf, s32 count)
{
    memset(mq, 0, sizeof *mq);
    mq->msgCount = count;
    mq->msg = buf;
}
static s32 send(OSMesgQueue *mq, OSMesg msg, s32 flag, int jam)
{
    HostThread *cur = self();
    pthread_mutex_lock(&big);
    while (mq->validCount >= mq->msgCount) {
        if (flag == OS_MESG_NOBLOCK || !cur) {
            pthread_mutex_unlock(&big);
            return -1;
        }
        cur->os->state = OS_STATE_WAITING;
        thread_enqueue(&mq->fullqueue, cur->os);
        trace("block-send", cur->os);
        switch_away(cur);
    }
    if (jam) {
        mq->first = (mq->first + mq->msgCount - 1) % mq->msgCount;
        mq->msg[mq->first] = msg;
    } else
        mq->msg[(mq->first + mq->validCount) % mq->msgCount] = msg;
    mq->validCount++;
    OSThread *w = thread_pop(&mq->mtqueue);
    if (w)
        wake(hostof(w));
    pthread_mutex_unlock(&big);
    return 0;
}
s32 osSendMesg(OSMesgQueue *mq, OSMesg msg, s32 flag) { return send(mq, msg, flag, 0); }
s32 osJamMesg(OSMesgQueue *mq, OSMesg msg, s32 flag) { return send(mq, msg, flag, 1); }
s32 osRecvMesg(OSMesgQueue *mq, OSMesg *msg, s32 flag)
{
    HostThread *cur = self();
    pthread_mutex_lock(&big);
    while (mq->validCount == 0) {
        if (flag == OS_MESG_NOBLOCK || !cur) {
            pthread_mutex_unlock(&big);
            return -1;
        }
        cur->os->state = OS_STATE_WAITING;
        thread_enqueue(&mq->mtqueue, cur->os);
        trace("block-recv", cur->os);
        switch_away(cur);
    }
    if (msg)
        *msg = mq->msg[mq->first];
    mq->first = (mq->first + 1) % mq->msgCount;
    mq->validCount--;
    OSThread *w = thread_pop(&mq->fullqueue);
    if (w)
        wake(hostof(w));
    pthread_mutex_unlock(&big);
    return 0;
}
void osSetEventMesg(u32 event, OSMesgQueue *mq, OSMesg msg)
{
    if (event < OS_NUM_EVENTS) {
        pthread_mutex_lock(&big);
        events[event].mq = mq;
        events[event].msg = msg;
        pthread_mutex_unlock(&big);
    }
}
static void deliver_pending(void)
{
    for (;;) {
        pthread_mutex_lock(&big);
        u32 event;
        for (event = 0; event < OS_NUM_EVENTS; event++)
            if ((pending_events & (1u << event)) && interrupt_enabled(event)) break;
        if (event == OS_NUM_EVENTS) {
            pthread_mutex_unlock(&big);
            return;
        }
        pending_events &= ~(1u << event);
        OSMesgQueue *mq = events[event].mq;
        OSMesg msg = events[event].msg;
        OSTime now = clock_now;
        pthread_mutex_unlock(&big);
        if (event == OS_EVENT_VI) n64c_vi_interrupt();
        if (event == OS_EVENT_COUNTER) n64c_clock_advance_to(now);
        if (mq) osSendMesg(mq, msg, OS_MESG_NOBLOCK);
    }
}
void n64c_event_post(u32 event)
{
    if (event >= OS_NUM_EVENTS) return;
    pthread_mutex_lock(&big);
    pending_events |= 1u << event;
    pthread_mutex_unlock(&big);
    deliver_pending();
}
OSIntMask osSetIntMask(OSIntMask mask)
{
    pthread_mutex_lock(&big);
    OSIntMask previous = interrupt_mask;
    interrupt_mask = mask & OS_IM_ALL;
    pthread_mutex_unlock(&big);
    deliver_pending();
    return previous;
}
static OSThread *faulted_thread;
enum { CONTEXT_CAUSE = 0x100, CONTEXT_BADVADDR = 0x104, FLAG_FAULT = 2 }; /* __OSThreadContext offsets, OS_FLAG_FAULT */
OSThread *osGetCurrFaultedThread(void)
{
    pthread_mutex_lock(&big);
    OSThread *t = faulted_thread;
    pthread_mutex_unlock(&big);
    return t;
}
int n64c_sched_fault(u32 cause, u32 badvaddr)
{
    HostThread *h = self();
    if (!h)
        return 0;
    pthread_mutex_lock(&big);
    OSMesgQueue *mq = events[OS_EVENT_FAULT].mq;
    OSMesg msg = events[OS_EVENT_FAULT].msg;
    if (!mq || baton != h) {
        pthread_mutex_unlock(&big);
        fprintf(stderr, "n64c: TLB miss at 0x%08x not serviced: %s\n", (unsigned)badvaddr,
                mq ? "the faulting thread does not hold the CPU" : "no OS_EVENT_FAULT queue is set");
        return 0;
    }
    OSThread *t = h->os;
    memcpy(t->context + CONTEXT_CAUSE, &cause, sizeof cause);
    memcpy(t->context + CONTEXT_BADVADDR, &badvaddr, sizeof badvaddr);
    t->state = OS_STATE_STOPPED;
    t->flags |= FLAG_FAULT;
    h->faulted = 1;
    faulted_thread = t;
    trace("fault", t);
    pthread_mutex_unlock(&big);
    /* The faulted thread keeps the baton while it posts (wake() never requeues it), then gives it away. */
    osSendMesg(mq, msg, OS_MESG_NOBLOCK);
    pthread_mutex_lock(&big);
    if (h->faulted)
        switch_away(h);
    pthread_mutex_unlock(&big);
    return 1;
}
OSThread *n64c_running_thread(void)
{
    HostThread *h = self();
    return h ? h->os : NULL;
}
void osCreateThread(OSThread *t, OSId id, void (*entry)(void *), void *arg, void *sp, OSPri pri)
{
    (void)sp; /* guest stacks are host stacks */
    HostThread *h = calloc(1, sizeof *h);
    memset(t, 0, sizeof *t);
    t->id = id;
    t->priority = pri;
    t->state = OS_STATE_STOPPED;
    h->os = t;
    h->entry = entry;
    h->arg = arg;
    pthread_cond_init(&h->cv, NULL);
    pthread_mutex_lock(&big);
    if (n_threads == 64) {
        fprintf(stderr, "n64c-sched: more than 64 guest threads created\n");
        abort();
    }
    h->slot = n_threads;
    all_threads[n_threads++] = h;
    pthread_mutex_unlock(&big);
    u32 slot = (u32)h->slot + 1;
    memcpy(t->context, &slot, sizeof slot);
}
/* The boot census (boot/trace.c) notes the first thread start; absent when it is not linked. */
void osStartThread(OSThread *t)
{
    if (n64c_trace_event) n64c_trace_event("first thread start");
    HostThread *h = hostof(t);
    pthread_mutex_lock(&big);
    if (t->state == OS_STATE_STOPPED && !h->finished) {
        if (!h->created) {
            pthread_attr_t a;
            pthread_attr_init(&a);
            void *stack = slot_stack(h->slot);
            if (stack)
                pthread_attr_setstack(&a, stack, STACK_BYTES);
            else
                pthread_attr_setstacksize(&a, STACK_BYTES);
            pthread_create(&h->th, &a, thread_main, h);
            pthread_attr_destroy(&a);
            h->created = 1;
        }
        trace("start", t);
        h->faulted = 0;
        wake(h);
    }
    pthread_mutex_unlock(&big);
}
void osDestroyThread(OSThread *t)
{
    HostThread *h = t ? hostof(t) : self();
    if (!t)
        t = h->os;
    pthread_mutex_lock(&big);
    if (t->queue) {
        for (OSThread **p = t->queue; *p; p = &(*p)->next)
            if (*p == t) {
                *p = t->next;
                break;
            }
        t->queue = NULL;
    }
    runq_remove(h);
    t->state = OS_STATE_STOPPED;
    h->finished = 1;
    if (h == self()) {
        trace("destroy-self", t);
        dispatch();
        pthread_mutex_unlock(&big);
        longjmp(h->exit, 1);
    }
    pthread_mutex_unlock(&big);
}
void osSetThreadPri(OSThread *t, OSPri pri)
{
    HostThread *cur = self();
    pthread_mutex_lock(&big);
    OSThread *tt = t ? t : (cur ? cur->os : NULL);
    if (tt) {
        tt->priority = pri;
        HostThread *th = hostof(tt);
        /* OS_PRIORITY_IDLE: a thread that lowers itself to 0 is the console's idle loop. On hardware the retrace
         * interrupt preempts it; here nothing preempts a host thread, so it would hold the baton against the host
         * driver for good and no retrace would be posted. It is parked off the run queue instead (it never has
         * work); raising its priority again makes it runnable. */
        if (th->idle && pri > 0) {
            th->idle = 0;
            runq_insert(th);
            if (cur && baton == cur && th->os->priority > cur->os->priority) {
                runq_insert(cur);
                switch_away(cur);
            }
            pthread_mutex_unlock(&big);
            return;
        }
        if (pri == 0 && cur && th == cur && baton == cur) {
            cur->idle = 1;
            trace("idle", tt);
            switch_away(cur);
            pthread_mutex_unlock(&big);
            return;
        }
        if (th->idle) {
            pthread_mutex_unlock(&big);
            return;
        }
        if (tt->state == OS_STATE_RUNNABLE) {
            runq_remove(hostof(tt));
            runq_insert(hostof(tt));
        } else if (tt->state == OS_STATE_WAITING && tt->queue) {
            OSThread **q = tt->queue;
            for (OSThread **p = q; *p; p = &(*p)->next)
                if (*p == tt) {
                    *p = tt->next;
                    break;
                }
            thread_enqueue(q, tt);
        }
        /* The running thread yields when someone runnable now outranks it. */
        if (cur && baton == cur && runq && runq->os->priority > cur->os->priority) {
            runq_insert(cur);
            switch_away(cur);
        }
    }
    pthread_mutex_unlock(&big);
}
OSPri osGetThreadPri(OSThread *t) { HostThread *c = self(); return t ? t->priority : (c ? c->os->priority : 0); }
OSId osGetThreadId(OSThread *t) { HostThread *c = self(); return t ? t->id : (c ? c->os->id : 0); }
void osYieldThread(void)
{
    HostThread *cur = self();
    if (!cur)
        return;
    pthread_mutex_lock(&big);
    runq_insert(cur);
    trace("yield", cur->os);
    switch_away(cur);
    pthread_mutex_unlock(&big);
}
/* ---- virtual clock and timers ---- */
void osSetTimer(OSTimer *t, OSTime countdown, OSTime interval, OSMesgQueue *mq, OSMesg msg)
{
    pthread_mutex_lock(&big);
    t->interval = interval;
    t->value = clock_now + countdown;
    t->mq = mq;
    t->msg = msg;
    OSTimer **p = &timers;
    while (*p && (*p)->value <= t->value)
        p = &(*p)->next;
    t->next = *p;
    *p = t;
    pthread_mutex_unlock(&big);
}
s32 osStopTimer(OSTimer *t)
{
    s32 found = -1;
    pthread_mutex_lock(&big);
    for (OSTimer **p = &timers; *p; p = &(*p)->next)
        if (*p == t) {
            *p = t->next;
            found = 0;
            break;
        }
    pthread_mutex_unlock(&big);
    return found;
}
static OSTime clock_read(void)
{
    HostThread *h = self();
    if (h) {
        pthread_mutex_lock(&big);
        int spinning = baton == h && ++clock_polls > SPIN_POLLS;
        OSTime t = clock_now + SPIN_TICKS;
        pthread_mutex_unlock(&big);
        if (spinning)
            n64c_clock_advance_to(t);
    }
    pthread_mutex_lock(&big);
    OSTime now = clock_now;
    pthread_mutex_unlock(&big);
    return now;
}
OSTime osGetTime(void) { return clock_read(); }
u32 osGetCount(void) { return (u32)clock_read(); }
OSTime n64c_clock_now(void) { return clock_now; }
/* R4000 User's Manual, Compare register(11): Count equality raises IP7;
 * writing Compare clears that interrupt. Use the existing virtual Count and
 * interrupt delivery; no wall clock or original timer-manager code. */
void __osSetCompare(u32 value)
{
    pthread_mutex_lock(&big);
    compare_deadline = clock_now + (u32)(value - (u32)clock_now);
    compare_armed = 1;
    pending_events &= ~(1u << OS_EVENT_COUNTER);
    /* Native software timers are independent of the guest Compare register. */
    if (timers && timers->value <= clock_now) pending_events |= 1u << OS_EVENT_COUNTER;
    pthread_mutex_unlock(&big);
}
void n64c_clock_arm_ai(OSTime t, void (*due)(void))
{
    pthread_mutex_lock(&big);
    ai_deadline = t;
    ai_due = due;
    ai_armed = 1;
    pthread_mutex_unlock(&big);
}
void n64c_clock_advance_to(OSTime t)
{
    for (;;) {
        pthread_mutex_lock(&big);
        OSTimer *due = timers && timers->value <= t ? timers : NULL;
        /* The audio FIFO's oldest buffer finished: the AI interrupt that tells the game to refill it. */
        if (ai_armed && ai_deadline <= t && (!due || ai_deadline <= due->value) &&
            (!compare_armed || ai_deadline <= compare_deadline)) {
            ai_armed = 0;
            if (ai_deadline > clock_now) clock_now = ai_deadline;
            pthread_mutex_unlock(&big);
            ai_due();
            if (!self()) n64c_sched_run_idle();
            continue;
        }
        if (compare_armed && compare_deadline <= t &&
            (!due || !interrupt_enabled(OS_EVENT_COUNTER) || compare_deadline <= due->value)) {
            compare_armed = 0;
            if (compare_deadline > clock_now) clock_now = compare_deadline;
            pthread_mutex_unlock(&big);
            n64c_event_post(OS_EVENT_COUNTER);
            if (!self()) n64c_sched_run_idle();
            continue;
        }
        if (due && !interrupt_enabled(OS_EVENT_COUNTER)) {
            pending_events |= 1u << OS_EVENT_COUNTER;
            if (t > clock_now) clock_now = t;
            pthread_mutex_unlock(&big);
            return;
        }
        if (due) {
            timers = due->next;
            if (due->value > clock_now) clock_now = due->value;
            OSMesgQueue *mq = due->mq;
            OSMesg msg = due->msg;
            if (due->interval) {
                due->value += due->interval;
                OSTimer **p = &timers;
                while (*p && (*p)->value <= due->value)
                    p = &(*p)->next;
                due->next = *p;
                *p = due;
            }
            pthread_mutex_unlock(&big);
            osSendMesg(mq, msg, OS_MESG_NOBLOCK);
            if (!self()) n64c_sched_run_idle();
            continue;
        }
        if (t > clock_now)
            clock_now = t;
        pthread_mutex_unlock(&big);
        return;
    }
}
/* ---- host side ---- */
void n64c_sched_run_idle(void)
{
    pthread_mutex_lock(&big);
    while (runq && !halted) {
        dispatch();
        while (baton != NULL)
            pthread_cond_wait(&host_cv, &big);
    }
    pthread_mutex_unlock(&big);
}
/* The running guest thread ends the run where it stands: it is parked for good (it never returns, so no
 * value is invented for its caller), the host gets the baton back and no thread is dispatched again until
 * n64c_sched_reset. Only a guest thread may halt. */
void n64c_sched_halt(void)
{
    HostThread *h = self();
    if (!h)
        abort();
    pthread_mutex_lock(&big);
    halted = 1;
    h->os->state = OS_STATE_STOPPED;
    trace("halt", h->os);
    baton = NULL;
    pthread_cond_signal(&host_cv);
    wait_baton(h); /* quitting returns through the host entry boundary */
    abort();
}
int n64c_sched_halted(void)
{
    pthread_mutex_lock(&big);
    int r = halted;
    pthread_mutex_unlock(&big);
    return r;
}
int n64c_sched_on_guest(void) { return self() != NULL; }
int n64c_sched_alive(void)
{
    int n = 0;
    pthread_mutex_lock(&big);
    for (int i = 0; i < n_threads; i++)
        n += all_threads[i]->created && !all_threads[i]->finished;
    pthread_mutex_unlock(&big);
    return n;
}
uint32_t n64c_sched_waiting(void)
{
    uint32_t n = 0;
    pthread_mutex_lock(&big);
    for (int i = 0; i < n_threads; i++)
        n += all_threads[i]->created && !all_threads[i]->finished && all_threads[i]->os->state == OS_STATE_WAITING;
    pthread_mutex_unlock(&big);
    return n;
}
void n64c_sched_trace(void (*sink)(const char *, void *), void *user)
{
    trace_sink = sink;
    trace_user = user;
}
void n64c_sched_reset(void)
{
    pthread_mutex_lock(&big);
    quitting = 1;
    for (int i = 0; i < n_threads; i++)
        pthread_cond_broadcast(&all_threads[i]->cv);
    pthread_mutex_unlock(&big);
    for (int i = 0; i < n_threads; i++) {
        if (all_threads[i]->created)
            pthread_join(all_threads[i]->th, NULL);
        pthread_cond_destroy(&all_threads[i]->cv);
        free(all_threads[i]);
        all_threads[i] = NULL;
    }
    n_threads = 0;
    quitting = 0;
    halted = 0;
    runq = NULL;
    baton = NULL;
    timers = NULL;
    clock_now = 0;
    compare_deadline = 0;
    compare_armed = 0;
    ai_armed = 0;
    interrupt_mask = OS_IM_ALL;
    pending_events = 0;
    memset(events, 0, sizeof events);
    trace_sink = NULL;
}
u32 __osDisableInt(void)
{
    pthread_mutex_lock(&big);
    u32 previous = interrupt_mask & 1u;
    interrupt_mask &= ~1u;
    pthread_mutex_unlock(&big);
    return previous;
}
void __osRestoreInt(u32 mask)
{
    pthread_mutex_lock(&big);
    interrupt_mask = (interrupt_mask & ~1u) | (mask & 1u);
    pthread_mutex_unlock(&big);
    deliver_pending();
}
/* Cache operations need no host emulation. */
void osWritebackDCache(void *p, size_t n) { (void)p; (void)n; }
void osWritebackDCacheAll(void) {}
void osInvalDCache(void *p, size_t n) { (void)p; (void)n; }
void osInvalICache(void *p, size_t n) { (void)p; (void)n; }
void osMapTLBRdb(void) {}
u32 osVirtualToPhysical(void *p) { return n64c_memory_physical(p); }
