/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_SYSTEM_H
#define N64C_SYSTEM_H
#include <stdarg.h>
#include "n64c_os.h"
/* SDK-owned state lives in the original bound memory, not a second store. */
typedef struct {
    u32 *initialized, *tv_type, *reset_type, *memory_size, *video_clock;
    u64 *clock_rate;
    u8 *nmi_buffer; /* exactly64bytes, preserved on warm reset */
} N64cSystemGlobals;
int n64c_system_bind(const N64cSystemGlobals *globals, u32 tv_type, u32 reset_type, u32 memory_size);
void n64c_system_initialize(void);
void n64c_system_reset(void);
void n64c_fpu_thread_start(void); /* worker entry: fresh OS FPU mode */
/* Native control-only FCSR boundary. Rounding, five trap enables and flush
 * mode are supported. Cause/condition/status state has no exact host mapping:
 * dirty host flags or unsupported requested bits stop explicitly. */
u32 n64c_fpu_get_control(void);
u32 n64c_fpu_set_control(u32 control); /* returns the previous control word */

s32 n64c_sprintf(char *buffer, const char *format, ...);
/* Compiler runtime and C library pieces the game links as its own functions. */
f64 n64c_floatdidf(s64 value); /* signed 64-bit integer to double (the compiler runtime conversion) */
void *n64c_copy_bytes(void *destination, const void *source, s32 count); /* forward byte copy, returns destination */
s32 n64c_string_length(const u8 *string);
s32 n64c_printf(void *(*emit)(void *, const void *, s32), void *target, const char *format, va_list args);
void *n64c_printf_copy(void *destination, const void *source, s32 count);
u8 *n64c_string_find(u8 *string, u8 value);
/* Mask the floating point traps the guest enabled while host code runs (Windows only; elsewhere a no-op): the mask returned
 * goes back to n64c_fp_traps_restore. */
int n64c_fp_traps_mask(void);
void n64c_fp_traps_restore(int enabled);
/* The development-cartridge handshake port (host debugger link) does not exist on the host, so the handler
 * a game registers is only remembered and never called. */
void n64c_dev_handler_set(void *handler);
void *n64c_dev_handler(void);

#endif
