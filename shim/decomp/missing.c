/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "stop.h"
#include <stdio.h>
#include <stdlib.h>
/* The stop that ended the run, for a debugger holding the process in n64c_boot_stopped. */
const char *volatile n64c_boot_stop_symbol;
void (*volatile n64c_boot_stop_hook)(const char *symbol);
/* The process ends here. A supervisor that keeps the last presented frame on screen breaks on
 * this function and holds the process after the stop has been reported. */
__attribute__((noinline, noreturn)) void n64c_boot_stopped(void)
{
    _Exit(78);
}
extern void n64c_trace_summary(void) __attribute__((weak)); /* trace.c, when the census is built in */
void n64c_boot_missing(const char *symbol)
{
    fprintf(stderr, "boot-missing: %s\n", symbol);
    fflush(stderr);
    if (n64c_trace_summary) n64c_trace_summary();
    /* A stub may be called on a shim worker. The run ends instead of returning an invented value
     * or stranding the scheduler's baton. */
    n64c_boot_stop_symbol = symbol;
    void (*hook)(const char *) = n64c_boot_stop_hook;
    if (hook)
        hook(symbol);
    n64c_boot_stopped();
}
