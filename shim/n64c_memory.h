/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_MEMORY_H
#define N64C_MEMORY_H
#include "n64c_os.h"
/* Native i386 address windows. Register typed PI resources before opening.
 * ROM and RAM aliases share backing pages. No guest instruction is executed.
 * Close only after guest threads have stopped. Unsupported platforms refuse. */
typedef struct {
    u32 rdram_bytes, rom_bytes, video_clock_hz;
} N64cMemoryConfig;
int n64c_memory_open(const N64cMemoryConfig *config);
void n64c_memory_close(void);
void *n64c_memory_ram(u32 physical, u32 bytes);
u32 n64c_memory_physical(const void *pointer);
/* Nonzero when a /proc/<pid>/status text names a tracer (TracerPid not 0). */
int n64c_memory_tracer_in_status(const char *text);
/* Length, register, direction and immediate of the 32-bit mov at code, or 0 for any other instruction. */
int n64c_memory_decode_mov(const unsigned char *code,unsigned *length,unsigned *reg,unsigned *write,u32 *immediate);
#endif
