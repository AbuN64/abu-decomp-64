/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_DECOMP_TRACE_H
#define N64C_DECOMP_TRACE_H
/* Boot hit census. The oracle's boot-to-menu trace (boot-trace.tsv, in first-hit order) is compiled
 * into n64c_trace_entries by trace.py. With RAGE_DECOMP_TRACE the original units are built with
 * -finstrument-functions and the first entry of each trace function prints
 *     boot-step: <n>/<total> <function>
 * so the supervisor's console follows the boot and n is measured, not inferred from a stop. */
typedef struct {
    unsigned order;       /* position in the oracle's first-hit order, 1-based */
    unsigned address;     /* us-rev1 virtual address the oracle saw */
    int shim_bound;       /* bound to a clean shim: no original body to instrument */
    const char *names[5]; /* symbols that may name the function in this source pin, 0-terminated */
} N64cTraceEntry;
extern const N64cTraceEntry n64c_trace_entries[];
extern const unsigned n64c_trace_count;
/* Resolve the entries and arm the hook. Call before the original entry runs. */
void n64c_trace_init(void) __attribute__((weak));
/* First occurrence of a runtime milestone (thread start, PI DMA, gfx task, audio task). */
void n64c_trace_event(const char *kind) __attribute__((weak));
/* Where the census stands; prints once (the stop that ends the run calls it). */
void n64c_trace_summary(void) __attribute__((weak));
#endif
