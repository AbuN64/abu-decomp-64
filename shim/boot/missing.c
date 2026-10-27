/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "stop.h"
#include "n64c_diag.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
/* The stop that ended the run, for a debugger holding the process in n64c_boot_stopped. */
const char *volatile n64c_boot_stop_symbol;
void (*volatile n64c_boot_stop_hook)(const char *symbol);
/* The process ends here. A supervisor that keeps the last presented frame on screen breaks on
 * this function and holds the process after the stop has been reported. */
__attribute__((noinline, noreturn)) void n64c_boot_stopped(void)
{
    _Exit(78);
}
/* N64C_MISSING_SWEEP=1: the sweep boot. Each unlinked symbol is reported once as "boot-missing-sweep: SYMBOL" on its
 * first call and the call returns 0, so one run lists every stub the match reaches. A development run, never shipped
 * behaviour: without the variable the first missing call still ends the run. */
static int sweep_record(const char *symbol)
{
    static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
    static const char *seen[256];
    static unsigned count;
    static int on = -1;
    int first = 0;
    pthread_mutex_lock(&lock);
    if (on < 0) {
        const char *v = getenv("N64C_MISSING_SWEEP");
        on = v && !strcmp(v, "1");
    }
    if (on) {
        unsigned i;
        for (i = 0; i < count; i++)
            if (!strcmp(seen[i], symbol))
                break;
        if (i == count && count < 256) {
            seen[count++] = symbol;
            first = 1;
        }
    }
    pthread_mutex_unlock(&lock);
    if (first) {
        fprintf(stderr, "boot-missing-sweep: %s\n", symbol);
        fflush(stderr);
    }
    return on;
}
long long n64c_boot_missing(const char *symbol)
{
    if (sweep_record(symbol))
        return 0;
    if (n64c_diag_missing && n64c_diag_missing(symbol))
        return 0; /* run-on: recorded in boot order; the call returns 0 and the boot goes on */
    fprintf(stderr, "boot-missing: %s\n", symbol);
    fflush(stderr);
    if (n64c_trace_summary) n64c_trace_summary();
    /* A stub may be called on a shim worker. The run ends instead of returning an invented value
     * or stranding the scheduler's baton. */
    n64c_boot_stop_symbol = symbol;
    void (*hook)(const char *) = n64c_boot_stop_hook;
    if (hook)
        hook(symbol);
    if (n64c_coverage_dump) n64c_coverage_dump();
    n64c_boot_stopped();
}
