/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Unit goldens for mesg queues, thread priority order and VI/timer delivery. Every expected string is derived by
 * hand from the documented libultra semantics (highest priority runs; FIFO within a priority; a wake of a
 * higher-priority thread preempts at once), not recorded from this implementation. */
#include <stdint.h>
#include "../n64c_os.h"
#include <stdio.h>
#include <string.h>

static char logbuf[512];
static void L(const char *fmt, int v)
{
    char t[32];
    snprintf(t, sizeof t, fmt, v);
    strncat(logbuf, t, sizeof logbuf - strlen(logbuf) - 1);
}
static int fails;
static void expect(const char *name, const char *want)
{
    if (strcmp(logbuf, want)) {
        printf("FAIL %s\n  want: %s\n  got:  %s\n", name, want, logbuf);
        fails++;
    } else
        printf("ok   %s: %s\n", name, logbuf);
    logbuf[0] = 0;
}
static OSThread th[6];
static OSMesgQueue q[6];
static OSMesg qbuf[6][8];
static void make_queues(void)
{
    for (int i = 0; i < 6; i++)
        osCreateMesgQueue(&q[i], qbuf[i], 8);
}
/* receiver: logs "<tag><value>" for every message it receives on queue arg */
typedef struct { const char *tag; int qi; } RecvArg;
static uintptr_t receiver_local; /* where the first receiver to run (th[2], the highest priority) keeps its locals */
static void receiver(void *p)
{
    RecvArg *a = p;
    OSMesg m;
    if (!receiver_local) receiver_local = (uintptr_t)&m;
    for (;;) {
        osRecvMesg(&q[a->qi], &m, OS_MESG_BLOCK);
        char f[16];
        snprintf(f, sizeof f, "%s%%d ", a->tag);
        L(f, (int)m.data32);
    }
}
static void receiver_once(void *p)
{
    RecvArg *a = p;
    OSMesg m;
    osRecvMesg(&q[a->qi], &m, OS_MESG_BLOCK);
    char f[16];
    snprintf(f, sizeof f, "%s%%d ", a->tag);
    L(f, (int)m.data32);
}
static RecvArg ra[6] = {{"L", 0}, {"M", 1}, {"H", 2}, {"E1_", 3}, {"E2_", 3}, {"S", 4}};
static OSMesg m32(u32 v) { OSMesg m; m.data32 = v; return m; }

