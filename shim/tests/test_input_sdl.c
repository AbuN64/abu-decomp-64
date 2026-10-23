/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Real adapter + SI boundary, fake SDL devices. No display or timing dependency. */
#define _POSIX_C_SOURCE 200809L
#include "n64c_input.h"
#include "n64c_internal.h"
#include "n64c_tas.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int failures, checks;
#define CHECK(x) do { ++checks; if (!(x)) { \
    fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #x); ++failures; } } while (0)
struct SDL_Gamepad {
    SDL_JoystickID id;
    bool connected, buttons[SDL_GAMEPAD_BUTTON_COUNT];
    Sint16 axes[SDL_GAMEPAD_AXIS_COUNT];
};
static struct SDL_Gamepad devices[2] = {{.id = 11}, {.id = 22}};
static SDL_Window *window = (SDL_Window *)(uintptr_t)1, *focus;
static bool keyboard[SDL_SCANCODE_COUNT];
static const char *driver = "wayland";
static int opens, closes, inits, quits, pumps, sends;
static bool init_ok = true, open_ok = true;
const char *SDL_GetCurrentVideoDriver(void) { return driver; }
bool SDL_InitSubSystem(SDL_InitFlags flags) { CHECK(flags == SDL_INIT_GAMEPAD); ++inits; return init_ok; }
void SDL_QuitSubSystem(SDL_InitFlags flags) { CHECK(flags == SDL_INIT_GAMEPAD); ++quits; }
SDL_JoystickID *SDL_GetGamepads(int *count)
{
    SDL_JoystickID *ids = calloc(3, sizeof *ids);
    *count = 0;
    for (int i = 0; i < 2; ++i) if (devices[i].connected) ids[(*count)++] = devices[i].id;
    return ids;
}
void SDL_free(void *p) { free(p); }
int n64c_tas_pads(void) { return 1; }
void n64c_tas_poll(OSContPad *pad) { (void)pad; } /* no movie: the live device is the only source */
SDL_Gamepad *SDL_OpenGamepad(SDL_JoystickID id)
{
    ++opens;
    for (int i = 0; open_ok && i < 2; ++i)
        if (devices[i].id == id && devices[i].connected) return &devices[i];
    return NULL;
}
void SDL_CloseGamepad(SDL_Gamepad *p) { CHECK(p != NULL); ++closes; }
SDL_JoystickID SDL_GetGamepadID(SDL_Gamepad *p) { return p->id; }
bool SDL_GamepadConnected(SDL_Gamepad *p) { return p->connected; }
bool SDL_GetGamepadButton(SDL_Gamepad *p, SDL_GamepadButton b) { ++pumps; return p->buttons[b]; }
Sint16 SDL_GetGamepadAxis(SDL_Gamepad *p, SDL_GamepadAxis a) { ++pumps; return p->axes[a]; }
SDL_Window *SDL_GetKeyboardFocus(void) { return focus; }
const bool *SDL_GetKeyboardState(int *count) { ++pumps; *count = SDL_SCANCODE_COUNT; return keyboard; }
void n64c_si_reset(void) {}
void n64c_event_post(u32 event) { (void)event; }
s32 osSendMesg(OSMesgQueue *mq, OSMesg m, s32 flag)
{
    (void)mq; (void)m; CHECK(flag == OS_MESG_NOBLOCK); ++sends; return 0;
}
static OSContPad read_channel(void)
{
    OSContPad pads[MAXCONTROLLERS];
    OSMesgQueue mq = {0};
    CHECK(osContStartReadData(&mq) == 0);
    osContGetReadData(pads);
    CHECK(pads[0].errno_ == 0);
    for (int i = 1; i < MAXCONTROLLERS; ++i) CHECK(pads[i].errno_ == CONT_ERR_NO_CONTROLLER);
    return pads[0];
}
static OSContPad sample(void) { n64c_input_update(); return read_channel(); }
static void event(Uint32 type, SDL_JoystickID id)
{
    SDL_Event e = {0}; e.type = type; e.gdevice.which = id; n64c_input_event(&e);
}
static void check_neutral(OSContPad p) { CHECK(p.button == 0 && p.stick_x == 0 && p.stick_y == 0); }
static void policy(void)
{
    unsetenv("N64C_LOCKSTEP");
    CHECK(n64c_input_start(NULL, NULL));
    driver = "offscreen"; CHECK(n64c_input_start(window, NULL));
    driver = "dummy"; CHECK(n64c_input_start(window, NULL));
    driver = "wayland";
    setenv("N64C_LOCKSTEP", "1", 1); CHECK(n64c_input_start(window, NULL));
    setenv("N64C_LOCKSTEP", "0", 1); CHECK(n64c_input_start(window, NULL));
    n64c_input_update(); event(SDL_EVENT_GAMEPAD_ADDED, 11);
    CHECK(inits == 0 && opens == 0 && pumps == 0);
    check_neutral(read_channel());
    unsetenv("N64C_LOCKSTEP");
    init_ok = false; CHECK(!n64c_input_start(window, NULL));
    CHECK(inits == 1 && quits == 0);
    init_ok = true;
}
static void mapping(void)
{
    CHECK(n64c_input_start(window, NULL)); focus = window;
    OSContStatus status[MAXCONTROLLERS]; u8 bits = 0;
    CHECK(osContInit(NULL, &bits, status) == 0 && bits == 1);
    CHECK(status[0].type == 5 && status[0].errno_ == 0 && status[0].status == 0);
    for (int i = 1; i < MAXCONTROLLERS; ++i) CHECK(status[i].errno_ == CONT_ERR_NO_CONTROLLER);
    /* Independent expected wire bits; a transposed config entry must fail. */
    const struct { SDL_Scancode key; u16 bit; } cases[] = {
        {SDL_SCANCODE_X, 0x8000}, {SDL_SCANCODE_C, 0x4000}, {SDL_SCANCODE_Z, 0x2000},
        {SDL_SCANCODE_RETURN, 0x1000}, {SDL_SCANCODE_A, 0x20}, {SDL_SCANCODE_S, 0x10},
        {SDL_SCANCODE_I, 8}, {SDL_SCANCODE_K, 4}, {SDL_SCANCODE_J, 2}, {SDL_SCANCODE_L, 1}
    };
    for (size_t i = 0; i < sizeof cases / sizeof *cases; ++i) {
        keyboard[cases[i].key] = true; CHECK(sample().button == cases[i].bit);
        keyboard[cases[i].key] = false; check_neutral(sample());
    }
    keyboard[SDL_SCANCODE_LEFT] = true; CHECK(sample().stick_x == -80);
    keyboard[SDL_SCANCODE_RIGHT] = true; CHECK(sample().stick_x == 0);
    keyboard[SDL_SCANCODE_LEFT] = false; CHECK(sample().stick_x == 80);
    keyboard[SDL_SCANCODE_UP] = true; CHECK(sample().stick_y == 80);
    keyboard[SDL_SCANCODE_DOWN] = true; CHECK(sample().stick_y == 0);
    keyboard[SDL_SCANCODE_UP] = false; CHECK(sample().stick_y == -80);
    keyboard[SDL_SCANCODE_X] = true; keyboard[SDL_SCANCODE_RETURN] = true;
    CHECK(sample().button == 0x9000);
    focus = NULL; check_neutral(sample()); focus = window;
    memset(keyboard, 0, sizeof keyboard); check_neutral(sample());
    keyboard[SDL_SCANCODE_X] = true;
    check_neutral(read_channel()); /* SI reads don't pump SDL or change the snapshot. */
    CHECK(sample().button == 0x8000);
    memset(keyboard, 0, sizeof keyboard);
    N64cKeyBinding rebound[] = {{SDL_SCANCODE_F2, 0x4000, 0, 0}};
    N64cInputConfig config = n64c_input_default_config;
    config.keys = rebound; config.key_count = 1;
    CHECK(n64c_input_start(window, &config));
    keyboard[SDL_SCANCODE_X] = true; check_neutral(sample());
    keyboard[SDL_SCANCODE_F2] = true; CHECK(sample().button == 0x4000);
    memset(keyboard, 0, sizeof keyboard);
    n64c_input_stop();
}
static void axes(void)
{
    CHECK(n64c_input_axis(-32768, 8000) == -80);
    CHECK(n64c_input_axis(32767, 8000) == 80);
    CHECK(n64c_input_axis(-8000, 8000) == 0 && n64c_input_axis(8000, 8000) == 0);
    CHECK(n64c_input_axis(-20384, 8000) == -40 && n64c_input_axis(20384, 8000) == 40);
    CHECK(n64c_input_axis(32767, 32767) == 0 && n64c_input_axis(0, -1) == 0);
    int previous = -80;
    for (int raw = -32768; raw <= 32767; ++raw) {
        int value = n64c_input_axis(raw, 8000);
        CHECK(value >= previous && value >= -80 && value <= 80);
        if (raw >= -8000 && raw <= 8000) CHECK(value == 0);
        previous = value;
    }
}
static void hotplug(void)
{
    CHECK(n64c_input_start(window, NULL));
    devices[0].connected = true; event(SDL_EVENT_GAMEPAD_ADDED, 11);
    int opened = opens; event(SDL_EVENT_GAMEPAD_ADDED, 11); CHECK(opens == opened);
    const struct { SDL_GamepadButton button; u16 bit; } cases[] = {
        {SDL_GAMEPAD_BUTTON_SOUTH, 0x8000}, {SDL_GAMEPAD_BUTTON_EAST, 0x4000},
        {SDL_GAMEPAD_BUTTON_WEST, 2}, {SDL_GAMEPAD_BUTTON_NORTH, 8},
        {SDL_GAMEPAD_BUTTON_START, 0x1000}, {SDL_GAMEPAD_BUTTON_LEFT_SHOULDER, 0x20},
        {SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER, 0x10}, {SDL_GAMEPAD_BUTTON_DPAD_UP, 0x800},
        {SDL_GAMEPAD_BUTTON_DPAD_DOWN, 0x400}, {SDL_GAMEPAD_BUTTON_DPAD_LEFT, 0x200},
        {SDL_GAMEPAD_BUTTON_DPAD_RIGHT, 0x100}
    };
    for (size_t i = 0; i < sizeof cases / sizeof *cases; ++i) {
        devices[0].buttons[cases[i].button] = true; CHECK(sample().button == cases[i].bit);
        devices[0].buttons[cases[i].button] = false;
    }
    devices[0].axes[SDL_GAMEPAD_AXIS_LEFTX] = -32768;
    devices[0].axes[SDL_GAMEPAD_AXIS_LEFTY] = -32768;
    OSContPad p = sample(); CHECK(p.stick_x == -80 && p.stick_y == 80);
    devices[0].axes[SDL_GAMEPAD_AXIS_LEFTY] = 32767; CHECK(sample().stick_y == -80);
    keyboard[SDL_SCANCODE_LEFT] = true; CHECK(sample().stick_x == -80);
    keyboard[SDL_SCANCODE_X] = true; devices[0].buttons[SDL_GAMEPAD_BUTTON_EAST] = true;
    CHECK(sample().button == 0xc000);
    memset(keyboard, 0, sizeof keyboard); memset(devices[0].buttons, 0, sizeof devices[0].buttons);
    memset(devices[0].axes, 0, sizeof devices[0].axes);
    const struct { SDL_GamepadAxis axis; int raw; u16 bit; } acases[] = {
        {SDL_GAMEPAD_AXIS_RIGHTX, -16001, 2}, {SDL_GAMEPAD_AXIS_RIGHTX, 16001, 1},
        {SDL_GAMEPAD_AXIS_RIGHTY, -16001, 8}, {SDL_GAMEPAD_AXIS_RIGHTY, 16001, 4},
        {SDL_GAMEPAD_AXIS_LEFT_TRIGGER, 16001, 0x2000},
        {SDL_GAMEPAD_AXIS_RIGHT_TRIGGER, 16001, 0x2000}
    };
    for (size_t i = 0; i < sizeof acases / sizeof *acases; ++i) {
        devices[0].axes[acases[i].axis] = acases[i].raw; CHECK(sample().button == acases[i].bit);
        devices[0].axes[acases[i].axis] = acases[i].raw > 0 ? 16000 : -16000;
        CHECK(sample().button == 0); devices[0].axes[acases[i].axis] = 0;
    }
    devices[1].connected = true; devices[1].buttons[SDL_GAMEPAD_BUTTON_START] = true;
    event(SDL_EVENT_GAMEPAD_ADDED, 22); CHECK(opens == opened);
    devices[0].buttons[SDL_GAMEPAD_BUTTON_SOUTH] = true; CHECK(sample().button == 0x8000);
    devices[0].connected = false; event(SDL_EVENT_GAMEPAD_REMOVED, 11);
    check_neutral(read_channel()); CHECK(sample().button == 0x1000); /* failover */
    int closed = closes; event(SDL_EVENT_GAMEPAD_REMOVED, 11); CHECK(closes == closed);
    devices[1].connected = false; CHECK(sample().button == 0); /* missed remove still clears */
    keyboard[SDL_SCANCODE_X] = true; CHECK(sample().button == 0x8000);
    keyboard[SDL_SCANCODE_X] = false;
    devices[0].connected = true; open_ok = false;
    event(SDL_EVENT_GAMEPAD_ADDED, 11); check_neutral(sample());
    open_ok = true; event(SDL_EVENT_GAMEPAD_ADDED, 11); CHECK(sample().button == 0x8000);
    n64c_cont_reset(NULL); check_neutral(read_channel());
    CHECK(inits - 1 == quits); /* exactly one unsuccessful init */
    devices[0].connected = false;
    CHECK(n64c_input_start(window, NULL));
    keyboard[SDL_SCANCODE_X] = true; CHECK(sample().button == 0x8000);
    n64c_cont_close(); check_neutral(read_channel());
    n64c_input_stop(); CHECK(inits - 1 == quits && sends > 0);
}
int main(void)
{
    n64c_cont_reset(NULL);
    policy(); mapping(); axes(); hotplug();
    printf("input_sdl: %d checks, %d failures (mapping, SI, deadzone, policy, focus, hotplug, lifecycle)\n",
           checks, failures);
    return failures ? 1 : 0;
}
