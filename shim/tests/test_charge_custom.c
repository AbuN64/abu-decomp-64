/* SPDX-License-Identifier: GPL-3.0-or-later */
/* A unit compiled by a custom command with N64C_CHARGE_COMPILE_FLAGS is charged on entry with no hook call written
 * here: the compiler's entry call alone moves the guest clock (CONTRACTS.md "Work scale"). */
#include "../n64c_internal.h"
#include <stdio.h>
#include <stdlib.h>

static unsigned checks;
#define CHECK(x) do { checks++; if (!(x)) { fprintf(stderr, "charge_custom:%d: %s\n", __LINE__, #x); exit(1); } } while (0)
extern void n64c_test_custom_entry(void);
static OSThread worker_thread;
static OSTime after_charged;
static void worker(void *arg)
{
    (void)arg;
    for (int i = 0; i < 100; i++) n64c_test_custom_entry();
    after_charged = n64c_clock_now();
}
int main(void)
{
    N64cConfig cfg = {0};
    n64c_init(&cfg);
    n64c_clock_set_work((unsigned long long)1000 << N64C_WORK_FRAC);
    n64c_test_custom_entry(); /* the host holds no baton: nothing is charged */
    CHECK(n64c_clock_now() == 0);
    osCreateThread(&worker_thread, 2, worker, 0, 0, 10);
    osStartThread(&worker_thread);
    n64c_run_idle();
    CHECK(after_charged == 4687); /* 100 entries of 1000 ns at 46.875 Count ticks per us */
    n64c_shutdown();
    printf("charge_custom: %u checks\n", checks);
    return 0;
}
