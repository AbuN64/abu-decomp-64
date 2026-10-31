/* SPDX-License-Identifier: GPL-3.0-or-later */
/* n64c_scsched: the task scheduler the port links in place of the original sample scheduler.
 * Written from the public libultra scheduler contract (a thread that serves one interrupt queue, an audio list
 * and a graphics list, one RSP and one RDP owner, completion messages to the task's queue, a retrace message to
 * every client). The RSP runs inside osSpTaskStartGo here, so a task never yields and no yield path exists.
 * Per-frame game bookkeeping (frame ring, timing statistics, screen-mode checks) is not the scheduler's job;
 * the game attaches it through n64c_sc_set_retrace_hook. */
#include "n64c_internal.h"
#include <string.h>

#define NEEDS_UNITS (OS_SC_NEEDS_RDP | OS_SC_NEEDS_RSP)

static void (*retrace_hook)(void);

static int (*pre_retrace_hook)(void);
static void (*task_hook)(OSSched *, OSScTask *, N64cScTaskEvent);
static void (*prenmi_hook)(OSSched *);
static int (*task_ready_hook)(const OSScTask *);
void n64c_sc_set_retrace_hook(void (*hook)(void)) { retrace_hook = hook; }
void n64c_sc_set_pre_retrace_hook(int (*hook)(void)) { pre_retrace_hook = hook; }
void n64c_sc_set_task_hook(void (*hook)(OSSched *, OSScTask *, N64cScTaskEvent)) { task_hook = hook; }
void n64c_sc_set_prenmi_hook(void (*hook)(OSSched *)) { prenmi_hook = hook; }
void n64c_sc_set_task_ready_hook(int (*hook)(const OSScTask *)) { task_ready_hook = hook; }

#if __SIZEOF_POINTER__ == 4
/* The offsets the game's own code addresses. */
_Static_assert(offsetof(OSSched, interruptQueue) == 0x40, "interrupt queue offset");
_Static_assert(offsetof(OSSched, commandQueue) == 0x78, "command queue offset");
_Static_assert(offsetof(OSSched, clientList) == 0x2E0, "client list offset");
_Static_assert(offsetof(OSSched, curRSPTask) == 0x2F4, "RSP owner offset");
_Static_assert(offsetof(OSSched, curRDPTask) == 0x2F8, "RDP owner offset");
_Static_assert(offsetof(OSSched, doAudio) == 0x300, "audio flag offset");
_Static_assert(offsetof(OSScTask, list) == 0x10, "task list offset");
#endif

void osCreateScheduler(OSSched *sc, void *stack, OSPri priority, u8 mode, u8 numFields)
{
    (void)mode;
    memset(sc, 0, sizeof *sc);
    sc->retraceMsg = 1;
    sc->prenmiMsg = 4;
    osCreateMesgQueue(&sc->interruptQueue, sc->interruptSlots, 8);
    osCreateMesgQueue(&sc->commandQueue, sc->commandSlots, 8);
    osCreateViManager(OS_PRIORITY_VIMGR);
    osViSetEvent(&sc->interruptQueue, (OSMesg){.data32 = N64C_SC_RETRACE_MSG}, numFields);
    osSetEventMesg(OS_EVENT_SP, &sc->interruptQueue, (OSMesg){.data32 = N64C_SC_SP_DONE_MSG});
    osSetEventMesg(OS_EVENT_DP, &sc->interruptQueue, (OSMesg){.data32 = N64C_SC_DP_DONE_MSG});
    osSetEventMesg(OS_EVENT_PRENMI, &sc->interruptQueue, (OSMesg){.data32 = N64C_SC_PRENMI_MSG});
    osCreateThread(&sc->th.thread, 5, n64c_sc_main, sc, stack, priority);
    osStartThread(&sc->th.thread);
}

void osScAddClient(OSSched *sc, OSScClient *client, OSMesgQueue *msgQ)
{
    u32 mask = __osDisableInt();
    client->msgQ = msgQ;
    client->next = sc->clientList;
    sc->clientList = client;
    __osRestoreInt(mask);
}

void osScRemoveClient(OSSched *sc, OSScClient *client)
{
    u32 mask = __osDisableInt();
    OSScClient **link = &sc->clientList;
    while (*link && *link != client)
        link = &(*link)->next;
    if (*link)
        *link = client->next;
    __osRestoreInt(mask);
}

/* The queue a client posts to: submitted tasks wake the scheduler thread through it. */
OSMesgQueue *osScGetCmdQ(OSSched *sc) { return &sc->interruptQueue; }

/* Queue a task on its list in arrival order; it owes the units its flags name. */
void n64c_sc_append(OSSched *sc, OSScTask *t)
{
    OSScTask **head = t->list.type == M_AUDTASK ? &sc->audioListHead : &sc->gfxListHead;
    OSScTask **tail = t->list.type == M_AUDTASK ? &sc->audioListTail : &sc->gfxListTail;
    t->next = NULL;
    t->state = t->flags & NEEDS_UNITS;
    if (*tail)
        (*tail)->next = t;
    else
        *head = t;
    *tail = t;
    if (t->list.type == M_AUDTASK)
        sc->doAudio = 1;
}

static void drain(OSSched *sc)
{
    OSMesg m;
    while (osRecvMesg(&sc->commandQueue, &m, OS_MESG_NOBLOCK) == 0)
        if (m.ptr)
            n64c_sc_append(sc, m.ptr);
}

