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
#else
#define N64C_ZONE(id, name) ((void)0)
#define N64C_ZONE_END(id) ((void)0)
#define N64C_FRAME_MARK() ((void)0)
#endif
#endif
