/* SPDX-License-Identifier: GPL-3.0-or-later */
/* On-screen touch controls: round zones laid over the window (coordinates 0..1 across it, origin top-left), one per
 * N64 button and one stick. Pure C, no SDL: n64c_input.c feeds finger events and reads the resulting pad. The
 * layout is editable in place (n64c_touch_set_editing) and kept as text in config/touch-layout.cfg. */
#ifndef N64C_TOUCH_H
#define N64C_TOUCH_H
#include "n64c_binds.h"
#include <stddef.h>
#ifdef __cplusplus
extern "C" {
#endif
enum { N64C_TOUCH_STICK = N64C_ACT_STICK_UP, N64C_TOUCH_ZONES = N64C_TOUCH_STICK + 1, N64C_TOUCH_FINGERS = 10 };
typedef struct { float x, y, r; } N64cTouchZone; /* centre and radius, in window widths */
typedef struct { N64cTouchZone zone[N64C_TOUCH_ZONES]; } N64cTouchLayout;
/* Corner targets that are never zones: top left opens the Controls page, top right (while editing) resets. */
enum { N64C_TOUCH_CORNER_NONE, N64C_TOUCH_CORNER_MENU, N64C_TOUCH_CORNER_RESET };
void n64c_touch_defaults(N64cTouchLayout *layout);
const char *n64c_touch_zone_name(int zone);
size_t n64c_touch_format(const N64cTouchLayout *layout, char *buf, size_t cap); /* bytes needed without the NUL */
int n64c_touch_parse(N64cTouchLayout *layout, const char *text, char *err, size_t errcap); /* 0 leaves *layout unchanged */
int n64c_touch_save(const N64cTouchLayout *layout, const char *path);
int n64c_touch_load(N64cTouchLayout *layout, const char *path, char *err, size_t errcap); /* absent file = 1 */
int n64c_touch_corner(float x, float y);
/* The live layout and finger state. aspect = window width / height (zones are round on screen). */
N64cTouchLayout *n64c_touch_layout(void);
void n64c_touch_set_aspect(float aspect);
void n64c_touch_set_editing(int editing);
int n64c_touch_editing(void);
int n64c_touch_seen(void); /* a finger has touched this window: the zones are drawn and port 1 reads them */
/* Returns the corner a finger went down in (N64C_TOUCH_CORNER_*), else 0. */
int n64c_touch_down(long long id, float x, float y);
void n64c_touch_move(long long id, float x, float y);
void n64c_touch_up(long long id);
void n64c_touch_release_all(void);
/* Play mode: buttons held (N64C_PAD_* bits) and the stick, -80..80. */
void n64c_touch_pad(unsigned *buttons, int *stick_x, int *stick_y);
/* For drawing: zone index being pressed or dragged, as a bit mask. */
unsigned n64c_touch_active_zones(void);
#ifdef __cplusplus
}
#endif
#endif
