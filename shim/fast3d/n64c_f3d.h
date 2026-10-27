/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_F3D_H
#define N64C_F3D_H
/* n64c_f3d: the port's Fast3D renderer, callable from C (the original-C boot and the replay tool).
 * Display lists go through n64c_dl first, so Fast3D only sees host pointers. Every colour image the
 * game binds (G_SETCIMG, by physical address, other than its depth image) is its own Fast3D
 * framebuffer that keeps its pixels from one task to the next, so a double-buffered game draws into
 * one while the VI shows the other, and the surface it presents can be read back. */
#include <stddef.h>
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
/* The colour image at physical address phys as RGBA8 (red in the low byte, top row first): its top `rows` rows (the
 * ones the VI shows; 0: all) resampled bilinearly to w x h (the VI's scaling). 0 when no run has drawn to it. */
int n64c_f3d_read(N64cF3d *f3d, uint32_t phys, uint32_t *rgba, uint32_t w, uint32_t h, uint32_t rows);
/* Show the colour image at physical address phys on the window, scaled to fill it (the VI's output), and
 * flip. phys 0, or an address no run has drawn, shows black. Returns 0 once the window has been closed. */
int n64c_f3d_present(N64cF3d *f3d, uint32_t phys);
/* n64c_f3d_present with the port's own status text (ASCII, lines split by newlines) drawn over the image on
 * a dark panel at the top left: what the port shows when the original run cannot draw (a boot stop). */
int n64c_f3d_present_text(N64cF3d *f3d, uint32_t phys, const char *text);
/* Frame-rate unlock. The game draws a picture every game frame (a retrace, or every second one); with unlock on the
 * renderer also keeps what the frame's graphics tasks asked for and can draw that scene again at a moment between
 * two game frames, with each matrix moved part of the way from where it was in the frame before to where this frame
 * puts it (Fast3D's matrix replacement). The draws go to their own framebuffers, never to the game's colour images, and
 * read game memory only: nothing the game or the TAS recorder can see changes.
 *   group_begin before the retrace's tasks run; group_end after: returns 0 when no task ran, else 1, plus 2 when the
 *   frame draws the same scene as the one before (so interp_draw works). interp_draw(alpha 0..1) draws the current
 *   frame at alpha of the way from the previous one; 0 when it cannot (the caller shows the VI's picture instead).
 *   interp_present shows that picture and flips; interp_read reads it like n64c_f3d_read; events polls the window. */
void n64c_f3d_unlock(N64cF3d *f3d, int on);
void n64c_f3d_group_begin(N64cF3d *f3d);
int n64c_f3d_group_end(N64cF3d *f3d);
int n64c_f3d_interp_draw(N64cF3d *f3d, float alpha);
int n64c_f3d_interp_present(N64cF3d *f3d);
int n64c_f3d_interp_read(N64cF3d *f3d, uint32_t *rgba, uint32_t w, uint32_t h);
int n64c_f3d_events(N64cF3d *f3d);
/* The options screen (n64c_menu.h): its text, drawn over every picture presented until replaced ("" or NULL: none), with
 * the highlighted line (-1 none) and the largest letter scale; the window mode (N64C_WIN_*) and size; the render scale in
 * percent; the display's sizes, largest first; a request to end the run as if the window were closed. */
void n64c_f3d_overlay(N64cF3d *f3d, const char *text, int selected_line, int max_scale);
void n64c_f3d_window_mode(N64cF3d *f3d, int mode, uint32_t width, uint32_t height);
/* Writes the next presented window picture, overlay included, to path as a PPM (evidence). */
void n64c_f3d_window_shot(N64cF3d *f3d, const char *path);
void n64c_f3d_render_scale(N64cF3d *f3d, int percent);
size_t n64c_f3d_display_sizes(N64cF3d *f3d, uint32_t (*sizes)[2], size_t max);
void n64c_f3d_quit(N64cF3d *f3d);
void n64c_f3d_open_controls(N64cF3d *f3d); /* the Controls page, from the options screen */
/* Pixels (of w x h) where the interpolated draw differs from the game's own picture of the same frame: meaningful at
 * alpha 1 only; -1 when either picture is missing. The refresh rate of the display the window is on (60 when unknown). */
int n64c_f3d_interp_check(N64cF3d *f3d, uint32_t w, uint32_t h, uint32_t *drawn, uint32_t *game);
uint32_t n64c_f3d_refresh_hz(N64cF3d *f3d);
const char *n64c_f3d_renderer(const N64cF3d *f3d);
void n64c_f3d_close(N64cF3d *f3d);
#ifdef __cplusplus
}
#endif
#endif
