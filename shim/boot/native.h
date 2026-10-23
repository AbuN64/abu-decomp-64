/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_DECOMP_NATIVE_H
#define N64C_DECOMP_NATIVE_H
/* Compiler boundary only. Game bodies remain in the fetched source tree.
 * Mark only macros selected by the real version/NON_MATCHING preprocessor.
 * A separate translation unit supplies each non-returning missing function,
 * avoiding conflicts with the game's own declarations and return types. */
#define INCLUDE_ASM_H
#define INCLUDE_ASM(segment, name) extern void n64c_missing_marker_##name(void)
#define INCLUDE_DATA(segment, name) extern char n64c_data_marker_##name
#define INCLUDE_RODATA(segment, name) extern char n64c_data_marker_##name
#define INCLUDE_BSS(segment, name, size) extern char n64c_data_marker_##name
/* The fetched stdarg provider is for MIPS. Native C must use its compiler's
 * calling convention. The source's va_list is char*, as on the required i386
 * GNU ABI; no MIPS argument walking or compiler implementation is linked. */
#define UNBAKE_PUBLIC_STDARG_H
#define va_start(ap, last) __builtin_va_start(ap, last)
#define va_arg(ap, type) __builtin_va_arg(ap, type)
#define va_end(ap) __builtin_va_end(ap)
#define va_copy(dst, src) __builtin_va_copy(dst, src)
#endif