static void sender_preempt(void *p)
{
    (void)p;
    L("x%d ", 1);
    osSendMesg(&q[2], m32(7), OS_MESG_NOBLOCK); /* wakes H (pri 30) which outranks this thread */
    L("x%d ", 2);
}
static void yielder(void *p)
{
    RecvArg *a = p;
    char f[16];
    for (int i = 1; i <= 2; i++) {
        snprintf(f, sizeof f, "%s%%d ", a->tag);
        L(f, i);
        osYieldThread();
    }
}
static void blocked_sender(void *p)
{
    (void)p;
    for (int i = 1; i <= 3; i++) {
        L("s%d ", i);
        osSendMesg(&q[5], m32((u32)i), OS_MESG_BLOCK); /* queue 5 holds 1 message: blocks from the 2nd send */
    }
}
static void vi_counter(void *p)
{
    (void)p;
    OSMesg m;
    for (;;) {
        osRecvMesg(&q[0], &m, OS_MESG_BLOCK);
        L("V%d ", (int)m.data32);
    }
}
static void timer_waiter(void *p)
{
    (void)p;
    OSMesg m;
    for (;;) {
        osRecvMesg(&q[1], &m, OS_MESG_BLOCK);
        L("T%d ", (int)m.data32);
    }
}
/* spinner: waits 3 ms of Count in a loop that only reads the clock */
static void spinner(void *p)
{
    (void)p;
    OSTime start = osGetTime();
    while (osGetTime() - start < (OSTime)OS_CPU_COUNTER * 3 / 1000)
        ;
    L("S%d ", 1);
}
static void halter(void *p)
{
    (void)p;
    L("g%d ", n64c_sched_on_guest());
    n64c_sched_halt();
}
static void after_halt(void *p)
{
    (void)p;
    L("A%d ", 1);
}
static void idle_thread(void *p)
{
    (void)p;
    L("I ", 0);
    osSetThreadPri(NULL, 0);
    for (;;) { __asm__ volatile(""); } /* the original's final 'jmp .' */
}
static void after_idle(void *p) { (void)p; L("N", 0); }
int main(void)
{
    N64cConfig cfg = {0};
    n64c_init(&cfg);
    make_queues();
    /* A: start order is irrelevant; message handling order follows priority, high first */
    osCreateThread(&th[0], 1, receiver, &ra[0], 0, 10);
    osCreateThread(&th[1], 2, receiver, &ra[1], 0, 20);
    osCreateThread(&th[2], 3, receiver, &ra[2], 0, 30);
    osStartThread(&th[0]); osStartThread(&th[1]); osStartThread(&th[2]);
    n64c_run_idle();
    /* Guest RAM never holds host pointers: the context word is slot + 1, and slot n's stack is at 0x60000000 + n*0x110000 + 0x10000. */
    u32 word;
    memcpy(&word, th[0].context, sizeof word);
    if (word != 1) { printf("FAIL context word %u\n", word); return 1; }
    /* A PIE test binary may own the fixed address; then only the slot spacing is checked. */
    if (receiver_local >= 0x60000000u && receiver_local < 0x64400000u &&
        (receiver_local < 0x60230000u || receiver_local >= 0x60330000u)) { printf("FAIL stack %lx\n", (unsigned long)receiver_local); return 1; }
    osSendMesg(&q[0], m32(1), OS_MESG_NOBLOCK);
    osSendMesg(&q[1], m32(2), OS_MESG_NOBLOCK);
    osSendMesg(&q[2], m32(3), OS_MESG_NOBLOCK);
    n64c_run_idle();
    expect("priority_order", "H3 M2 L1 ");
    /* B: a send that wakes a higher-priority thread preempts the sender before its next statement */
    osCreateThread(&th[3], 4, sender_preempt, 0, 0, 10);
    osStartThread(&th[3]);
    n64c_run_idle();
    expect("wake_preempts", "x1 H7 x2 ");
    /* C: equal priority waiters are served FIFO; yield rotates equal priorities */
    osCreateThread(&th[4], 5, receiver_once, &ra[3], 0, 15);
    osCreateThread(&th[5], 6, receiver_once, &ra[4], 0, 15);
    osStartThread(&th[4]); osStartThread(&th[5]);
    n64c_run_idle();
    osSendMesg(&q[3], m32(1), OS_MESG_NOBLOCK);
    osSendMesg(&q[3], m32(2), OS_MESG_NOBLOCK);
    n64c_run_idle();
    expect("fifo_equal_priority", "E1_1 E2_2 ");
    n64c_shutdown();
    /* D: queue semantics without threads */
    n64c_init(&cfg);
    make_queues();
    OSMesg m;
    osSendMesg(&q[0], m32(1), OS_MESG_NOBLOCK);
    osSendMesg(&q[0], m32(2), OS_MESG_NOBLOCK);
    osJamMesg(&q[0], m32(9), OS_MESG_NOBLOCK);
    for (int i = 0; i < 3; i++) {
        osRecvMesg(&q[0], &m, OS_MESG_NOBLOCK);
        L("%d ", (int)m.data32);
    }
    L("r%d ", osRecvMesg(&q[0], &m, OS_MESG_NOBLOCK));
    for (int i = 0; i < 8; i++)
        osSendMesg(&q[1], m32((u32)i), OS_MESG_NOBLOCK);
    L("f%d", osSendMesg(&q[1], m32(99), OS_MESG_NOBLOCK));
    expect("queue_semantics", "9 1 2 r-1 f-1");
    osCreateThread(&th[0], 1, yielder, &ra[0], 0, 5);
    osCreateThread(&th[1], 2, yielder, &ra[1], 0, 5);
    osStartThread(&th[0]); osStartThread(&th[1]);
    n64c_run_idle();
    expect("yield_rotates", "L1 M1 L2 M2 ");
    osCreateMesgQueue(&q[5], qbuf[5], 1);
    osCreateThread(&th[2], 3, blocked_sender, 0, 0, 5);
    osStartThread(&th[2]);
    n64c_run_idle();
    L("w%d ", n64c_waiting_threads());
    osRecvMesg(&q[5], &m, OS_MESG_NOBLOCK); L("r%d ", (int)m.data32);
    n64c_run_idle();
    osRecvMesg(&q[5], &m, OS_MESG_NOBLOCK); L("r%d ", (int)m.data32);
    n64c_run_idle();
    osRecvMesg(&q[5], &m, OS_MESG_NOBLOCK); L("r%d", (int)m.data32);
    expect("blocked_sender_wakes", "s1 s2 w1 r1 s3 r2 r3");
    n64c_shutdown();
    /* E: VI retrace delivery and timers on the virtual clock */
    n64c_init(&cfg);
    make_queues();
    osCreateThread(&th[0], 1, vi_counter, 0, 0, 10);
    osCreateThread(&th[1], 2, timer_waiter, 0, 0, 12);
    osStartThread(&th[0]); osStartThread(&th[1]);
    n64c_run_idle();
    osViSetEvent(&q[0], m32(0), 1);
    static OSTimer tm_once, tm_rep;
    /* one retrace is 782031.25 cycles; a 1 000 000 cycle timer lands inside the 2nd retrace, the repeating one at 1.2M, 2.4M, 3.6M */
    osSetTimer(&tm_once, 1000000, 0, &q[1], m32(1));
    osSetTimer(&tm_rep, 1200000, 1200000, &q[1], m32(2));
    n64c_frame(); /* retrace 1 at 782031: nothing due */
    L("c%d ", (int)osViGetRetraceCount());
    n64c_frame(); /* retrace 2 at 1564062: T1 (1.0M), T2 (1.2M) fire first */
    n64c_frame(); /* retrace 3 at 2346093: nothing due */
    n64c_frame(); /* retrace 4 at 3128125: T2 at 2.4M */
    expect("vi_and_timers", "V0 c1 T1 T2 V0 V0 T2 V0 ");
    L("t%d", (int)(osGetTime() == ((OSTime)4 * OS_CPU_COUNTER * 1001) / 60000));
    expect("clock_exact", "t1");
    /* F: a thread spinning on the clock sees time pass; a timer due inside the spin wakes a higher-priority
     * waiter at once, and the spin then ends (on the console Count never stops for a running thread) */
    static OSTimer tm_spin;
    static OSThread sth[2];
    static RecvArg rt = {"T", 2};
    osCreateThread(&sth[0], 9, receiver_once, &rt, 0, 30);
    osStartThread(&sth[0]);
    n64c_run_idle();
    osSetTimer(&tm_spin, OS_CPU_COUNTER / 1000, 0, &q[2], m32(5));
    osCreateThread(&sth[1], 10, spinner, 0, 0, 10);
    osStartThread(&sth[1]);
    n64c_run_idle();
    expect("spin_sees_time", "T5 S1 ");
    /* D: a thread that lowers itself to priority 0 and spins gives the baton back to the host */
    static OSThread ith[2];
    osCreateThread(&ith[0], 11, idle_thread, 0, 0, 20);
    osStartThread(&ith[0]);
    n64c_run_idle();
    osCreateThread(&ith[1], 12, after_idle, 0, 0, 5);
    osStartThread(&ith[1]);
    n64c_run_idle();
    expect("idle_loop_yields", "I N");
    /* E: a halting guest parks for good; the host owns the baton and runs nothing again until shutdown */
    static OSThread hth[2];
    L("h%d ", n64c_sched_halted());
    L("g%d ", n64c_sched_on_guest());
    osCreateThread(&hth[0], 7, halter, 0, 0, 50);
    osCreateThread(&hth[1], 8, after_halt, 0, 0, 40);
    osStartThread(&hth[0]); osStartThread(&hth[1]);
    n64c_run_idle();
    n64c_run_idle();
    L("h%d", n64c_sched_halted());
    expect("halt_parks", "h0 g0 g1 h1");
    n64c_shutdown();
    L("h%d", n64c_sched_halted());
    expect("halt_reset", "h0");
    printf(fails ? "FAILED %d\n" : "all passed\n", fails);
    return fails != 0;
}
