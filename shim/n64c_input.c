/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Original native SDL3 adapter, using only SDL's public device API. */
#ifdef N64C_INPUT_SDL3
#include "n64c_input.h"
#include <pthread.h>
#include <stdlib.h>
#include <string.h>

static SDL_Window *input_window;
static SDL_Gamepad *gamepad;
static const N64cInputConfig *mapping;
static OSContPad snapshot;
static pthread_mutex_t snapshot_mutex = PTHREAD_MUTEX_INITIALIZER;

/* Integer rescaling preserves zero and both full-scale endpoints exactly. */
int n64c_input_axis(int raw, int deadzone)
{
    if (deadzone < 0 || deadzone >= 32767) return 0;
    if (raw < -32768) raw = -32768;
    if (raw > 32767) raw = 32767;
    int magnitude = raw < 0 ? -raw : raw;
    if (magnitude <= deadzone) return 0;
    int value = (magnitude - deadzone) * 80 / ((raw < 0 ? 32768 : 32767) - deadzone);
    return raw < 0 ? -value : value;
}
static void publish(OSContPad pad)
{
    pthread_mutex_lock(&snapshot_mutex);
    snapshot = pad;
    pthread_mutex_unlock(&snapshot_mutex);
}
static void read_pad(int channel, OSContPad *pad, void *user)
{
    (void)user;
    *pad = (OSContPad){0};
    if (channel != 0) { pad->errno_ = CONT_ERR_NO_CONTROLLER; return; }
    pthread_mutex_lock(&snapshot_mutex);
    *pad = snapshot;
    pthread_mutex_unlock(&snapshot_mutex);
}
static void discover(void)
{
    if (gamepad) return;
    int count = 0;
    SDL_JoystickID *ids = SDL_GetGamepads(&count);
    for (int i = 0; ids && i < count && !gamepad; ++i)
        gamepad = SDL_OpenGamepad(ids[i]);
    SDL_free(ids);
}
void n64c_input_stop(void)
{
    if (!input_window) return;
    n64c_cont_set_input(NULL, NULL);
    if (gamepad) SDL_CloseGamepad(gamepad);
    gamepad = NULL;
    input_window = NULL;
    mapping = NULL;
    publish((OSContPad){0});
    SDL_QuitSubSystem(SDL_INIT_GAMEPAD);
}
int n64c_input_start(SDL_Window *window, const N64cInputConfig *config)
{
    n64c_input_stop();
    const char *driver = SDL_GetCurrentVideoDriver();
    if (!window || getenv("N64C_LOCKSTEP") || !driver ||
        !strcmp(driver, "offscreen") || !strcmp(driver, "dummy")) return 1;
    if (!SDL_InitSubSystem(SDL_INIT_GAMEPAD)) return 0;
    input_window = window;
    mapping = config ? config : &n64c_input_default_config;
    publish((OSContPad){0});
    discover();
    n64c_cont_set_input(read_pad, NULL);
    return 1;
}
void n64c_input_event(const SDL_Event *event)
{
    if (!input_window) return;
    if (event->type == SDL_EVENT_GAMEPAD_REMOVED && gamepad &&
        SDL_GetGamepadID(gamepad) == event->gdevice.which) {
        SDL_CloseGamepad(gamepad);
        gamepad = NULL;
        publish((OSContPad){0});
        discover(); /* promote an already connected second pad */
    } else if (event->type == SDL_EVENT_GAMEPAD_ADDED && !gamepad) {
        gamepad = SDL_OpenGamepad(event->gdevice.which);
    }
}
static int clamp_stick(int value) { return value < -80 ? -80 : value > 80 ? 80 : value; }
void n64c_input_update(void)
{
    if (!input_window) return;
    OSContPad pad = {0};
    int x = 0, y = 0;
    if (gamepad && !SDL_GamepadConnected(gamepad)) {
        SDL_CloseGamepad(gamepad);
        gamepad = NULL;
        discover();
    }
    if (gamepad) {
        x = n64c_input_axis(SDL_GetGamepadAxis(gamepad, SDL_GAMEPAD_AXIS_LEFTX), mapping->deadzone);
        y = -n64c_input_axis(SDL_GetGamepadAxis(gamepad, SDL_GAMEPAD_AXIS_LEFTY), mapping->deadzone);
        for (size_t i = 0; i < mapping->button_count; ++i)
            if (SDL_GetGamepadButton(gamepad, mapping->buttons[i].source))
                pad.button |= mapping->buttons[i].button;
        for (size_t i = 0; i < mapping->axis_count; ++i) {
            const N64cAxisBinding *binding = &mapping->axes[i];
            int value = SDL_GetGamepadAxis(gamepad, binding->source);
            if (value * binding->direction > mapping->button_threshold)
                pad.button |= binding->button;
        }
    }
    if (SDL_GetKeyboardFocus() == input_window) {
        int count = 0;
        const bool *keys = SDL_GetKeyboardState(&count);
        for (size_t i = 0; keys && i < mapping->key_count; ++i) {
            const N64cKeyBinding *binding = &mapping->keys[i];
            if (count > 0 && (unsigned)binding->key < (unsigned)count && keys[binding->key]) {
                pad.button |= binding->button;
                x += binding->x * 80;
                y += binding->y * 80;
            }
        }
    }
    pad.stick_x = (s8)clamp_stick(x);
    pad.stick_y = (s8)clamp_stick(y);
    publish(pad);
}
#endif
