/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Real adapter + SI boundary, fake SDL devices. No display or timing dependency. */
#define _POSIX_C_SOURCE 200809L
#include "n64c_controls.h"
#include "n64c_input.h"
#include "n64c_touch.h"
#include "n64c_internal.h"
#include "n64c_tas.h"
#include <math.h>
#include <stdio.h>
#include <unistd.h>
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
static struct SDL_Gamepad devices[5] = {{.id = 11}, {.id = 22}, {.id = 33}, {.id = 44}, {.id = 55}};
static SDL_Window *window = (SDL_Window *)(uintptr_t)1, *focus;
static bool keyboard[SDL_SCANCODE_COUNT];
static const char *driver = "wayland";
static Uint32 mouse_mask;
static int relative_calls;
static bool relative_on;
static int opens, closes, inits, quits, pumps, sends;
static bool init_ok = true, open_ok = true;
const char *SDL_GetCurrentVideoDriver(void) { return driver; }
bool SDL_InitSubSystem(SDL_InitFlags flags) { CHECK(flags == SDL_INIT_GAMEPAD); ++inits; return init_ok; }
void SDL_QuitSubSystem(SDL_InitFlags flags) { CHECK(flags == SDL_INIT_GAMEPAD); ++quits; }
struct SDL_Joystick { int device; };
static struct SDL_Joystick joysticks[5];
static int virtual_attached;
SDL_JoystickID SDL_AttachVirtualJoystick(const SDL_VirtualJoystickDesc *desc)
{
    CHECK(desc->type == SDL_JOYSTICK_TYPE_GAMEPAD && desc->nbuttons == SDL_GAMEPAD_BUTTON_COUNT);
    devices[virtual_attached].connected = true;
    return devices[virtual_attached++].id;
}
SDL_Joystick *SDL_OpenJoystick(SDL_JoystickID id)
{
    for (int i = 0; i < 5; ++i) if (devices[i].id == id) { joysticks[i].device = i; return &joysticks[i]; }
    return NULL;
}
SDL_JoystickID SDL_GetJoystickID(SDL_Joystick *j) { return devices[j->device].id; }
void SDL_CloseJoystick(SDL_Joystick *j) { (void)j; }
bool SDL_DetachVirtualJoystick(SDL_JoystickID id)
{
    for (int i = 0; i < 5; ++i) if (devices[i].id == id) devices[i].connected = false;
    return true;
}
bool SDL_SetJoystickVirtualButton(SDL_Joystick *j, int button, bool down) { devices[j->device].buttons[button] = down; return true; }
SDL_GamepadButton SDL_GetGamepadButtonFromString(const char *s)
{
    return !strcmp(s, "a") ? SDL_GAMEPAD_BUTTON_SOUTH : !strcmp(s, "b") ? SDL_GAMEPAD_BUTTON_EAST : SDL_GAMEPAD_BUTTON_INVALID;
}
SDL_JoystickID *SDL_GetGamepads(int *count)
{
    SDL_JoystickID *ids = calloc(6, sizeof *ids);
    *count = 0;
    for (int i = 0; i < 5; ++i) if (devices[i].connected) ids[(*count)++] = devices[i].id;
    return ids;
}
void SDL_free(void *p) { free(p); }
int n64c_tas_pads(void) { return 1; }
void n64c_tas_poll(OSContPad *pad) { (void)pad; } /* no movie: the live device is the only source */
SDL_Gamepad *SDL_OpenGamepad(SDL_JoystickID id)
{
    ++opens;
    for (int i = 0; open_ok && i < 5; ++i)
        if (devices[i].id == id && devices[i].connected) return &devices[i];
    return NULL;
}
void SDL_CloseGamepad(SDL_Gamepad *p) { CHECK(p != NULL); ++closes; }
SDL_JoystickID SDL_GetGamepadID(SDL_Gamepad *p) { return p->id; }
bool SDL_GamepadConnected(SDL_Gamepad *p) { return p->connected; }
bool SDL_GetGamepadButton(SDL_Gamepad *p, SDL_GamepadButton b) { ++pumps; return p->buttons[b]; }
Sint16 SDL_GetGamepadAxis(SDL_Gamepad *p, SDL_GamepadAxis a) { ++pumps; return p->axes[a]; }
Uint32 SDL_GetMouseState(float *x, float *y) { (void)x; (void)y; return mouse_mask; }
bool SDL_SetHint(const char *name, const char *value) { (void)name; (void)value; return true; }
bool SDL_GetWindowSize(SDL_Window *w, int *wd, int *ht) { (void)w; *wd = 1600; *ht = 900; return true; }
bool SDL_SetWindowRelativeMouseMode(SDL_Window *w, bool on) { (void)w; ++relative_calls; relative_on = on; return true; }
const char *SDL_GetGamepadName(SDL_Gamepad *p) { return p->id == 11 ? "Pad Eleven" : "Pad"; }
const char *SDL_GetScancodeName(SDL_Scancode c)
{
    static char name[8][16]; static int next;
    char *n = name[next++ & 7]; snprintf(n, 16, "K%d", (int)c); return n;
}
SDL_Window *SDL_GetKeyboardFocus(void) { return focus; }
const bool *SDL_GetKeyboardState(int *count) { ++pumps; *count = SDL_SCANCODE_COUNT; return keyboard; }
void n64c_si_reset(void) {}
void n64c_event_post(u32 event) { (void)event; }
s32 osSendMesg(OSMesgQueue *mq, OSMesg m, s32 flag)
{
    (void)mq; (void)m; CHECK(flag == OS_MESG_NOBLOCK); ++sends; return 0;
}
static OSContPad read_port(int port, int *errno_out)
{
    OSContPad pads[MAXCONTROLLERS];
    OSMesgQueue mq = {0};
    CHECK(osContStartReadData(&mq) == 0);
    osContGetReadData(pads);
    if (errno_out) *errno_out = pads[port].errno_;
    return pads[port];
}
static OSContPad read_channel(void)
{
    int e; OSContPad p = read_port(0, &e);
    CHECK(e == 0);
    return p;
}
static OSContPad sample(void) { n64c_input_update(); return read_channel(); }
static OSContPad sample_port(int port) { n64c_input_update(); return read_port(port, NULL); }
static int port_errno(int port) { int e; read_port(port, &e); return e; }
static u8 present_bits(void)
{
    u8 bits = 0; OSContStatus status[MAXCONTROLLERS];
    CHECK(osContInit(NULL, &bits, status) == 0);
    for (int i = 0; i < MAXCONTROLLERS; ++i) CHECK((status[i].errno_ == 0) == ((bits >> i) & 1));
    return bits;
}
static void event(Uint32 type, SDL_JoystickID id)
{
    SDL_Event e = {0}; e.type = type; e.gdevice.which = id; n64c_input_event(&e);
}
static void key_event(SDL_Scancode code, bool repeat)
{
    SDL_Event e = {0}; e.type = SDL_EVENT_KEY_DOWN; e.key.scancode = code; e.key.repeat = repeat; n64c_input_event(&e);
}
static void pad_button_event(SDL_GamepadButton b)
{
    SDL_Event e = {0}; e.type = SDL_EVENT_GAMEPAD_BUTTON_DOWN; e.gbutton.button = b; n64c_input_event(&e);
}
static void mouse_motion(float dx, float dy)
{
    SDL_Event e = {0}; e.type = SDL_EVENT_MOUSE_MOTION; e.motion.xrel = dx; e.motion.yrel = dy; n64c_input_event(&e);
}

