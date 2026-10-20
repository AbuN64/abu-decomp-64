/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Fixed-width scalar names used by the pinned Fast3D/LUS sources (N64 SDK naming
 * convention: sN/uN = N-bit signed/unsigned, fN = N-bit float). Written for rage-ports;
 * replaces the grey LUS header of the same name (AUDIT.md). */
#pragma once
#include <stdint.h>
typedef int8_t s8;
typedef uint8_t u8;
typedef int16_t s16;
typedef uint16_t u16;
typedef int32_t s32;
typedef uint32_t u32;
typedef int64_t s64;
typedef uint64_t u64;
typedef float f32;
typedef double f64;
