/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The Controls page: rebind every N64 input per player for keyboard, gamepad and mouse. This is the page's
 * behaviour and text; the window draws the lines (fast3d/f3d_controls_overlay.cpp). The options screen opens it
 * with n64c_controls_open(); F10 opens and closes it from anywhere. While it is open the game sees neutral pads. */
#ifndef N64C_CONTROLS_H
#define N64C_CONTROLS_H
#include <SDL3/SDL.h>
#ifdef __cplusplus
extern "C" {
#endif
enum { N64C_CONTROLS_COLUMNS = 40, N64C_CONTROLS_MAX_LINES = 30 };
typedef struct { char text[N64C_CONTROLS_COLUMNS + 1]; } N64cControlsLine;
void n64c_controls_open(void);
void n64c_controls_close(void); /* saves the binds to the config file */
int n64c_controls_active(void);
/* Keys: up/down row, left/right slot, Tab device, 1-4 player, Enter rebind, Delete clear, Esc/F10 close.
 * Pad: d-pad move, A rebind, X clear, B close, Y player, LB/RB device. Returns 1 when the page took the event. */
int n64c_controls_event(const SDL_Event *event);
/* Once per frame: completes a pending rebind. */
void n64c_controls_update(void);
/* The page text, top line first; *cursor_line is the line to highlight (-1 none). Returns the line count. */
int n64c_controls_lines(N64cControlsLine *lines, int max, int *cursor_line);
#ifdef __cplusplus
}
#endif
#endif