static void check_neutral(OSContPad p) { CHECK(p.button == 0 && p.stick_x == 0 && p.stick_y == 0); }
static void clear_devices(void)
{
    memset(keyboard, 0, sizeof keyboard);
    for (int i = 0; i < 5; ++i) { devices[i].connected = false; memset(devices[i].buttons, 0, sizeof devices[i].buttons);
                                  memset(devices[i].axes, 0, sizeof devices[i].axes); }
    mouse_mask = 0;
}
static void fresh(void)
{
    n64c_input_stop(); clear_devices(); focus = window;
    n64c_input_set_config_path(NULL);
    n64c_binds_defaults(n64c_input_binds());
}
static void policy(void)
{
    unsetenv("N64C_LOCKSTEP");
    CHECK(n64c_input_start(NULL));
    driver = "offscreen"; CHECK(n64c_input_start(window));
    driver = "dummy"; CHECK(n64c_input_start(window));
    driver = "wayland";
    setenv("N64C_LOCKSTEP", "1", 1); CHECK(n64c_input_start(window));
    setenv("N64C_LOCKSTEP", "0", 1); CHECK(n64c_input_start(window));
    n64c_input_update(); event(SDL_EVENT_GAMEPAD_ADDED, 11);
    CHECK(inits == 0 && opens == 0 && pumps == 0);
    check_neutral(read_channel());
    unsetenv("N64C_LOCKSTEP");
    init_ok = false; CHECK(!n64c_input_start(window));
    CHECK(inits == 1 && quits == 0);
    init_ok = true;
}
static void mapping(void)
{
    fresh();
    CHECK(n64c_input_start(window));
    OSContStatus status[MAXCONTROLLERS]; u8 bits = 0;
    CHECK(osContInit(NULL, &bits, status) == 0 && bits == 1);
    CHECK(status[0].type == 5 && status[0].errno_ == 0 && status[0].status == 0);
    for (int i = 1; i < MAXCONTROLLERS; ++i) CHECK(status[i].errno_ == CONT_ERR_NO_CONTROLLER && port_errno(i));
    /* Independent expected wire bits; a transposed default must fail. */
    const struct { SDL_Scancode key; u16 bit; } cases[] = {
        {SDL_SCANCODE_X, 0x8000}, {SDL_SCANCODE_C, 0x4000}, {SDL_SCANCODE_Z, 0x2000},
        {SDL_SCANCODE_RETURN, 0x1000}, {SDL_SCANCODE_A, 0x20}, {SDL_SCANCODE_S, 0x10},
        {SDL_SCANCODE_I, 8}, {SDL_SCANCODE_K, 4}, {SDL_SCANCODE_J, 2}, {SDL_SCANCODE_L, 1},
        {SDL_SCANCODE_T, 0x800}, {SDL_SCANCODE_G, 0x400}, {SDL_SCANCODE_F, 0x200}, {SDL_SCANCODE_H, 0x100}
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
    /* A rebind takes the old key away and gives the new one the action. */
    N64cBinds *b = n64c_input_binds();
    CHECK(n64c_binds_set(b, 0, N64C_ACT_B, 0, (N64cBind){N64C_BIND_KEY, 0, SDL_SCANCODE_F2}));
    keyboard[SDL_SCANCODE_C] = true; check_neutral(sample());
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
static void pad_defaults(void)
{
    fresh();
    CHECK(n64c_input_start(window));
    devices[0].connected = true; event(SDL_EVENT_GAMEPAD_ADDED, 11);
    int opened = opens; event(SDL_EVENT_GAMEPAD_ADDED, 11); CHECK(opens == opened);
    CHECK(!strcmp(n64c_input_pad_name(0), "Pad Eleven") && !n64c_input_pad_name(1));
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
    n64c_input_stop();
}
/* One gamepad per N64 port, plugged and unplugged mid-run. */
static void four_ports(void)
{
    fresh();
    CHECK(n64c_input_start(window));
    CHECK(present_bits() == 1);
    for (int i = 0; i < 4; ++i) { devices[i].connected = true; event(SDL_EVENT_GAMEPAD_ADDED, devices[i].id); }
    CHECK(present_bits() == 0xF);
    for (int i = 0; i < 4; ++i) devices[i].buttons[SDL_GAMEPAD_BUTTON_SOUTH + (i & 1)] = true; /* A,B,A,B */
    devices[2].axes[SDL_GAMEPAD_AXIS_LEFTX] = 32767;
    n64c_input_update();
    CHECK(read_port(0, NULL).button == 0x8000 && read_port(1, NULL).button == 0x4000);
    CHECK(read_port(2, NULL).button == 0x8000 && read_port(2, NULL).stick_x == 80 && read_port(3, NULL).button == 0x4000);
    CHECK(read_port(0, NULL).stick_x == 0 && read_port(1, NULL).stick_x == 0);
    for (int i = 0; i < 5; ++i) { memset(devices[i].buttons, 0, sizeof devices[i].buttons); memset(devices[i].axes, 0, sizeof devices[i].axes); }
    /* Unplug player 2's pad: its port reports no controller on the next read, the others are untouched. */
    devices[1].connected = false; event(SDL_EVENT_GAMEPAD_REMOVED, 22);
    CHECK(port_errno(1) == CONT_ERR_NO_CONTROLLER && port_errno(0) == 0 && port_errno(2) == 0 && port_errno(3) == 0);
    CHECK(present_bits() == 0xD);
    int closed = closes; event(SDL_EVENT_GAMEPAD_REMOVED, 22); CHECK(closes == closed);
    /* A fifth pad waits for a free port; the new pad and the waiting one fill ports lowest first. */
    devices[4].connected = true; event(SDL_EVENT_GAMEPAD_ADDED, 55);
    CHECK(present_bits() == 0xF && !strcmp(n64c_input_pad_name(1), "Pad"));
    devices[4].buttons[SDL_GAMEPAD_BUTTON_START] = true;
    CHECK(sample_port(1).button == 0x1000);
    devices[4].buttons[SDL_GAMEPAD_BUTTON_START] = false;
    devices[1].connected = true; event(SDL_EVENT_GAMEPAD_ADDED, 22); CHECK(present_bits() == 0xF); /* all four taken */
    devices[2].connected = false; event(SDL_EVENT_GAMEPAD_REMOVED, 33);
    CHECK(present_bits() == 0xF); /* player 3 gets the waiting pad 22 */
    devices[1].buttons[SDL_GAMEPAD_BUTTON_START] = true; CHECK(sample_port(2).button == 0x1000);
    /* A missed remove event is caught by the connected check. */
    devices[3].connected = false; n64c_input_update(); CHECK(port_errno(3) == CONT_ERR_NO_CONTROLLER);
    /* Keyboard binds make another port present without a pad, and the pad does not leak into it. */
    CHECK(n64c_binds_set(n64c_input_binds(), 3, N64C_ACT_A, 0, (N64cBind){N64C_BIND_KEY, 0, SDL_SCANCODE_F5}));
    keyboard[SDL_SCANCODE_F5] = true;
    CHECK(sample_port(3).button == 0x8000 && port_errno(3) == 0 && sample_port(0).button == 0);
    devices[0].connected = false; open_ok = false;
    event(SDL_EVENT_GAMEPAD_REMOVED, 11); event(SDL_EVENT_GAMEPAD_ADDED, 11);
    CHECK(port_errno(0) == 0); /* player 1 stays present on the keyboard */
    open_ok = true;
    n64c_cont_reset(NULL); check_neutral(read_channel());
    n64c_input_stop();
}
static void mouse(void)
{
    fresh();
    N64cBinds *b = n64c_input_binds();
    CHECK(!n64c_binds_set(b, 0, N64C_ACT_A, 4, (N64cBind){N64C_BIND_KEY, 0, 4})); /* a key does not fit a mouse slot */
    CHECK(n64c_binds_set(b, 0, N64C_ACT_STICK_LEFT, 4, (N64cBind){N64C_BIND_MOUSE_LOOK, -1, 0}));
    CHECK(n64c_binds_set(b, 0, N64C_ACT_STICK_RIGHT, 4, (N64cBind){N64C_BIND_MOUSE_LOOK, 1, 0}));
    CHECK(n64c_binds_set(b, 0, N64C_ACT_A, 4, (N64cBind){N64C_BIND_MOUSE_BUTTON, 0, 1}));
    CHECK(n64c_binds_set(b, 0, N64C_ACT_CUP, 5, (N64cBind){N64C_BIND_MOUSE_LOOK, -1, 1}));
    CHECK(n64c_input_start(window));
    int calls = relative_calls;
    n64c_input_update(); CHECK(relative_on && relative_calls == calls + 1); /* look bound: the pointer is grabbed */
    n64c_input_update(); CHECK(relative_calls == calls + 1);
    mouse_motion(-3, 0); OSContPad p = sample(); CHECK(p.stick_x == -24 && p.stick_y == 0);
    CHECK(sample().stick_x == 0); /* motion is consumed once per frame */
    mouse_motion(100, 0); CHECK(sample().stick_x == 80);
    mouse_motion(0, -2); CHECK(sample().button == 8);
    mouse_mask = SDL_BUTTON_MASK(1); CHECK(sample().button == 0x8000);
    mouse_mask = SDL_BUTTON_MASK(3); check_neutral(sample());
    mouse_mask = 0;
    focus = NULL; mouse_motion(5, 0); check_neutral(sample());
    CHECK(!relative_on); /* focus lost releases the pointer, regained grabs it */
    focus = window; n64c_input_update(); CHECK(relative_on);
    n64c_input_stop(); CHECK(!relative_on);
}
static void rebinding(void)
{
    fresh();
    CHECK(n64c_input_start(window));
    N64cBind got = {0};
    CHECK(!n64c_input_capture_begin(N64C_DEV_COUNT) && n64c_input_capture_poll(&got) == -1);
    CHECK(n64c_input_capture_begin(N64C_DEV_KEYBOARD) && n64c_input_capturing() && n64c_input_capture_poll(&got) == 0);
    keyboard[SDL_SCANCODE_X] = true; check_neutral(sample()); /* neutral while the page waits for a key */
    keyboard[SDL_SCANCODE_X] = false;
    pad_button_event(SDL_GAMEPAD_BUTTON_SOUTH); mouse_motion(99, 0); /* other devices are ignored */
    key_event(SDL_SCANCODE_Q, true); CHECK(n64c_input_capture_poll(&got) == 0); /* auto-repeat is not a press */
    key_event(SDL_SCANCODE_Q, false);
    CHECK(n64c_input_capture_poll(&got) == 1 && got.kind == N64C_BIND_KEY && got.code == SDL_SCANCODE_Q);
    CHECK(n64c_input_capture_poll(&got) == -1 && !n64c_input_capturing());
    CHECK(n64c_input_capture_begin(N64C_DEV_KEYBOARD)); key_event(SDL_SCANCODE_ESCAPE, false);
    CHECK(n64c_input_capture_poll(&got) == -1);
    CHECK(n64c_input_capture_begin(N64C_DEV_GAMEPAD)); key_event(SDL_SCANCODE_Q, false);
    pad_button_event(SDL_GAMEPAD_BUTTON_WEST);
    CHECK(n64c_input_capture_poll(&got) == 1 && got.kind == N64C_BIND_PAD_BUTTON && got.code == SDL_GAMEPAD_BUTTON_WEST);
    CHECK(n64c_input_capture_begin(N64C_DEV_GAMEPAD));
    SDL_Event axis = {0}; axis.type = SDL_EVENT_GAMEPAD_AXIS_MOTION; axis.gaxis.axis = SDL_GAMEPAD_AXIS_LEFTY;
    axis.gaxis.value = 15000; n64c_input_event(&axis); CHECK(n64c_input_capture_poll(&got) == 0); /* below threshold */
    axis.gaxis.value = -20000; n64c_input_event(&axis);
    CHECK(n64c_input_capture_poll(&got) == 1 && got.kind == N64C_BIND_PAD_AXIS && got.code == SDL_GAMEPAD_AXIS_LEFTY && got.dir == -1);
    CHECK(n64c_input_capture_begin(N64C_DEV_MOUSE));
    mouse_motion(10, 5); mouse_motion(8, 20); CHECK(n64c_input_capture_poll(&got) == 0 || 1); /* 18 px: not yet */
    CHECK(n64c_input_capture_begin(N64C_DEV_MOUSE)); mouse_motion(2, -30);
    CHECK(n64c_input_capture_poll(&got) == 1 && got.kind == N64C_BIND_MOUSE_LOOK && got.code == 1 && got.dir == -1);
    CHECK(n64c_input_capture_begin(N64C_DEV_MOUSE));
    SDL_Event click = {0}; click.type = SDL_EVENT_MOUSE_BUTTON_DOWN; click.button.button = 3; n64c_input_event(&click);
    CHECK(n64c_input_capture_poll(&got) == 1 && got.kind == N64C_BIND_MOUSE_BUTTON && got.code == 3);
    n64c_input_stop();
}
static int page_has(const char *text)
{
    N64cControlsLine lines[N64C_CONTROLS_MAX_LINES];
    int n = n64c_controls_lines(lines, N64C_CONTROLS_MAX_LINES, NULL);
    for (int i = 0; i < n; ++i) if (strstr(lines[i].text, text)) return 1;
    return 0;
}
static char config_file[] = "/tmp/n64c_controls_test.cfg";
static void page(void)
{
    fresh();
    remove(config_file);
    n64c_input_set_config_path(config_file);
    CHECK(n64c_input_start(window));
    CHECK(!n64c_controls_active());
    key_event(SDL_SCANCODE_RETURN, false); CHECK(!n64c_controls_active());
    keyboard[SDL_SCANCODE_X] = true; CHECK(sample().button == 0x8000);
    key_event(SDL_SCANCODE_F10, false); CHECK(n64c_controls_active());
    check_neutral(sample()); /* the game sees neutral pads under the page */
    CHECK(page_has("PLAYER 1 OF 4") && page_has("[KEYBOARD]") && page_has("STICK-RIGHT") && page_has("MOUSE"));
    /* Rebind P1 B (row 1, keyboard slot 1) to Q. */
    key_event(SDL_SCANCODE_DOWN, false); key_event(SDL_SCANCODE_RIGHT, false);
    key_event(SDL_SCANCODE_RETURN, false); CHECK(n64c_input_capturing() && page_has("PRESS AN INPUT"));
    key_event(SDL_SCANCODE_Q, false); n64c_input_update();
    const N64cBind *slot = &n64c_input_binds()->player[0].slot[N64C_ACT_B][1];
    CHECK(slot->kind == N64C_BIND_KEY && slot->code == SDL_SCANCODE_Q && page_has("[K20"));
    FILE *f = fopen(config_file, "rb"); CHECK(f != NULL); if (f) fclose(f); /* saved at once */
    /* Binding the same key to A: both rows show the conflict and the footer names the other holder. */
    key_event(SDL_SCANCODE_UP, false); key_event(SDL_SCANCODE_LEFT, false);
    key_event(SDL_SCANCODE_RETURN, false); key_event(SDL_SCANCODE_Q, false); n64c_input_update();
    CHECK(page_has("CONFLICT: ALSO P1 B") && page_has("2 BINDS IN CONFLICT"));
    CHECK(n64c_binds_conflict_count(n64c_input_binds()) == 2);
    /* Clear the second one: the conflict is gone. */
    key_event(SDL_SCANCODE_DELETE, false); key_event(SDL_SCANCODE_DOWN, false); key_event(SDL_SCANCODE_RIGHT, false);
    key_event(SDL_SCANCODE_DELETE, false);
    CHECK(n64c_binds_conflict_count(n64c_input_binds()) == 0 && !page_has("IN CONFLICT"));
    /* Player 2 and the gamepad tab; the same key on two ports is a conflict across the shared keyboard. */
    key_event(SDL_SCANCODE_2, false); CHECK(page_has("PLAYER 2 OF 4") && page_has("NONE PLUGGED IN"));
    key_event(SDL_SCANCODE_TAB, false); CHECK(page_has("[GAMEPAD]") && page_has("START"));
    pad_button_event(SDL_GAMEPAD_BUTTON_NORTH); CHECK(page_has("PLAYER 3 OF 4")); /* pad Y moves on to player 3 */
    pad_button_event(SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER); CHECK(page_has("[MOUSE]"));
    /* Reset needs a second confirmation. */
    key_event(SDL_SCANCODE_1, false);
    for (int i = 0; i < 20 && !page_has(">RESET THIS PLAYER"); ++i) key_event(SDL_SCANCODE_DOWN, false);
    CHECK(page_has(">RESET THIS PLAYER"));
    CHECK(n64c_binds_set(n64c_input_binds(), 0, N64C_ACT_B, 1, (N64cBind){N64C_BIND_KEY, 0, SDL_SCANCODE_Q}));
    key_event(SDL_SCANCODE_RETURN, false);
    CHECK(page_has("ENTER AGAIN") && n64c_input_binds()->player[0].slot[N64C_ACT_B][1].kind == N64C_BIND_KEY);
    key_event(SDL_SCANCODE_RETURN, false);
    CHECK(page_has("PLAYER RESET") && n64c_input_binds()->player[0].slot[N64C_ACT_B][1].kind == N64C_BIND_NONE);
    /* Close with Escape, saved; a fresh start restores a rebind from the file. */
    CHECK(n64c_binds_set(n64c_input_binds(), 1, N64C_ACT_START, 0, (N64cBind){N64C_BIND_KEY, 0, SDL_SCANCODE_F7}));
    key_event(SDL_SCANCODE_ESCAPE, false); CHECK(!n64c_controls_active());
    n64c_input_stop();
    n64c_binds_defaults(n64c_input_binds());
    CHECK(n64c_input_binds()->player[1].slot[N64C_ACT_START][0].kind == N64C_BIND_NONE);
    CHECK(n64c_input_start(window));
    CHECK(n64c_input_binds()->player[1].slot[N64C_ACT_START][0].code == SDL_SCANCODE_F7);
    keyboard[SDL_SCANCODE_F7] = true; CHECK(sample_port(1).button == 0x1000);
    /* A broken file is refused whole: the controls stay as they were. */
    n64c_input_stop();
    f = fopen(config_file, "wb"); fputs("[player1]\nA = key:7\n", f); fclose(f);
    n64c_binds_defaults(n64c_input_binds());
    CHECK(n64c_input_start(window));
    CHECK(n64c_input_binds()->player[0].slot[N64C_ACT_A][0].code == SDL_SCANCODE_X);
    n64c_input_stop();
    remove(config_file);
}
static void binds_model(void)
{
    N64cBinds a, b;
    n64c_binds_defaults(&a); b = a;
    CHECK(n64c_binds_conflict_count(&a) == 0);
    /* every default of every player fits its slot, and no two players share a pad bind */
    for (int p = 0; p < N64C_PLAYERS; ++p) for (int s = 0; s < N64C_ACT_COUNT; ++s)
        for (int k = 0; k < N64C_SLOT_COUNT; ++k) CHECK(n64c_bind_fits(n64c_bind_slot_device(k), &a.player[p].slot[s][k]));
    CHECK(!n64c_binds_set(&a, 4, 0, 0, (N64cBind){0}) && !n64c_binds_set(&a, 0, N64C_ACT_COUNT, 0, (N64cBind){0}) &&
          !n64c_binds_set(&a, 0, 0, N64C_SLOT_COUNT, (N64cBind){0}));
    CHECK(!n64c_binds_set(&a, 0, 0, 2, (N64cBind){N64C_BIND_PAD_AXIS, 0, 1})); /* an axis needs a side */
    CHECK(n64c_binds_set(&a, 2, N64C_ACT_Z, 0, (N64cBind){N64C_BIND_KEY, 0, SDL_SCANCODE_X})); /* P3 steals X from P1 */
    int op = -1, oa = -1;
    CHECK(n64c_binds_conflict(&a, 2, N64C_ACT_Z, 0, &op, &oa) && op == 0 && oa == N64C_ACT_A);
    CHECK(n64c_binds_conflict(&a, 0, N64C_ACT_A, 0, &op, &oa) && op == 2 && oa == N64C_ACT_Z);
    /* the same pad button on two players is no conflict, on one player it is */
    CHECK(n64c_binds_set(&a, 1, N64C_ACT_B, 2, (N64cBind){N64C_BIND_PAD_BUTTON, 0, SDL_GAMEPAD_BUTTON_SOUTH}));
    CHECK(n64c_binds_conflict(&a, 1, N64C_ACT_B, 2, &op, &oa) && op == 1 && oa == N64C_ACT_A);
    CHECK(n64c_binds_conflict_count(&a) == 4);
    /* text round trip is exact */
    char text[16384], err[128];
    size_t need = n64c_binds_format(&a, text, sizeof text);
    CHECK(need > 1000 && need < sizeof text && strlen(text) == need);
    CHECK(n64c_binds_format(&a, NULL, 0) == need);
    CHECK(n64c_binds_parse(&b, text, err, sizeof err) && !memcmp(&a, &b, sizeof a));
    /* refusals name the line and change nothing */
    const char *bad[] = {"[player9]\n", "[player1]\nQ = none none none none none none\n", "[player1]\nA = key:4\n",
        "[player1]\nA = btn:1 none none none none none\n", "[player1]\nA = key:4 none none none none none none\n",
        "deadzone = x\n", "[player1]\nA = axis:1 none none none none none none\n", "A = none\n", "[player1]\nA key\n"};
    for (size_t i = 0; i < sizeof bad / sizeof *bad; ++i) {
        N64cBinds c = a; err[0] = 0;
        CHECK(!n64c_binds_parse(&c, bad[i], err, sizeof err) && !memcmp(&c, &a, sizeof a) && !strncmp(err, "line ", 5));
    }
    N64cBinds d; n64c_binds_defaults(&d); CHECK(n64c_binds_parse(&d, "# only this\n[player2]\nB = key:9 none none none none none\n", err, sizeof err));
    CHECK(d.player[1].slot[N64C_ACT_B][0].code == 9 && d.player[0].slot[N64C_ACT_A][0].code == SDL_SCANCODE_X);
    CHECK(n64c_binds_save(&a, "/tmp/n64c_binds_model.cfg") && n64c_binds_load(&d, "/tmp/n64c_binds_model.cfg", err, sizeof err) &&
          !memcmp(&a, &d, sizeof a));
    remove("/tmp/n64c_binds_model.cfg");
    CHECK(n64c_binds_load(&d, "/tmp/n64c_binds_absent.cfg", err, sizeof err) && !memcmp(&a, &d, sizeof a));
    CHECK(!n64c_binds_save(&a, "/nonexistent-dir/x.cfg"));
}
static void finger(Uint32 type, long long id, float x, float y)
{
    SDL_Event e = {0}; e.type = type; e.tfinger.fingerID = (SDL_FingerID)id; e.tfinger.x = x; e.tfinger.y = y; n64c_input_event(&e);
}
static void touch(void)
{
    char dir[] = "/tmp/n64c_touch_XXXXXX", ctl[80], tp[80], err[100] = "";
    CHECK(mkdtemp(dir));
    snprintf(ctl, sizeof ctl, "%s/controls.cfg", dir); /* the layout is kept beside the binds file */
    snprintf(tp, sizeof tp, "%s/touch-layout.cfg", dir);
    fresh();
    n64c_touch_defaults(n64c_touch_layout());
    n64c_input_set_config_path(ctl);
    CHECK(n64c_input_start(window));
    N64cTouchLayout *l = n64c_touch_layout();
    CHECK(read_port(0, NULL).button == 0);
    /* Play: a finger on A presses A; sliding it to B presses B; the stick drag steers to full scale. */
    finger(SDL_EVENT_FINGER_DOWN, 1, l->zone[N64C_ACT_A].x, l->zone[N64C_ACT_A].y); n64c_input_update();
    CHECK(read_port(0, NULL).button == N64C_PAD_A);
    finger(SDL_EVENT_FINGER_MOTION, 1, l->zone[N64C_ACT_B].x, l->zone[N64C_ACT_B].y); n64c_input_update();
    CHECK(read_port(0, NULL).button == N64C_PAD_B);
    finger(SDL_EVENT_FINGER_UP, 1, 0, 0); n64c_input_update();
    CHECK(read_port(0, NULL).button == 0);
    float sx = l->zone[N64C_TOUCH_STICK].x, sy = l->zone[N64C_TOUCH_STICK].y;
    finger(SDL_EVENT_FINGER_DOWN, 2, sx, sy); finger(SDL_EVENT_FINGER_MOTION, 2, sx + 0.5f, sy); n64c_input_update();
    CHECK(read_port(0, NULL).stick_x == 80 && read_port(0, NULL).stick_y == 0 && read_port(1, NULL).stick_x == 0);
    finger(SDL_EVENT_FINGER_UP, 2, 0, 0);
    /* Edit: the top-left corner starts editing; drag moves a zone, a second finger drags its size; leaving saves. */
    finger(SDL_EVENT_FINGER_DOWN, 3, 0.01f, 0.01f); finger(SDL_EVENT_FINGER_UP, 3, 0, 0);
    CHECK(n64c_touch_editing());
    float ax = l->zone[N64C_ACT_A].x, ay = l->zone[N64C_ACT_A].y, ar = l->zone[N64C_ACT_A].r;
    finger(SDL_EVENT_FINGER_DOWN, 4, ax, ay); n64c_input_update();
    CHECK(read_port(0, NULL).button == 0); /* editing does not press buttons */
    finger(SDL_EVENT_FINGER_MOTION, 4, ax - 0.2f, ay - 0.1f);
    CHECK(fabsf(l->zone[N64C_ACT_A].x - (ax - 0.2f)) < 1e-4f && fabsf(l->zone[N64C_ACT_A].y - (ay - 0.1f)) < 1e-4f);
    finger(SDL_EVENT_FINGER_DOWN, 5, 0.5f, 0.5f); finger(SDL_EVENT_FINGER_MOTION, 5, 0.5f, 0.3f);
    CHECK(l->zone[N64C_ACT_A].r > ar);
    finger(SDL_EVENT_FINGER_UP, 5, 0, 0); finger(SDL_EVENT_FINGER_UP, 4, 0, 0);
    float moved_x = l->zone[N64C_ACT_A].x, moved_r = l->zone[N64C_ACT_A].r;
    finger(SDL_EVENT_FINGER_DOWN, 6, 0.01f, 0.01f); finger(SDL_EVENT_FINGER_UP, 6, 0, 0);
    CHECK(!n64c_touch_editing());
    /* Restore: a fresh start reads the saved layout back; reset in edit mode restores the factory one. */
    n64c_touch_defaults(l);
    CHECK(n64c_input_start(window) && fabsf(n64c_touch_layout()->zone[N64C_ACT_A].x - moved_x) < 1e-4f &&
          fabsf(n64c_touch_layout()->zone[N64C_ACT_A].r - moved_r) < 1e-4f);
    finger(SDL_EVENT_FINGER_DOWN, 7, 0.01f, 0.01f); finger(SDL_EVENT_FINGER_UP, 7, 0, 0);
    finger(SDL_EVENT_FINGER_DOWN, 8, 0.99f, 0.01f); finger(SDL_EVENT_FINGER_UP, 8, 0, 0);
    N64cTouchLayout def; n64c_touch_defaults(&def);
    CHECK(!memcmp(&def, n64c_touch_layout(), sizeof def));
    /* Bad file: refused with its line number, layout untouched. */
    N64cTouchLayout keep = *n64c_touch_layout();
    CHECK(!n64c_touch_parse(n64c_touch_layout(), "a = 0.5 0.5 0.05\nq = 1 1 1\n", err, sizeof err) && strstr(err, "line 2") &&
          !memcmp(&keep, n64c_touch_layout(), sizeof keep));
    CHECK(!n64c_touch_parse(n64c_touch_layout(), "a = 2 0.5 0.05\n", err, sizeof err));
    /* A pad's Back button opens the Controls page. */
    CHECK(!n64c_controls_active()); pad_button_event(SDL_GAMEPAD_BUTTON_BACK); CHECK(n64c_controls_active());
    n64c_controls_close();
    remove(tp); remove(ctl); rmdir(dir);
    n64c_input_stop();
    n64c_touch_set_editing(0);
}

/* Proof hook: N64C_VIRTUAL_PADS gives a headless run one pad per port; N64C_VPAD_SCRIPT presses their buttons by input frame. */
static void virtual_pads(void)
{
    const char *path = "n64c_vpad_script.txt";
    FILE *f = fopen(path, "w");
    CHECK(f != NULL);
    if (f) {
        fputs("2 1 a down\n2 4 b down\n3 1 a up\nbogus line\n", f);
        fclose(f);
    }
    fresh();
    virtual_attached = 0;
    setenv("N64C_VIRTUAL_PADS", "4", 1);
    setenv("N64C_VPAD_SCRIPT", path, 1);
    CHECK(n64c_input_start(window));
    CHECK(virtual_attached == 4 && present_bits() == 0xF);
    n64c_input_update();
    CHECK(read_port(0, NULL).button == 0 && read_port(3, NULL).button == 0);
    n64c_input_update();
    CHECK(read_port(0, NULL).button == 0x8000 && read_port(3, NULL).button == 0x4000 && read_port(1, NULL).button == 0);
    n64c_input_update();
    CHECK(read_port(0, NULL).button == 0 && read_port(3, NULL).button == 0x4000);
    n64c_input_stop();
    CHECK(!devices[0].connected && !devices[3].connected);
    unsetenv("N64C_VIRTUAL_PADS");
    unsetenv("N64C_VPAD_SCRIPT");
    remove(path);
}
int main(void)
{
    n64c_cont_reset(NULL);
    policy(); binds_model(); mapping(); axes(); pad_defaults(); four_ports(); virtual_pads(); mouse(); rebinding(); page(); touch();
    CHECK(inits - 1 == quits); /* exactly one unsuccessful init */
    printf("input_sdl: %d checks, %d failures (binds, mapping, SI, deadzone, policy, focus, 4-port hotplug, mouse, rebinding, page, touch)\n",
           checks, failures);
    return failures ? 1 : 0;
}
