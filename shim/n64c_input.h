/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Hand-written host input adapter. SDL is sampled by the window owner; guest SI
 * reads only the last complete snapshot, never pumps a window from a guest thread. */
#ifndef N64C_INPUT_H
#define N64C_INPUT_H
#include "n64c_cont.h"
#include <SDL3/SDL.h>
#include <stddef.h>
#ifdef __cplusplus
extern "C" {
#endif
enum {
    N64C_PAD_A = 0x8000, N64C_PAD_B = 0x4000, N64C_PAD_Z = 0x2000,
    N64C_PAD_START = 0x1000, N64C_PAD_UP = 0x0800, N64C_PAD_DOWN = 0x0400,
    N64C_PAD_LEFT = 0x0200, N64C_PAD_RIGHT = 0x0100,
    N64C_PAD_L = 0x0020, N64C_PAD_R = 0x0010,
    N64C_PAD_CUP = 0x0008, N64C_PAD_CDOWN = 0x0004,
    N64C_PAD_CLEFT = 0x0002, N64C_PAD_CRIGHT = 0x0001
};
typedef struct {
    SDL_Scancode key;
    u16 button;
    s8 x, y; /* digital stick contributions, -1..1 */
} N64cKeyBinding;
typedef struct {
    SDL_GamepadButton source;
    u16 button;
} N64cButtonBinding;
typedef struct {
    SDL_GamepadAxis source;
    int direction; /* -1 or 1 */
    u16 button;
} N64cAxisBinding;
typedef struct {
    const N64cKeyBinding *keys;
    size_t key_count;
    const N64cButtonBinding *buttons;
    size_t button_count;
    const N64cAxisBinding *axes;
    size_t axis_count;
    int deadzone, button_threshold; /* raw SDL axis units */
} N64cInputConfig;
extern const N64cInputConfig n64c_input_default_config;
/* NULL/offscreen windows and every N64C_LOCKSTEP environment disable live input.
 * config is immutable and must outlive the adapter; NULL selects the defaults. */
int n64c_input_start(SDL_Window *window, const N64cInputConfig *config);
void n64c_input_event(const SDL_Event *event);
void n64c_input_update(void);
void n64c_input_stop(void);
int n64c_input_axis(int raw, int deadzone);
#ifdef __cplusplus
}
#endif
#endif
