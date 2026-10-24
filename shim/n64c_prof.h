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
/* The client starts after the guest address windows are mapped (its startup allocations must not take them) and
 * flushes at exit. */
#include <stdlib.h>
static inline void n64c_prof_start(void) { ___tracy_startup_profiler(); atexit(___tracy_shutdown_profiler); }
#define N64C_PROFILE_START() n64c_prof_start()
#else
#define N64C_ZONE(id, name) ((void)0)
#define N64C_ZONE_END(id) ((void)0)
#define N64C_FRAME_MARK() ((void)0)
#define N64C_PROFILE_START() ((void)0)
#endif
#endif
