/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_F3D_H
#define N64C_F3D_H
/* n64c_f3d: the port's Fast3D renderer, callable from C (the original-C boot and the replay tool).
 * Display lists go through n64c_dl first, so Fast3D only sees host pointers. Every colour image the
 * game binds (G_SETCIMG, by physical address, other than its depth image) is its own Fast3D
 * framebuffer that keeps its pixels from one task to the next, so a double-buffered game draws into
 * one while the VI shows the other, and the surface it presents can be read back. */
#include <stdint.h>
#include "n64c_dl.h"
#ifdef __cplusplus
extern "C" {
#endif
typedef struct N64cF3d N64cF3d;
typedef struct N64cF3dConfig {
    const char *shaders;     /* fast3d-shaders.o2r (Fast3D's own shader sources); NULL: beside the executable */
    const char *settings;    /* renderer settings file (created when missing); NULL: beside the executable */
    uint32_t window_w, window_h;
    uint32_t fb_w, fb_h;     /* the screen size the window maps (VI output) and the colour image height until the game sets a scissor */
    int software;            /* nonzero: render on Mesa's software EGL device when present */
    int offscreen;           /* nonzero: no visible window (SDL's offscreen video driver) */
} N64cF3dConfig;
/* NULL when the window, GL context or shader archive cannot be opened (reason on stderr). */
N64cF3d *n64c_f3d_open(const N64cF3dConfig *config);
/* Render the list at dl from mem (one graphics task). Returns the commands translated, or -1. */
int n64c_f3d_run(N64cF3d *f3d, const N64cGfxMem *mem, uint32_t dl, N64cDlStats *stats);
/* Physical address of the colour image the latest run drew to last, 0 when it bound none. */
uint32_t n64c_f3d_surface(const N64cF3d *f3d);
/* The colour image at physical address phys as RGBA8 (red in the low byte, top row first), resampled
 * bilinearly to w x h (the VI's scaling). 0 when no run has drawn to that address. */
int n64c_f3d_read(N64cF3d *f3d, uint32_t phys, uint32_t *rgba, uint32_t w, uint32_t h);
/* Show the colour image at physical address phys on the window, scaled to fill it (the VI's output), and
 * flip. phys 0, or an address no run has drawn, shows black. Returns 0 once the window has been closed. */
int n64c_f3d_present(N64cF3d *f3d, uint32_t phys);
/* n64c_f3d_present with the port's own status text (ASCII, lines split by newlines) drawn over the image on
 * a dark panel at the top left: what the port shows when the original run cannot draw (a boot stop). */
int n64c_f3d_present_text(N64cF3d *f3d, uint32_t phys, const char *text);
const char *n64c_f3d_renderer(const N64cF3d *f3d);
void n64c_f3d_close(N64cF3d *f3d);
#ifdef __cplusplus
}
#endif
#endif
