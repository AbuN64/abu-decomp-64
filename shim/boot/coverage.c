/* SPDX-License-Identifier: GPL-3.0-or-later */
/* gcov coverage flavour (RAGE_DECOMP_COVERAGE): write the counters on every way the port ends. libgcov dumps at a
 * normal exit() by itself; the port leaves through _Exit (movie end, boot stops, host faults), abort and signals,
 * which skip it. The linker wraps those three calls (cmake/decomp.cmake) and a constructor arms the fatal signals. */
#define _GNU_SOURCE
#include <signal.h>
#include <stdlib.h>
#include <unistd.h>

extern void __gcov_dump(void);
extern void __real__Exit(int status) __attribute__((noreturn));
extern void __real__exit(int status) __attribute__((noreturn));
extern void __real_abort(void) __attribute__((noreturn));

void __wrap__Exit(int status)
{
    __gcov_dump();
    __real__Exit(status);
}

void __wrap__exit(int status)
{
    __gcov_dump();
    __real__exit(status);
}

void __wrap_abort(void)
{
    __gcov_dump();
    __real_abort();
}

/* The supervisor ends a hung or stopped run with SIGTERM; a fault the shim does not take arrives as SIGBUS, SIGFPE
 * or SIGILL. SIGSEGV belongs to the shim (guest TLB service) and SIGABRT already passed through the wrapper. */
static void dump_and_die(int sig)
{
    __gcov_dump();
    signal(sig, SIG_DFL);
    raise(sig);
}

__attribute__((constructor)) static void arm_coverage_signals(void)
{
    static const int fatal[] = {SIGTERM, SIGINT, SIGHUP, SIGBUS, SIGFPE, SIGILL};
    for (unsigned i = 0; i < sizeof fatal / sizeof *fatal; i++)
        signal(fatal[i], dump_and_die);
}


void n64c_coverage_dump(void)
{
    __gcov_dump();
}
