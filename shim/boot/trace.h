/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_DECOMP_TRACE_H
#define N64C_DECOMP_TRACE_H
#include <stdint.h>
#include "../n64c_hook.h"
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
/* Effective link bindings grouped by native entry point. The hook logs the first
 * actual entry, independently of the original-code census and lockstep. */
typedef struct {
    const char *native;
    const char *bound;
} N64cShimEntry;
extern const N64cShimEntry n64c_shim_entries[];
extern const unsigned n64c_shim_count;
/* The original functions whose entries charge guest work (RUNTIME.md "Virtual guest time"): every function of the
 * version's symbols outside the SDK range and not bound to native code. */
extern const char *const n64c_trace_charged[];
extern const unsigned n64c_trace_charged_count;
/* Resolve the entries and arm the hook. Call before the original entry runs. */
#ifdef _WIN32
#define n64c_trace_init N64C_ABSENT(void (*)(void)) /* n64c_hook.h: the Windows build has no census */
#else
void n64c_trace_init(void) __attribute__((weak));
#endif
/* First occurrence of a runtime milestone (thread start, PI DMA, gfx task, audio task): n64c_trace_event, in n64c_hook.h. */
/* Where the census stands; prints once (the stop that ends the run calls it): n64c_trace_summary, in n64c_hook.h. */

/* Call frames for the run-on diagnostics (runon.c). The entry hook also keeps, per thread, the instrumented frames
 * that are live: the function, its frame pointer (the units are built with -fno-omit-frame-pointer), its return
 * address, and the callee-saved registers as its caller had them where the function had not saved them yet. */
enum { N64C_TRACE_DEPTH = 512 };
typedef struct {
    void *fn, *frame, *site;
    unsigned long regs[3]; /* ebx, esi, edi at the entry hook (i386); 0 elsewhere */
} N64cTraceFrame;
typedef struct N64cTraceStack {
    unsigned long thread;   /* pthread_t of the owner */
    unsigned depth;         /* may exceed N64C_TRACE_DEPTH: deeper frames are counted, not kept */
    struct N64cTraceStack *next;
    N64cTraceFrame frame[N64C_TRACE_DEPTH];
} N64cTraceStack;
/* The calling thread's stack (NULL before its first instrumented call). */
N64cTraceStack *n64c_trace_stack(void);
/* The stack of the thread that entered an instrumented function last (the baton holder), and all stacks. */
N64cTraceStack *n64c_trace_last_stack(void);
N64cTraceStack *n64c_trace_stacks(void);
/* The function symbol containing address (offset into it), or NULL. */
const char *n64c_trace_symbol(const void *address, unsigned *offset);
/* Census position: first hits so far, the trace size, and the last function hit (NULL before the first). */
unsigned n64c_trace_position(unsigned *total, const char **last);
/* Run-on diagnostics (runon.c, built with the census): init arms them when N64C_RUN_ON=1 (returns 1 then); end prints
 * the run's failure list and returns the exit code (78 when anything failed, else rc). */
#ifdef _WIN32
#define n64c_runon_init N64C_ABSENT(int (*)(void))
#define n64c_runon_end N64C_ABSENT(int (*)(int))
#else
int n64c_runon_init(void) __attribute__((weak));
int n64c_runon_end(int rc) __attribute__((weak));
#endif
/* Lockstep (N64C_LOCKSTEP=1 with N64C_LOCKSTEP_DIR, N64C_LOCKSTEP_WATCH; see trace.c and lockstep.py): page hashes of
 * RDRAM at each first hit plus the first calls' arguments and returns, compared offline with the oracle's. */
uint64_t n64c_lockstep_hash(const unsigned char *page);
/* The reach stream (boot/reach.h): once per poll n64c_trace_reach_tick (n64c_hook.h) sends the run's newly entered functions
 * on the datagram socket N64C_INPUT + ".reach", for the rows named by the file N64C_REACH_ROWS. */
#endif
