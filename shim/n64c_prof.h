/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Profiler zones at the shim's platform boundaries. Compiled out (empty macros) unless N64C_PROFILE is defined,
 * which the port's `profile` build sets together with the Tracy client include path. One header, no other coupling. */
#ifndef N64C_PROF_H
#define N64C_PROF_H
#ifdef N64C_PROFILE
#include <tracy/TracyC.h>
#define N64C_ZONE(id, name) TracyCZoneN(id, name, 1)
#define N64C_ZONE_END(id) TracyCZoneEnd(id)
#define N64C_FRAME_MARK() TracyCFrameMark
#define N64C_PLOT(name, value) TracyCPlot(name, (double)(value))
/* The client starts after the guest address windows are mapped (its startup allocations must not take them) and
 * flushes at exit. */
#include <stdlib.h>
static inline void n64c_prof_start(void) { ___tracy_startup_profiler(); atexit(___tracy_shutdown_profiler); }
#define N64C_PROFILE_START() n64c_prof_start()
#else
#define N64C_ZONE(id, name) ((void)0)
#define N64C_ZONE_END(id) ((void)0)
#define N64C_FRAME_MARK() ((void)0)
#define N64C_PLOT(name, value) ((void)0)
#define N64C_PROFILE_START() ((void)0)
#endif
/* Resident set and heap in use, as plots (Linux/glibc; other hosts plot nothing). Called about once a second. */
#if defined(N64C_PROFILE) && defined(__linux__)
#include <stdio.h>
#include <malloc.h>
static inline void n64c_prof_memory(void)
{
    FILE *f = fopen("/proc/self/statm", "r");
    long pages = 0, rss = 0;
    if (f) {
        if (fscanf(f, "%ld %ld", &pages, &rss) != 2) rss = 0;
        fclose(f);
        N64C_PLOT("rss_kb", rss * 4);
    }
#if defined(__GLIBC__) && (__GLIBC__ > 2 || __GLIBC_MINOR__ >= 33)
    struct mallinfo2 mi = mallinfo2();
    N64C_PLOT("heap_in_use_kb", (mi.uordblks + mi.hblkhd) / 1024);
#endif
}
#define N64C_PROFILE_MEMORY() n64c_prof_memory()
#else
#define N64C_PROFILE_MEMORY() ((void)0)
#endif
#endif
