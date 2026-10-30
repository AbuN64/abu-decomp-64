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
/* Variadic values always follow the native compiler ABI, including targets
 * whose va_list is a record rather than a byte pointer. */
#include <stdarg.h>
#endif
