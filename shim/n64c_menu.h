/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The port's options screen: a presentation-layer overlay (window mode, resolution or render scale, frame-rate unlock,
 * quit) opened from the game's main and pause menus and driven by the same pad and keyboard. It lives wholly in the
 * host layer: while it is open the game reads a neutral pad; nothing in the guest's code, clock or memory changes.
 * No SDL here: the host reports the display's sizes and applies what the screen asks for (n64c_menu_take). */
#ifndef N64C_MENU_H
#define N64C_MENU_H
#include "n64c_cont.h"
#include <stdbool.h>
#include <stddef.h>
#ifdef __cplusplus
extern "C" {
#endif
enum { N64C_WIN_WINDOWED, N64C_WIN_BORDERLESS, N64C_WIN_FULLSCREEN };
enum { N64C_MENU_MAIN = 1, N64C_MENU_PAUSE = 2 };                   /* n64c_menu_context: which game menu is up */
enum { N64C_DO_WINDOW = 1, N64C_DO_UNLOCK = 2, N64C_DO_SCALE = 4, N64C_DO_QUIT = 8, N64C_DO_CONTROLS = 16 }; /* n64c_menu_take */
/* The display half of the options: what the host program applies to its window and renderer. */
typedef struct {
    int fullscreen;      /* N64C_WIN_*: 0 windowed, 1 borderless, 2 fullscreen */
    u32 width, height;   /* the window's size (windowed) or the display mode (fullscreen) */
    int render_scale;    /* percent of the window's pixels the game renders at, 25..100 */
} N64cDisplaySettings;
/* Who owns each field (apply / archive):
 *   display.*   applied by the host's controller through n64c_display_apply (boot/frame.h), archived by the host.
 *   fps_unlock  applied natively by the shim (the frame pacer, N64C_DO_UNLOCK) once a request has succeeded, archived by the
 *               host: it travels in every request, so the host's archive must keep it. The framerate card will later own the
 *               full cap/vsync feature. */
typedef struct {
    N64cDisplaySettings display;
    int fps_unlock;      /* 0/1 */
} N64cOptions;

void n64c_options_default(N64cOptions *o);

/* The settings seam. The host program binds a context with two callbacks and owns its config file and its lifecycle: the
 * shim reads and writes no options file (the host saves in its own exit path, after the game's main returns), and
 * without a bound host the screen refuses every change.
 *   load:    at startup, fills *options (already holding the defaults) from the host's config; false when it has none.
 *   request: when the screen asks for a change, with the complete options requested. The host's controller applies the
 *            display part by calling n64c_display_apply (boot/frame.h) exactly once and saves its archive (atomically:
 *            a temp file renamed into place). A request is atomic: true means applied AND saved. If the save fails the
 *            controller restores the display it had (n64c_display_apply with the prior effective settings, a restore and not
 *            a second apply of the request), puts back its working copy and cvars, and returns false. fps_unlock is
 *            applied by the shim only after a true return, so a refusal leaves no mixed state. It returns true after writing into *effective the display settings that really took effect (the window's
 *            size and the render scale the target actually has; *effective arrives holding the request); the screen
 *            shows and keeps those. On failure it returns false after writing a message into error (capacity bytes,
 *            supplied by the shim) and leaves the prior display unchanged: the screen shows the message and keeps the
 *            options it had. The screen never applies a second time and n64c_display_apply never calls back into it.
 *            Called from the thread that calls n64c_menu_take, with no shim lock held.
 * The context is borrowed: the host keeps it alive from bind until n64c_settings_unbind.
 * The registration is the host's and outlives n64c_game_main: the host binds before calling it, the game's boot binds its
 * own flags-only host only when none is bound, and only n64c_settings_unbind removes a binding (the one who bound it
 * unbinds it). There is one public name for it, this one; no other store is consulted. */
typedef bool (*N64cSettingsLoadFn)(void *context, N64cOptions *options);
typedef bool (*N64cSettingsRequestFn)(void *context, const N64cOptions *requested, N64cDisplaySettings *effective, char *error,
                                      size_t capacity);
/* Binds the host. False, with nothing changed, when a callback is missing or a host is already bound: a binding is never
 * replaced, it is removed by n64c_settings_unbind first. */
bool n64c_settings_bind(void *context, N64cSettingsLoadFn load, N64cSettingsRequestFn request);
/* Removes the binding (none: no effect). Afterwards n64c_settings_load is false and the screen refuses every change. */
void n64c_settings_unbind(void);
/* The starting options: *o as the bound load callback leaves it. False when nothing is bound or it has no config. */
bool n64c_settings_load(N64cOptions *o);

/* Start: the options now in force, the display sizes the screen offers (largest first; may be empty) and whether the host
 * is a phone (render scale instead of window mode and resolution). */
void n64c_menu_configure(const N64cOptions *now, const u32 (*sizes)[2], size_t size_count, int mobile);
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
/* What the screen changed since the last call (N64C_DO_*), with the options in force. A change is applied here (the host's
 * request) and only once that succeeds is it in force; a failed one is dropped from the result and its message
 * is shown on the screen. */
unsigned n64c_menu_take(N64cOptions *now);
#ifdef __cplusplus
}
#endif
#endif
