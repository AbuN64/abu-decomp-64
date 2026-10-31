/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The entry charge (n64c_charge.h): the compiler's entry call of a charged function moves the guest clock by the
 * project's work scale; an uncharged one and a thread without the baton do not (test_charge_refuse.c: no declaration). */
#include "../n64c_charge.h"
#include "../n64c_internal.h"
#include <stdio.h>
#include <stdlib.h>

static unsigned checks;
#define CHECK(x) do { checks++; if (!(x)) { fprintf(stderr, "charge:%d: %s\n", __LINE__, #x); exit(1); } } while (0)
enum { ENTRY_NS = 1000 };
extern void n64c_test_charged_entry(void);
__attribute__((noinline)) static void uncharged_entry(void) {}
void __cyg_profile_func_enter(void *fn, void *call_site);
static OSThread worker_thread;
static OSTime after_charged, after_uncharged, retrace_two_at;
static unsigned retraces_before, retraces_after;
static void worker(void *arg)
{
    (void)arg;
    for (int i = 0; i < 100; i++) __cyg_profile_func_enter((void *)n64c_test_charged_entry, 0);
    after_charged = n64c_clock_now();
    for (int i = 0; i < 100; i++) __cyg_profile_func_enter((void *)uncharged_entry, 0);
    after_uncharged = n64c_clock_now();
    /* retrace 2 falls due at 16.6667 ms: the 16667th entry, and no earlier one, delivers it */
    for (int i = 0; i < 16566; i++) __cyg_profile_func_enter((void *)n64c_test_charged_entry, 0);
    retraces_before = osViGetRetraceCount();
    __cyg_profile_func_enter((void *)n64c_test_charged_entry, 0);
    retraces_after = osViGetRetraceCount();
    retrace_two_at = n64c_clock_now();
}
int main(void)
{
    N64cConfig cfg = {0};
    n64c_init(&cfg); /* the table of test_charged_table.c: declared scale, one charged function */
    n64c_clock_set_work((unsigned long long)ENTRY_NS << N64C_WORK_FRAC);
    __cyg_profile_func_enter((void *)n64c_test_charged_entry, 0); /* the host holds no baton: nothing is charged */
    CHECK(n64c_clock_now() == 0);
    osCreateThread(&worker_thread, 2, worker, 0, 0, 10);
    osStartThread(&worker_thread);
    n64c_run_idle();
    CHECK(after_charged == 4687);                    /* 100 entries of 1000 ns: 100000 ns of 46.875 Count ticks per us */
    CHECK(after_uncharged == after_charged);         /* a function outside the charged table costs nothing */
    CHECK(retraces_before == 1 && retraces_after == 2); /* 16666 entries into the second frame: not yet; the next: due */
    CHECK(retrace_two_at >= 781250 - 1 && retrace_two_at <= 781250 + 47);
    n64c_shutdown();
    printf("charge: %u checks\n", checks);
    return 0;
}