/* ---- Guest work ----
 * The port runs this scheduler in place of the sample scheduler the ROM links as its own code, so each step charges the
 * entries of the sample's functions it stands in for (RUNTIME.md "Virtual guest time"): a pass over the lists is
 * __scSchedule, entered once more after each pass that took a unit; a task started is __scExec; a unit reporting done
 * is __scTaskComplete. */
static void charge(unsigned entries)
{
    while (entries--)
        n64c_clock_work();
}

static OSScTask *pop(OSScTask **head, OSScTask **tail)
{
    OSScTask *t = *head;
    *head = t->next;
    if (!*head)
        *tail = NULL;
    t->next = NULL;
    return t;
}

/* A graphics buffer remains owned by the VI while displayed or awaiting a swap.
 * Leave its task at the head of the list until retrace releases it. Game frame
 * rings can retain additional buffers through the same admission boundary. */
static int graphics_ready(const OSScTask *t)
{
    if (t->framebuffer && (t->framebuffer == osViGetCurrentFramebuffer() ||
                           t->framebuffer == osViGetNextFramebuffer()))
        return 0;
    return !task_ready_hook || task_ready_hook(t);
}

/* Start the next task when the RSP is free: audio first, then graphics once its
 * RDP and framebuffer are free. Blocked graphics must not hold up audio. */
static void schedule(OSSched *sc)
{
    OSScTask *t;
    charge(1);
    if (sc->curRSPTask)
        return;
    if (sc->audioListHead) {
        t = pop(&sc->audioListHead, &sc->audioListTail);
        if (!sc->audioListHead)
            sc->doAudio = 0;
    } else if (sc->gfxListHead && !((sc->gfxListHead->state & OS_SC_NEEDS_RDP) && sc->curRDPTask) &&
               graphics_ready(sc->gfxListHead)) {
        t = pop(&sc->gfxListHead, &sc->gfxListTail);
    } else
        return;
    charge(2);
    sc->curRSPTask = t;
    if (t->state & OS_SC_NEEDS_RDP)
        sc->curRDPTask = t;
    if (task_hook)
        task_hook(sc, t, N64C_SC_TASK_START);
    osSpTaskLoad(&t->list);
    osSpTaskStartGo(&t->list);
}

/* A task that owes nothing is finished: a last graphics task shows its frame, a finished audio task or last
 * graphics task tells its owner. Earlier tasks of a multi-task frame finish silently. */
static void finish(OSSched *sc, OSScTask *t)
{
    (void)sc;
    if (t->state & NEEDS_UNITS)
        return;
    if (t->list.type == M_GFXTASK) {
        if (!(t->flags & OS_SC_LAST_TASK))
            return;
        if (t->flags & OS_SC_SWAPBUFFER)
            osViSwapBuffer(t->framebuffer);
    }
    if (t->msgQ)
        osSendMesg(t->msgQ, t->msg, OS_MESG_BLOCK);
}

static void unit_done(OSSched *sc, OSScTask **owner, u32 unit)
{
    OSScTask *t = *owner;
    if (!t)
        return;
    *owner = NULL;
    t->state &= ~unit;
    charge(1);
    if (task_hook)
        task_hook(sc, t, unit == OS_SC_NEEDS_RSP ? N64C_SC_RSP_DONE : N64C_SC_RDP_DONE);
    finish(sc, t);
    schedule(sc);
}

void n64c_sc_retrace(OSSched *sc)
{
    OSScClient *c;
    if (pre_retrace_hook && pre_retrace_hook())
        return;
    sc->frameCount++;
    drain(sc);
    schedule(sc);
    for (c = sc->clientList; c; c = c->next)
        osSendMesg(c->msgQ, (OSMesg){.ptr = sc}, OS_MESG_NOBLOCK);
    if (retrace_hook)
        retrace_hook();
}

/* The RSP finished its task / the RDP finished its task. */
void n64c_sc_sp_done(OSSched *sc) { unit_done(sc, &sc->curRSPTask, OS_SC_NEEDS_RSP); }
void n64c_sc_dp_done(OSSched *sc) { unit_done(sc, &sc->curRDPTask, OS_SC_NEEDS_RDP); }
/* A task was submitted: take everything waiting on the command queue and start what can start. */
void n64c_sc_command(OSSched *sc)
{
    drain(sc);
    schedule(sc);
}
/* Ask the RSP task to yield. The native RSP runs a task to completion inside osSpTaskStartGo, so a task is
 * never in flight when the scheduler thread can ask: there is nothing to yield. */
void n64c_sc_yield(OSSched *sc) { (void)sc; }

void n64c_sc_main(void *arg)
{
    OSSched *sc = arg;
    OSMesg m;
    for (;;) {
        osRecvMesg(&sc->interruptQueue, &m, OS_MESG_BLOCK);
        switch (m.data32) {
        case N64C_SC_RETRACE_MSG:
            n64c_sc_retrace(sc);
            break;
        case N64C_SC_SP_DONE_MSG:
            n64c_sc_sp_done(sc);
            break;
        case N64C_SC_DP_DONE_MSG:
            n64c_sc_dp_done(sc);
            break;
        case N64C_SC_CMD_MSG:
            n64c_sc_command(sc);
            break;
        case N64C_SC_PRENMI_MSG:
            if (prenmi_hook)
                prenmi_hook(sc); /* the native machine raises none; a test or host may post it */
            break;
        default:   /* a task posted straight to the queue osScGetCmdQ returned */
            if (m.ptr) {
                n64c_sc_append(sc, m.ptr);
                schedule(sc);
            }
        }
    }
}
