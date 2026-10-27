/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The port's options screen: a presentation-layer overlay (window mode, resolution or render scale, frame-rate unlock,
 * quit) opened from the game's main and pause menus and driven by the same pad and keyboard. It lives wholly in the
 * host layer: while it is open the game reads a neutral pad; nothing in the guest's code, clock or memory changes.
 * No SDL here: the host reports the display's sizes and applies what the screen asks for (n64c_menu_take). */
#ifndef N64C_MENU_H
#define N64C_MENU_H
#include "n64c_cont.h"
#include <stddef.h>
#ifdef __cplusplus
extern "C" {
#endif
enum { N64C_WIN_WINDOWED, N64C_WIN_BORDERLESS, N64C_WIN_FULLSCREEN };
enum { N64C_MENU_MAIN = 1, N64C_MENU_PAUSE = 2 };                   /* n64c_menu_context: which game menu is up */
enum { N64C_DO_WINDOW = 1, N64C_DO_UNLOCK = 2, N64C_DO_SCALE = 4, N64C_DO_QUIT = 8 }; /* n64c_menu_take */
typedef struct {
    int window_mode;     /* N64C_WIN_* */
    u32 width, height;   /* the window's size (windowed) or the display mode (fullscreen) */
    int fps_unlock;      /* 0/1 */
    int render_scale;    /* percent of the window's pixels the game renders at, 25..100 */
} N64cOptions;

void n64c_options_default(N64cOptions *o);
/* Reads "key=value" lines (window_mode, width, height, fps_unlock, render_scale); keys absent or invalid keep *o.
 * Returns 1 when the file was read. */
int n64c_options_load(const char *path, N64cOptions *o);
/* Writes the file whole (beside it, then renamed). Returns 1 on success. */
int n64c_options_save(const char *path, const N64cOptions *o);

/* Start: the options now in force, the file the screen saves to (NULL: none), the display sizes it offers (largest first;
 * may be empty) and whether the host is a phone (render scale instead of window mode and resolution). */
void n64c_menu_configure(const N64cOptions *now, const char *save_path, const u32 (*sizes)[2], size_t size_count, int mobile);
/* The port reports, every retrace, which game menu is up (N64C_MENU_*; 0 for neither). The screen opens only then. */
void n64c_menu_context(unsigned menus);
/* One input sample (the window owner's cadence): pad as read from the devices, hotkey the keyboard's open key held.
 * Opens on hotkey or L+R inside a game menu; while open the pad is cleared and the keys steer the screen. */
void n64c_menu_filter(OSContPad *pad, int hotkey);
int n64c_menu_is_open(void);
/* The game menu the port last reported (N64C_MENU_*; 0 for neither). */
unsigned n64c_menu_current(void);
/* The text the renderer draws (lines split by newlines) and the highlighted line, or 0 when nothing is shown; maximum
 * letter scale for the panel in *scale. A closed screen inside a game menu shows a one-line hint. */
size_t n64c_menu_text(char *buf, size_t cap, int *selected_line, int *scale);
/* What the screen changed since the last call (N64C_DO_*), with the options in force. */
unsigned n64c_menu_take(N64cOptions *now);
#ifdef __cplusplus
}
#endif
#endif
