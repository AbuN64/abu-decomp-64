/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Per-player controller bindings: the data model behind the Controls page. Pure C, no SDL calls: a bind names a
 * host input by number, n64c_input.c samples it. Each N64 input holds two slots per device (keyboard, gamepad,
 * mouse) for each of the four N64 ports. The text form is the file the port keeps in config/. */
#ifndef N64C_BINDS_H
#define N64C_BINDS_H
#include <stddef.h>
#ifdef __cplusplus
extern "C" {
#endif
typedef enum {
    N64C_ACT_A, N64C_ACT_B, N64C_ACT_Z, N64C_ACT_L, N64C_ACT_R, N64C_ACT_START,
    N64C_ACT_CUP, N64C_ACT_CDOWN, N64C_ACT_CLEFT, N64C_ACT_CRIGHT,
    N64C_ACT_DUP, N64C_ACT_DDOWN, N64C_ACT_DLEFT, N64C_ACT_DRIGHT,
    N64C_ACT_STICK_UP, N64C_ACT_STICK_DOWN, N64C_ACT_STICK_LEFT, N64C_ACT_STICK_RIGHT,
    N64C_ACT_COUNT
} N64cAction;
typedef enum {
    N64C_BIND_NONE, N64C_BIND_KEY /* code = scancode */, N64C_BIND_PAD_BUTTON /* code = button */,
    N64C_BIND_PAD_AXIS /* code = axis, dir = side */, N64C_BIND_MOUSE_BUTTON /* code = button 1.. */,
    N64C_BIND_MOUSE_LOOK /* code = 0 x or 1 y, dir = side */
} N64cBindKind;
enum { N64C_DEV_KEYBOARD, N64C_DEV_GAMEPAD, N64C_DEV_MOUSE, N64C_DEV_COUNT };
enum { N64C_SLOTS_PER_DEVICE = 2, N64C_SLOT_COUNT = N64C_DEV_COUNT * N64C_SLOTS_PER_DEVICE, N64C_PLAYERS = 4 };
typedef struct {
    unsigned char kind;
    signed char dir; /* -1 or 1 for axes and look, else 0 */
    short code;
} N64cBind;
typedef struct { N64cBind slot[N64C_ACT_COUNT][N64C_SLOT_COUNT]; } N64cPlayerBinds;
typedef struct {
    N64cPlayerBinds player[N64C_PLAYERS];
    int deadzone, button_threshold; /* raw SDL axis units */
    int mouse_sensitivity;          /* stick units per pixel of motion in one frame */
} N64cBinds;
/* Slots 0-1 are keyboard, 2-3 gamepad, 4-5 mouse. */
static inline int n64c_bind_slot_device(int slot) { return slot / N64C_SLOTS_PER_DEVICE; }
int n64c_bind_fits(int device, const N64cBind *bind); /* a bind may only sit in a slot of its own device (or be none) */
int n64c_bind_equal(const N64cBind *a, const N64cBind *b);
const char *n64c_action_name(int action);
/* Factory bindings (n64c_input_config.c): player 1 owns the keyboard, players 1-4 their gamepad. */
void n64c_binds_defaults(N64cBinds *binds);
void n64c_binds_player_defaults(N64cBinds *binds, int player);
/* Returns 0 and changes nothing if the arguments are out of range or the bind does not fit the slot. */
int n64c_binds_set(N64cBinds *binds, int player, int action, int slot, N64cBind bind);
/* A bind is in conflict when another action on the same port, or on any port for the shared keyboard and mouse,
 * holds the same bind. Reports the first such holder; returns 0 for none. */
int n64c_binds_conflict(const N64cBinds *binds, int player, int action, int slot, int *other_player,
                        int *other_action);
int n64c_binds_conflict_count(const N64cBinds *binds);
int n64c_binds_player_has_device(const N64cBinds *binds, int player, int device);
int n64c_binds_has_mouse_look(const N64cBinds *binds);
/* Text file form. parse starts from the contents of *binds and overrides what the text names; on a refusal it
 * returns 0, writes "line N: reason" to err and leaves *binds unchanged. */
size_t n64c_binds_format(const N64cBinds *binds, char *buf, size_t cap); /* bytes needed without the NUL */
int n64c_binds_parse(N64cBinds *binds, const char *text, char *err, size_t errcap);
int n64c_binds_save(const N64cBinds *binds, const char *path);                 /* temp file then rename; 1 ok */
int n64c_binds_load(N64cBinds *binds, const char *path, char *err, size_t errcap); /* absent file = 1, untouched */
#ifdef __cplusplus
}
#endif
#endif
