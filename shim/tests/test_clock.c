/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The virtual guest clock: it advances only by the work a guest thread is charged at a function entry (the oracle's
 * HOST_GUEST_NS_PER_ENTRY), never by the host; retrace 1 is at time 0 and the rest 60 Hz of guest time; a device
 * event that falls due inside the charged span fires in time order on the charging thread. */
#include "../n64c_internal.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static unsigned checks;
#define CHECK(x) do { checks++; if (!(x)) { fprintf(stderr, "clock:%d: %s\n", __LINE__, #x); exit(1); } } while (0)
enum { ENTRY_NS = 1000 }; /* 1000 ns is 46.875 Count ticks: 64 entries are exactly 3000 ticks */
static char log_[128];
static OSMesgQueue q;
static OSMesg slot[2];
static OSTime rsp_done_at;
static OSTimer timer;
static OSThread worker_thread, waiter_thread;
static unsigned retraces_after_first, retraces_before_second, retraces_at_second;

static void L(const char *s) { strcat(log_, s); }
static void rsp_done(void *arg)
{
    (void)arg;
    rsp_done_at = n64c_clock_now();
    L("R ");
}
static void waiter(void *arg)
{
    (void)arg;
    OSMesg m;
    if (osRecvMesg(&q, &m, OS_MESG_BLOCK) == 0) {
        char t[24];
        snprintf(t, sizeof t, "T@%llu ", (unsigned long long)n64c_clock_now());
        L(t);
    }
}
static void worker(void *arg)
{
    (void)arg;
    n64c_clock_work();
    retraces_after_first = osViGetRetraceCount(); /* retrace 1 is due at time 0: the first charge delivers it */
    for (int i = 1; i < 16666; i++) n64c_clock_work();
    retraces_before_second = osViGetRetraceCount();
    n64c_clock_work();
    retraces_at_second = osViGetRetraceCount();
    for (int i = 0; i < 3000; i++) n64c_clock_work(); /* past the timer (100000) and the RSP task (50000) */
}

static void run(void)
{
    N64cConfig cfg = {0};
    n64c_init(&cfg);
    n64c_clock_set_work_ns(ENTRY_NS);
    log_[0] = 0;
    osCreateMesgQueue(&q, slot, 2);
    n64c_clock_work(); /* the host is not a guest thread: nothing is charged */
    CHECK(n64c_clock_now() == 0 && n64c_clock_now_ns() == 0);
    osCreateThread(&waiter_thread, 1, waiter, 0, 0, 20);
    osStartThread(&waiter_thread);
    n64c_run_idle();
    osSetTimer(&timer, 100000, 0, &q, (OSMesg){.data32 = 1});
    n64c_clock_schedule(50000, rsp_done, 0);
    osCreateThread(&worker_thread, 2, worker, 0, 0, 10);
    osStartThread(&worker_thread);
    n64c_run_idle();
}

int main(void)
{
    run();
    CHECK(retraces_after_first == 1);
    CHECK(retraces_before_second == 1); /* 16666 entries are 16.666 ms: retrace 2 is at 16.6667 ms */
    CHECK(retraces_at_second == 2);
    CHECK(rsp_done_at == 50000); /* a device event runs at its own time, not at the end of the charged span */
    CHECK(strcmp(log_, "R T@100000 ") == 0); /* time order: the RSP task first, then the timer's message */
    /* 16667 + 3000 charged entries of 1000 ns: ticks and nanoseconds agree, the fraction is carried, not rounded */
    CHECK(n64c_clock_now_ns() >= 19667000 - 21 && n64c_clock_now_ns() <= 19667000);
    char first[128];
    strcpy(first, log_);
    OSTime first_end = n64c_clock_now();
    /* the same run again gives the same clock: nothing about the host is in it */
    n64c_shutdown();
    run();
    CHECK(strcmp(log_, first) == 0 && n64c_clock_now() == first_end);
    /* no charge configured: guest work does not move the clock */
    n64c_shutdown();
    N64cConfig cfg = {0};
    n64c_init(&cfg);
    osCreateThread(&worker_thread, 2, worker, 0, 0, 10);
    osStartThread(&worker_thread);
    n64c_run_idle();
    CHECK(n64c_clock_now() == 0 && retraces_before_second == 0);
    /* retrace n is at (n - 1) / 60 s */
    CHECK(n64c_vi_next_retrace_time() == 781250 * osViGetRetraceCount());
    n64c_shutdown();
    printf("clock: %u checks passed\n", checks);
    return 0;
}
