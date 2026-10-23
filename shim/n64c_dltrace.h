/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_DLTRACE_H
#define N64C_DLTRACE_H
/* A readable record of what one display-list walk draws (N64C_TAS_TRACE_DRAWS, see n64c_tas.h): one line per
 * triangle command or rectangle with the texture it samples (address, format, size), the palette, the
 * combiner, the prim/env/fill colours and the geometry (object and clip coordinates, or the rectangle in
 * pixels with its scale), so a wrong picture can be traced to the draw and the data that made it. The walker
 * (n64c_dl) feeds every command it reads, in RSP order, while a trace is open; the trace keeps the RDP state. */
#include <stdint.h>
#include <stdio.h>
#ifdef __cplusplus
extern "C" {
#endif
typedef struct N64cTraceVtx {
    int16_t ob[3], st[2];
    float clip[4];
    int loaded, clip_known;
} N64cTraceVtx;
/* Start writing to `out` (NULL stops); the RDP state starts unknown. */
void n64c_dltrace_begin(FILE *out);
int n64c_dltrace_active(void);
/* Nested pause (+1) and resume (-1) while the walker follows a path the RSP may not take (G_BRANCH_Z target). */
void n64c_dltrace_pause(int delta);
/* One command: its words; `timg` is the physical address of a G_SETTIMG; `half1`/`half2` the words after a texture rectangle. */
void n64c_dltrace_cmd(uint32_t w0, uint32_t w1, uint32_t timg, uint32_t half1, uint32_t half2);
/* A triangle command: `count` vertex views (1 or 2 triangles are 3 or 6) and the modelview top (16 floats, or NULL when unknown). */
void n64c_dltrace_tris(uint32_t w0, int count, const N64cTraceVtx *v, const float *mv);
#ifdef __cplusplus
}
#endif
#endif
