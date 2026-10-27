/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Port-wide factory bindings: no game identity or game logic belongs here. */
#ifdef N64C_INPUT_SDL3
#include "n64c_input.h"
#include <string.h>

static N64cBind key(SDL_Scancode code) { return (N64cBind){N64C_BIND_KEY, 0, (short)code}; }
static N64cBind btn(SDL_GamepadButton b) { return (N64cBind){N64C_BIND_PAD_BUTTON, 0, (short)b}; }
static N64cBind axis(SDL_GamepadAxis a, int dir) { return (N64cBind){N64C_BIND_PAD_AXIS, (signed char)dir, (short)a}; }
static void put(N64cBinds *binds, int player, int action, int slot, N64cBind bind)
{
    n64c_binds_set(binds, player, action, slot, bind);
}
void n64c_binds_player_defaults(N64cBinds *binds, int player)
{
    memset(&binds->player[player], 0, sizeof binds->player[player]);
    if (player == 0) { /* the keyboard belongs to player 1 until another player binds keys */
        static const struct { int action, slot; SDL_Scancode code; } keys[] = {
            {N64C_ACT_STICK_LEFT, 0, SDL_SCANCODE_LEFT}, {N64C_ACT_STICK_RIGHT, 0, SDL_SCANCODE_RIGHT},
            {N64C_ACT_STICK_UP, 0, SDL_SCANCODE_UP}, {N64C_ACT_STICK_DOWN, 0, SDL_SCANCODE_DOWN},
            {N64C_ACT_A, 0, SDL_SCANCODE_X}, {N64C_ACT_B, 0, SDL_SCANCODE_C}, {N64C_ACT_Z, 0, SDL_SCANCODE_Z},
            {N64C_ACT_START, 0, SDL_SCANCODE_RETURN}, {N64C_ACT_L, 0, SDL_SCANCODE_A}, {N64C_ACT_R, 0, SDL_SCANCODE_S},
            {N64C_ACT_CUP, 0, SDL_SCANCODE_I}, {N64C_ACT_CDOWN, 0, SDL_SCANCODE_K},
            {N64C_ACT_CLEFT, 0, SDL_SCANCODE_J}, {N64C_ACT_CRIGHT, 0, SDL_SCANCODE_L},
            {N64C_ACT_DUP, 0, SDL_SCANCODE_T}, {N64C_ACT_DDOWN, 0, SDL_SCANCODE_G},
            {N64C_ACT_DLEFT, 0, SDL_SCANCODE_F}, {N64C_ACT_DRIGHT, 0, SDL_SCANCODE_H}};
        for (size_t i = 0; i < sizeof keys / sizeof *keys; ++i)
            put(binds, 0, keys[i].action, keys[i].slot, key(keys[i].code));
    }
    put(binds, player, N64C_ACT_A, 2, btn(SDL_GAMEPAD_BUTTON_SOUTH));
    put(binds, player, N64C_ACT_B, 2, btn(SDL_GAMEPAD_BUTTON_EAST));
    put(binds, player, N64C_ACT_Z, 2, axis(SDL_GAMEPAD_AXIS_LEFT_TRIGGER, 1));
    put(binds, player, N64C_ACT_Z, 3, axis(SDL_GAMEPAD_AXIS_RIGHT_TRIGGER, 1));
    put(binds, player, N64C_ACT_L, 2, btn(SDL_GAMEPAD_BUTTON_LEFT_SHOULDER));
    put(binds, player, N64C_ACT_R, 2, btn(SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER));
    put(binds, player, N64C_ACT_START, 2, btn(SDL_GAMEPAD_BUTTON_START));
    put(binds, player, N64C_ACT_CUP, 2, axis(SDL_GAMEPAD_AXIS_RIGHTY, -1));
    put(binds, player, N64C_ACT_CUP, 3, btn(SDL_GAMEPAD_BUTTON_NORTH));
    put(binds, player, N64C_ACT_CDOWN, 2, axis(SDL_GAMEPAD_AXIS_RIGHTY, 1));
    put(binds, player, N64C_ACT_CLEFT, 2, axis(SDL_GAMEPAD_AXIS_RIGHTX, -1));
    put(binds, player, N64C_ACT_CLEFT, 3, btn(SDL_GAMEPAD_BUTTON_WEST));
    put(binds, player, N64C_ACT_CRIGHT, 2, axis(SDL_GAMEPAD_AXIS_RIGHTX, 1));
    put(binds, player, N64C_ACT_DUP, 2, btn(SDL_GAMEPAD_BUTTON_DPAD_UP));
    put(binds, player, N64C_ACT_DDOWN, 2, btn(SDL_GAMEPAD_BUTTON_DPAD_DOWN));
    put(binds, player, N64C_ACT_DLEFT, 2, btn(SDL_GAMEPAD_BUTTON_DPAD_LEFT));
    put(binds, player, N64C_ACT_DRIGHT, 2, btn(SDL_GAMEPAD_BUTTON_DPAD_RIGHT));
    put(binds, player, N64C_ACT_STICK_UP, 2, axis(SDL_GAMEPAD_AXIS_LEFTY, -1));
    put(binds, player, N64C_ACT_STICK_DOWN, 2, axis(SDL_GAMEPAD_AXIS_LEFTY, 1));
    put(binds, player, N64C_ACT_STICK_LEFT, 2, axis(SDL_GAMEPAD_AXIS_LEFTX, -1));
    put(binds, player, N64C_ACT_STICK_RIGHT, 2, axis(SDL_GAMEPAD_AXIS_LEFTX, 1));
}
void n64c_binds_defaults(N64cBinds *binds)
{
    memset(binds, 0, sizeof *binds);
    binds->deadzone = 8000;
    binds->button_threshold = 16000;
    binds->mouse_sensitivity = 8;
    for (int p = 0; p < N64C_PLAYERS; ++p) n64c_binds_player_defaults(binds, p);
}
#endif
