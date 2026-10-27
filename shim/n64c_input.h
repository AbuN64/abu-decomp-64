/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Hand-written host input adapter. SDL is sampled by the window owner; guest SI
 * reads only the last complete snapshot, never pumps a window from a guest thread.
 * Four N64 ports: port N reads the Nth gamepad that was plugged in (hot-plug aware) plus whichever keyboard and
 * mouse binds that player holds (n64c_binds.h). */
#ifndef N64C_INPUT_H
#define N64C_INPUT_H
#include "n64c_binds.h"
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
/* Bit of the SI button word for an action; 0 for the four stick directions. */
unsigned n64c_action_button(int action);
/* The live bind table, valid before start and after stop. Edit it with n64c_binds_set(); call
 * n64c_input_binds_save() to keep a change. Only the window thread may touch it. */
N64cBinds *n64c_input_binds(void);
/* Factory bindings applied to the live table (all players or one), saved if a path is set. */
void n64c_input_binds_reset(int player /* -1 = all */);
/* Path of the bind file (config/controls.cfg of the user folder). start loads it; an unreadable file is reported
 * on stderr and the factory table stays. NULL/"" disables persistence. */
void n64c_input_set_config_path(const char *path);
int n64c_input_binds_save(void);
/* Touch zones (n64c_touch.h): edit mode on/off (leaving saves config/touch-layout.cfg, beside the binds). */
int n64c_input_touch_save(void);
void n64c_input_touch_edit(int editing);
/* NULL/offscreen windows and every N64C_LOCKSTEP environment disable live input. */
int n64c_input_start(SDL_Window *window);
void n64c_input_event(const SDL_Event *event);
void n64c_input_update(void);
void n64c_input_stop(void);
int n64c_input_axis(int raw, int deadzone);
/* Gamepad owning an N64 port (NULL name = none) and the port's presence as the guest sees it. */
const char *n64c_input_pad_name(int port);
int n64c_input_port_present(int port);
/* Rebinding: wait for the next input of one device (N64C_DEV_*). Escape cancels. While waiting, and while the
 * Controls page is open, the game sees neutral pads. poll returns 0 waiting, 1 captured (*out set), -1 cancelled
 * or idle. */
int n64c_input_capture_begin(int device);
int n64c_input_capture_poll(N64cBind *out);
void n64c_input_capture_cancel(void);
int n64c_input_capturing(void);
/* Short label of a bind for the Controls page ("X", "LB", "LS RIGHT", "MOUSE 1", "LOOK LEFT"). */
void n64c_bind_label(const N64cBind *bind, char *out, size_t cap);
#ifdef __cplusplus
}
#endif
#endif
