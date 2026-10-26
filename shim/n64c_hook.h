/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Optional functions: a build may or may not link them (run-on diagnostics, boot census, coverage), and the shim tests
 * the address before it calls. On ELF an undefined weak reference is NULL. On Windows (PE) it is not: the linker resolves
 * it to an unrelated default symbol, so `if (hook)` would be true and call the wrong function. The Windows build has none
 * of those builds, so there a hook's name reads as the absent function, a constant NULL of its type (N64C_ABSENT).
 * A function the Windows build always links but another build may not (the game's stop function) is declared plain
 * there; N64C_LINKED(f) is the test either way. */
#ifndef N64C_HOOK_H
#define N64C_HOOK_H
#ifdef _WIN32
#define N64C_ABSENT(type) ((type)0)
#define N64C_LINKED(function) 1
/* boot/trace.c census, used by the shim and the boot driver */
#define n64c_trace_event N64C_ABSENT(void (*)(const char *))
#define n64c_trace_summary N64C_ABSENT(void (*)(void))
#define n64c_trace_reach_tick N64C_ABSENT(void (*)(long))
#else
#define N64C_LINKED(function) ((function) != 0)
void n64c_trace_event(const char *kind) __attribute__((weak));
void n64c_trace_summary(void) __attribute__((weak));
void n64c_trace_reach_tick(long poll) __attribute__((weak));
#endif
#endif
