/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_MMIO_H
#define N64C_MMIO_H
#include "n64c_os.h"
/* Register transactions run in normal thread context, never a signal handler.
 * Only aligned 32-bit accesses to the documented PI/VI/AI/SI pages are accepted.
 * Return zero for an unsupported transaction instead of fabricating hardware. */
void n64c_mmio_reset(u32 video_clock_hz);
int n64c_mmio_read(u32 physical, u32 *value);
int n64c_mmio_write(u32 physical, u32 value);
#endif
