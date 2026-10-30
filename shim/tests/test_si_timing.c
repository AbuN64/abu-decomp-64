/* SPDX-License-Identifier: GPL-3.0-or-later */
/* A controller read is a packet exchange that takes bus time and ends in the SI interrupt, as on the console: the pads
 * are sampled when the PIF answers, the call returns with the exchange running, and the interrupt (one read time
 * later) is the message the game's next poll waits for. A query is the status command's write and read. Times are Count
 * ticks (46.875 MHz): 64 us = 3000, 576 us = 27000, from T0, the end of the host's first query. */
#include "../n64c_internal.h"
#include "../n64c_cont.h"
#include <stdio.h>
#include <stdlib.h>

static unsigned checks;
#define CHECK(x) do { checks++; if (!(x)) { fprintf(stderr, "si_timing:%d: %s\n", __LINE__, #x); exit(1); } } while (0)
enum { WRITE = 3000, READ = 27000, READS = 3 };
static OSMesgQueue q;
static OSMesg slot[1];
static OSThread poller, idler;
static OSTime sample_at[8], call_at[READS], ret_at[READS], done_at[READS];
static int samples;
static OSTime T0;
static void pad_source(int port, OSContPad *p, void *user)
{
    (void)port; (void)p; (void)user;
    sample_at[samples++] = osGetTime();
}
static int idle_wait_returned;
static void idle_entry(void *arg)
{
    (void)arg;
    OSMesg m = {.data32 = 99};
    /* a reply wait with no exchange started and no SI interrupt pending is an idle-bus round trip, not a stop */
    CHECK(osRecvMesg(&q, &m, OS_MESG_BLOCK) == 0 && m.ptr == NULL);
    idle_wait_returned = 1;
}
static void poll_entry(void *arg)
{
    (void)arg;
    OSMesg m;
    for (int i = 0; i < READS; i++) {
        if (i == 2) {
            CHECK(osContStartQuery(&q) == 0); /* another exchange: the next read has to write its request again */
            CHECK(osRecvMesg(&q, &m, OS_MESG_BLOCK) == 0);
        }
        call_at[i] = osGetTime();
        CHECK(osContStartReadData(&q) == 0);
        ret_at[i] = osGetTime();
        CHECK(osRecvMesg(&q, &m, OS_MESG_BLOCK) == 0 && m.data32 == 1); /* the SI interrupt's message */
        done_at[i] = osGetTime();
    }
}
int main(void)
{
    N64cConfig cfg = {0};
    n64c_init(&cfg);
    osCreateMesgQueue(&q, slot, 1);
    osSetEventMesg(OS_EVENT_SI, &q, (OSMesg){.data32 = 1});
    n64c_cont_set_input(pad_source, NULL);
    /* names q the controller reply queue: the write is waited out, the call returns with the read on the bus */
    CHECK(osContStartQuery(&q) == 0 && osGetTime() == WRITE && q.validCount == 0);
    n64c_clock_advance_to(WRITE + READ);
    CHECK(osRecvMesg(&q, NULL, OS_MESG_NOBLOCK) == 0);
    T0 = osGetTime();
    osCreateThread(&idler, 2, idle_entry, NULL, NULL, 10);
    osStartThread(&idler);
    n64c_run_idle();
    CHECK(idle_wait_returned == 1 && osGetTime() == T0);
    osCreateThread(&poller, 1, poll_entry, NULL, NULL, 10);
    osStartThread(&poller);
    n64c_run_idle();
    /* first read: the request is written first; the caller waits for that interrupt and nothing is sampled yet */
    CHECK(samples == 0 && osGetTime() == T0);
    n64c_clock_advance_to(T0 + WRITE - 1);
    CHECK(samples == 0);
    n64c_clock_advance_to(T0 + WRITE);
    CHECK(samples == 1 && sample_at[0] == T0 + WRITE && ret_at[0] == T0 + WRITE && done_at[0] == 0);
    /* the exchange is on the bus: the call returned, the interrupt has not come, and a second start is refused */
    n64c_clock_advance_to(T0 + WRITE + READ - 1);
    CHECK(done_at[0] == 0);
    CHECK(osContStartReadData(&q) == -1 && samples == 1);
    /* one big step: the device events run at their own times, ahead of the end of the step, in order */
    n64c_clock_advance_to(T0 + 1000000);
    CHECK(done_at[0] == T0 + WRITE + READ);
    /* second read: the request is already in the PIF, so it is sampled at once and only the answer takes time */
    CHECK(call_at[1] == T0 + WRITE + READ && sample_at[1] == T0 + WRITE + READ && ret_at[1] == T0 + WRITE + READ);
    CHECK(done_at[1] == T0 + WRITE + 2 * READ);
    /* after a query (its write and read) the read writes its request again */
    CHECK(call_at[2] == T0 + 2 * WRITE + 3 * READ && sample_at[2] == call_at[2] + WRITE && ret_at[2] == sample_at[2]);
    CHECK(done_at[2] == sample_at[2] + READ);
    CHECK(samples == READS && n64c_sched_alive() == 0);
    n64c_shutdown();
    printf("si_timing: %u checks passed\n", checks);
    return 0;
}
