/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_BOOT_STOP_H
#define N64C_BOOT_STOP_H
/* Boot stops (missing.c). A call into original code the port has not linked yet ends the run in
 * n64c_boot_missing: it reports the symbol, records it, gives n64c_boot_stop_hook (when set) the chance
 * to show it, and ends in n64c_boot_stopped, the hold point a supervising debugger breaks on.
 * The one exception is the walk's run-on diagnostic boot (runon.c, N64C_RUN_ON=1): it records the stop and the
 * call returns 0 (eax:edx), so one boot lists every unlinked call in boot order. */
extern const char *volatile n64c_boot_stop_symbol;
/* Called on the thread that hit the stop. It may not return (a guest thread is parked). */
extern void (*volatile n64c_boot_stop_hook)(const char *symbol);
/* Coverage flavour (coverage.c): write the gcov counters. Call it before n64c_boot_stopped, where a debugger holds and
 * then kills the process. Absent (NULL) in every other build. */
#ifdef _WIN32
#include "../n64c_hook.h"
#define n64c_coverage_dump N64C_ABSENT(void (*)(void))
#else
void n64c_coverage_dump(void) __attribute__((weak));
#endif
__attribute__((noreturn)) void n64c_boot_stopped(void);
long long n64c_boot_missing(const char *symbol);
#endif
