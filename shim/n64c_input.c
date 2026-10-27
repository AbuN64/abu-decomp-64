/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Original native SDL3 adapter, using only SDL's public device API. */
#ifdef N64C_INPUT_SDL3
#include "n64c_input.h"
#include "n64c_controls.h"
#include "n64c_touch.h"
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { STICK_MAX = 80, LOOK_CAPTURE_PIXELS = 24 };
static SDL_Window *input_window;
static SDL_Gamepad *pads[N64C_PLAYERS];
static N64cBinds binds;
static int binds_ready;
static char config_path[512], touch_path[512];
static OSContPad snapshot[N64C_PLAYERS];
static int present[N64C_PLAYERS];
static pthread_mutex_t snapshot_mutex = PTHREAD_MUTEX_INITIALIZER;
static int mouse_dx, mouse_dy, relative_mouse;
static struct { int device, state; N64cBind result; int look_x, look_y; } capture; /* state: 0 idle 1 waiting 2 got 3 cancelled */

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
unsigned n64c_action_button(int action)
{
    static const unsigned bits[N64C_ACT_COUNT] = {
        N64C_PAD_A, N64C_PAD_B, N64C_PAD_Z, N64C_PAD_L, N64C_PAD_R, N64C_PAD_START,
        N64C_PAD_CUP, N64C_PAD_CDOWN, N64C_PAD_CLEFT, N64C_PAD_CRIGHT,
        N64C_PAD_UP, N64C_PAD_DOWN, N64C_PAD_LEFT, N64C_PAD_RIGHT, 0, 0, 0, 0};
    return action >= 0 && action < N64C_ACT_COUNT ? bits[action] : 0;
}
N64cBinds *n64c_input_binds(void)
{
    if (!binds_ready) { n64c_binds_defaults(&binds); binds_ready = 1; }
    return &binds;
}
void n64c_input_set_config_path(const char *path)
{
    snprintf(config_path, sizeof config_path, "%s", path ? path : "");
    touch_path[0] = 0; /* the touch layout sits beside the binds */
    if (config_path[0]) {
        const char *slash = strrchr(config_path, '/');
        snprintf(touch_path, sizeof touch_path, "%.*stouch-layout.cfg", slash ? (int)(slash - config_path) + 1 : 0, config_path);
    }
}
int n64c_input_touch_save(void)
{
    if (!touch_path[0]) return 0;
    if (n64c_touch_save(n64c_touch_layout(), touch_path)) return 1;
    fprintf(stderr, "input: cannot save the touch layout to %s\n", touch_path);
    return 0;
}
void n64c_input_touch_edit(int editing)
{
    if (editing == n64c_touch_editing()) return;
    n64c_touch_set_editing(editing);
    if (!editing) n64c_input_touch_save();
}
int n64c_input_binds_save(void)
{
    if (!config_path[0]) return 0;
    if (n64c_binds_save(n64c_input_binds(), config_path)) return 1;
    fprintf(stderr, "input: cannot save the controls to %s\n", config_path);
    return 0;
}
void n64c_input_binds_reset(int player)
{
    N64cBinds *b = n64c_input_binds();
    for (int p = 0; p < N64C_PLAYERS; ++p)
        if (player < 0 || player == p) n64c_binds_player_defaults(b, p);
    n64c_input_binds_save();
}
static void load_config(void)
{
    N64cBinds *b = n64c_input_binds();
    char err[160] = "";
    if (config_path[0] && !n64c_binds_load(b, config_path, err, sizeof err))
        fprintf(stderr, "input: %s: %s; using the current controls\n", config_path, err);
}
static void publish(int port, OSContPad pad, int is_present)
{
    pthread_mutex_lock(&snapshot_mutex);
    snapshot[port] = pad;
    if (is_present >= 0) present[port] = is_present;
    pthread_mutex_unlock(&snapshot_mutex);
}
int n64c_input_port_present(int port)
{
    if (port < 0 || port >= N64C_PLAYERS) return 0;
    pthread_mutex_lock(&snapshot_mutex);
    int value = present[port];
    pthread_mutex_unlock(&snapshot_mutex);
    return value;
}
/* The guest sees a plug or unplug at its next read, not a frame later. */
static void refresh_presence(void)
{
    pthread_mutex_lock(&snapshot_mutex);
    for (int p = 0; p < N64C_PLAYERS; ++p)
        present[p] = p == 0 || pads[p] || n64c_binds_player_has_device(&binds, p, N64C_DEV_KEYBOARD) ||
                     n64c_binds_player_has_device(&binds, p, N64C_DEV_MOUSE);
    if (getenv("N64C_INPUT_LOG")) {
        static int last_mask = -1, last_pads = -1;
        int mask = 0, pad_mask = 0;
        for (int p = 0; p < N64C_PLAYERS; ++p) {
            mask |= present[p] << p;
            pad_mask |= (pads[p] != NULL) << p;
        }
        if (mask != last_mask || pad_mask != last_pads)
            fprintf(stderr, "input: ports present 0x%x, gamepad on ports 0x%x\n", mask, pad_mask);
        last_mask = mask;
        last_pads = pad_mask;
    }
    pthread_mutex_unlock(&snapshot_mutex);
}
static int port_present(int channel, void *user)
{
    (void)user;
    return n64c_input_port_present(channel);
}
/* ---- scripted virtual pads (proof hook) ----
 * N64C_VIRTUAL_PADS=N attaches N SDL virtual gamepads at start, so a headless run (Xvfb) has one pad per port. N64C_VPAD_SCRIPT=FILE
 * presses their buttons: one "FRAME PORT BUTTON down|up" per line (BUTTON is an SDL gamepad button name: a, b, start, dpup ...),
 * applied when the input frame count reaches FRAME. N64C_INPUT_LOG=1 prints each change of the present-port mask. */
enum { VPAD_MAX = N64C_PLAYERS, VPAD_SCRIPT_MAX = 512 };
static SDL_Joystick *vpads[VPAD_MAX];
static struct { long frame; int port, button, down; } vscript[VPAD_SCRIPT_MAX];
static int vscript_count, vscript_next;
static long vframe;
static int vpad_attach(int count)
{
    SDL_VirtualJoystickDesc desc;
    SDL_INIT_INTERFACE(&desc);
    desc.type = SDL_JOYSTICK_TYPE_GAMEPAD;
    desc.naxes = SDL_GAMEPAD_AXIS_COUNT;
    desc.nbuttons = SDL_GAMEPAD_BUTTON_COUNT;
    desc.name = "n64c virtual pad";
    for (int i = 0; i < count && i < VPAD_MAX; ++i) {
        SDL_JoystickID id = SDL_AttachVirtualJoystick(&desc);
        if (id) vpads[i] = SDL_OpenJoystick(id);
    }
    return count;
}
static void vpad_load_script(const char *path)
{
    FILE *f = fopen(path, "r");
    char name[32], dir[8];
    long frame;
    int port;
    vscript_count = vscript_next = 0;
    vframe = 0;
    while (f && vscript_count < VPAD_SCRIPT_MAX && fscanf(f, "%ld %d %31s %7s", &frame, &port, name, dir) == 4) {
        SDL_GamepadButton b = SDL_GetGamepadButtonFromString(name);
        if (b == SDL_GAMEPAD_BUTTON_INVALID || port < 1 || port > VPAD_MAX) continue;
        vscript[vscript_count].frame = frame;
        vscript[vscript_count].port = port - 1;
        vscript[vscript_count].button = b;
        vscript[vscript_count++].down = !strcmp(dir, "down");
    }
    if (f) fclose(f);
}
static void vpad_tick(void)
{
    ++vframe;
    while (vscript_next < vscript_count && vscript[vscript_next].frame <= vframe) {
        SDL_Joystick *j = vpads[vscript[vscript_next].port];
        if (j) SDL_SetJoystickVirtualButton(j, vscript[vscript_next].button, vscript[vscript_next].down != 0);
        ++vscript_next;
    }
}
static void vpad_detach(void)
{
    for (int i = 0; i < VPAD_MAX; ++i)
        if (vpads[i]) {
            SDL_JoystickID id = SDL_GetJoystickID(vpads[i]);
            SDL_CloseJoystick(vpads[i]);
            SDL_DetachVirtualJoystick(id);
            vpads[i] = NULL;
        }
    vscript_count = vscript_next = 0;
}

static void read_pad(int channel, OSContPad *pad, void *user)
{
    (void)user;
    *pad = (OSContPad){0};
    if (channel < 0 || channel >= N64C_PLAYERS) { pad->errno_ = CONT_ERR_NO_CONTROLLER; return; }
    pthread_mutex_lock(&snapshot_mutex);
    *pad = snapshot[channel];
    if (!present[channel]) pad->errno_ = CONT_ERR_NO_CONTROLLER;
    pthread_mutex_unlock(&snapshot_mutex);
}
const char *n64c_input_pad_name(int port)
{
    return port >= 0 && port < N64C_PLAYERS && pads[port] ? SDL_GetGamepadName(pads[port]) : NULL;
}
/* One pad per port: a new pad takes the lowest free port, a removed one frees its port for the next arrival. */
static void discover(void)
{
    int count = 0;
    SDL_JoystickID *ids = SDL_GetGamepads(&count);
    for (int i = 0; ids && i < count; ++i) {
        int taken = 0, free_port = -1;
        for (int p = 0; p < N64C_PLAYERS; ++p) {
            if (pads[p] && SDL_GetGamepadID(pads[p]) == ids[i]) taken = 1;
            else if (!pads[p] && free_port < 0) free_port = p;
        }
        if (!taken && free_port >= 0) pads[free_port] = SDL_OpenGamepad(ids[i]);
    }
    SDL_free(ids);
}
static void drop_pad(int port)
{
    if (pads[port]) SDL_CloseGamepad(pads[port]);
    pads[port] = NULL;
}
void n64c_input_stop(void)
{
    if (!input_window) return;
    n64c_cont_set_presence(NULL, NULL);
    n64c_cont_set_input(NULL, NULL);
    n64c_controls_close();
    if (relative_mouse) SDL_SetWindowRelativeMouseMode(input_window, false);
    relative_mouse = 0;
    for (int p = 0; p < N64C_PLAYERS; ++p) {
        drop_pad(p);
        publish(p, (OSContPad){0}, 0);
    }
    capture.state = 0;
    input_window = NULL;
    vpad_detach();
    SDL_QuitSubSystem(SDL_INIT_GAMEPAD);
}
int n64c_input_start(SDL_Window *window)
{
    n64c_input_stop();
    const char *driver = SDL_GetCurrentVideoDriver();
    if (!window || getenv("N64C_LOCKSTEP") || !driver ||
        !strcmp(driver, "offscreen") || !strcmp(driver, "dummy")) return 1;
    if (!SDL_InitSubSystem(SDL_INIT_GAMEPAD)) return 0;
    input_window = window;
    load_config();
    {
        char err[160] = "";
        if (touch_path[0] && !n64c_touch_load(n64c_touch_layout(), touch_path, err, sizeof err))
            fprintf(stderr, "input: %s: %s; using the current touch layout\n", touch_path, err);
    }
    SDL_SetHint(SDL_HINT_TOUCH_MOUSE_EVENTS, "0"); /* fingers drive the touch zones, not a mouse pointer */
    n64c_touch_release_all();
    mouse_dx = mouse_dy = 0;
    capture.state = 0;
    for (int p = 0; p < N64C_PLAYERS; ++p) publish(p, (OSContPad){0}, p == 0);
    {
        const char *virt = getenv("N64C_VIRTUAL_PADS"), *script = getenv("N64C_VPAD_SCRIPT");
        if (virt && atoi(virt) > 0) vpad_attach(atoi(virt));
        if (script && *script) vpad_load_script(script);
    }
    discover();
    refresh_presence(); /* pads already plugged in at launch are present for the game's first osContInit */
    n64c_cont_set_presence(port_present, NULL);
    n64c_cont_set_input(read_pad, NULL);
    return 1;
}
/* ---- rebinding ---- */
int n64c_input_capture_begin(int device)
{
    if (device < 0 || device >= N64C_DEV_COUNT || !input_window) return 0;
    capture.device = device;
    capture.state = 1;
    capture.look_x = capture.look_y = 0;
    return 1;
}
int n64c_input_capture_poll(N64cBind *out)
{
    if (capture.state == 1) return 0;
    int got = capture.state == 2;
    if (got && out) *out = capture.result;
    capture.state = 0;
    return got ? 1 : -1;
}
void n64c_input_capture_cancel(void) { capture.state = 0; }
int n64c_input_capturing(void) { return capture.state == 1; }
static void captured(N64cBind bind) { capture.result = bind; capture.state = 2; }
static void capture_event(const SDL_Event *e)
{
    int dev = capture.device;
    switch (e->type) {
    case SDL_EVENT_KEY_DOWN:
        if (e->key.repeat) break;
        if (e->key.scancode == SDL_SCANCODE_ESCAPE) capture.state = 3;
        else if (dev == N64C_DEV_KEYBOARD) captured((N64cBind){N64C_BIND_KEY, 0, (short)e->key.scancode});
        break;
    case SDL_EVENT_GAMEPAD_BUTTON_DOWN:
        if (dev == N64C_DEV_GAMEPAD) captured((N64cBind){N64C_BIND_PAD_BUTTON, 0, (short)e->gbutton.button});
        break;
    case SDL_EVENT_GAMEPAD_AXIS_MOTION: {
        int v = e->gaxis.value;
        if (dev == N64C_DEV_GAMEPAD && (v >= binds.button_threshold || v <= -binds.button_threshold))
            captured((N64cBind){N64C_BIND_PAD_AXIS, (signed char)(v > 0 ? 1 : -1), (short)e->gaxis.axis});
        break;
    }
    case SDL_EVENT_MOUSE_BUTTON_DOWN:
        if (dev == N64C_DEV_MOUSE) captured((N64cBind){N64C_BIND_MOUSE_BUTTON, 0, (short)e->button.button});
        break;
    case SDL_EVENT_MOUSE_MOTION:
        if (dev != N64C_DEV_MOUSE) break;
        capture.look_x += (int)e->motion.xrel;
        capture.look_y += (int)e->motion.yrel;
        if (abs(capture.look_x) >= LOOK_CAPTURE_PIXELS || abs(capture.look_y) >= LOOK_CAPTURE_PIXELS) {
            int on_x = abs(capture.look_x) >= abs(capture.look_y), v = on_x ? capture.look_x : capture.look_y;
            captured((N64cBind){N64C_BIND_MOUSE_LOOK, (signed char)(v > 0 ? 1 : -1), (short)(on_x ? 0 : 1)});
        }
        break;
    default: break;
    }
}
void n64c_input_event(const SDL_Event *event)
{
    if (!input_window) return;
    if (event->type == SDL_EVENT_GAMEPAD_REMOVED) {
        for (int p = 0; p < N64C_PLAYERS; ++p)
            if (pads[p] && SDL_GetGamepadID(pads[p]) == event->gdevice.which) {
                drop_pad(p);
                publish(p, (OSContPad){0}, -1);
            }
        discover(); /* promote an already connected spare pad */
        refresh_presence();
    } else if (event->type == SDL_EVENT_GAMEPAD_ADDED) {
        discover();
        refresh_presence();
    } else if (event->type == SDL_EVENT_FINGER_DOWN || event->type == SDL_EVENT_FINGER_MOTION ||
               event->type == SDL_EVENT_FINGER_UP || event->type == SDL_EVENT_FINGER_CANCELED) {
        int w = 0, h = 0;
        SDL_GetWindowSize(input_window, &w, &h);
        if (w > 0 && h > 0) n64c_touch_set_aspect((float)w / (float)h);
        long long id = (long long)event->tfinger.fingerID;
        if (event->type == SDL_EVENT_FINGER_DOWN) {
            int corner = n64c_touch_down(id, event->tfinger.x, event->tfinger.y);
            if (corner == N64C_TOUCH_CORNER_MENU) n64c_input_touch_edit(!n64c_touch_editing());
            else if (corner == N64C_TOUCH_CORNER_RESET && n64c_touch_editing()) {
                n64c_touch_defaults(n64c_touch_layout());
                n64c_input_touch_save();
            }
        } else if (event->type == SDL_EVENT_FINGER_MOTION) n64c_touch_move(id, event->tfinger.x, event->tfinger.y);
        else n64c_touch_up(id);
        return;
    } else if (event->type == SDL_EVENT_GAMEPAD_BUTTON_DOWN && event->gbutton.button == SDL_GAMEPAD_BUTTON_BACK &&
               !n64c_controls_active() && capture.state != 1) {
        n64c_controls_open(); /* a pad's Back button opens the Controls page, as F10 does on a keyboard */
        return;
    } else if (event->type == SDL_EVENT_MOUSE_MOTION) {
        mouse_dx += (int)event->motion.xrel;
        mouse_dy += (int)event->motion.yrel;
    }
    if (capture.state == 1) capture_event(event);
    else n64c_controls_event(event);
}
/* The strongest contribution (0..80) of one action's slots; digital sources give full scale. */
static int action_level(int port, int action, const bool *keys, int key_count, int has_focus, Uint32 mouse_mask)
{
    const N64cPlayerBinds *player = &binds.player[port];
    int stick = n64c_action_button(action) == 0, best = 0;
    for (int s = 0; s < N64C_SLOT_COUNT; ++s) {
        const N64cBind *b = &player->slot[action][s];
        int level = 0;
        switch (b->kind) {
        case N64C_BIND_KEY:
            level = has_focus && keys && b->code < key_count && keys[b->code] ? STICK_MAX : 0;
            break;
        case N64C_BIND_PAD_BUTTON:
            level = pads[port] && SDL_GetGamepadButton(pads[port], (SDL_GamepadButton)b->code) ? STICK_MAX : 0;
            break;
        case N64C_BIND_PAD_AXIS:
            if (pads[port]) {
                int v = SDL_GetGamepadAxis(pads[port], (SDL_GamepadAxis)b->code) * b->dir;
                if (stick) level = v > 0 ? n64c_input_axis(v, binds.deadzone) : 0;
                else level = v > binds.button_threshold ? STICK_MAX : 0;
            }
            break;
        case N64C_BIND_MOUSE_BUTTON:
            level = has_focus && (mouse_mask & SDL_BUTTON_MASK(b->code)) ? STICK_MAX : 0;
            break;
        case N64C_BIND_MOUSE_LOOK: {
            int d = (b->code ? mouse_dy : mouse_dx) * b->dir;
            level = has_focus && d > 0 ? (d * binds.mouse_sensitivity > STICK_MAX ? STICK_MAX : d * binds.mouse_sensitivity) : 0;
            break;
        }
        default: break;
        }
        if (level > best) best = level;
    }
    return best;
}
static int clamp_stick(int value) { return value < -STICK_MAX ? -STICK_MAX : value > STICK_MAX ? STICK_MAX : value; }
void n64c_input_update(void)
{
    if (!input_window) return;
    vpad_tick();
    int has_focus = SDL_GetKeyboardFocus() == input_window;
    int page = n64c_controls_active() || capture.state == 1;
    int want_relative = has_focus && !page && n64c_binds_has_mouse_look(&binds);
    if (want_relative != relative_mouse) {
        SDL_SetWindowRelativeMouseMode(input_window, want_relative != 0);
        relative_mouse = want_relative;
    }
    for (int p = 0; p < N64C_PLAYERS; ++p)
        if (pads[p] && !SDL_GamepadConnected(pads[p])) drop_pad(p);
    int key_count = 0;
    const bool *keys = SDL_GetKeyboardState(&key_count);
    Uint32 mouse_mask = SDL_GetMouseState(NULL, NULL);
    for (int p = 0; p < N64C_PLAYERS; ++p) {
        OSContPad pad = {0};
        if (!page) {
            int level[N64C_ACT_COUNT];
            for (int a = 0; a < N64C_ACT_COUNT; ++a) {
                level[a] = action_level(p, a, keys, key_count, has_focus, mouse_mask);
                if (n64c_action_button(a) && level[a]) pad.button |= (u16)n64c_action_button(a);
            }
            pad.stick_x = (s8)clamp_stick(level[N64C_ACT_STICK_RIGHT] - level[N64C_ACT_STICK_LEFT]);
            pad.stick_y = (s8)clamp_stick(level[N64C_ACT_STICK_UP] - level[N64C_ACT_STICK_DOWN]);
        }
        if (p == 0 && !page && n64c_touch_seen()) {
            unsigned tb;
            int tx, ty;
            n64c_touch_pad(&tb, &tx, &ty);
            pad.button |= (u16)tb;
            if (!pad.stick_x) pad.stick_x = (s8)tx;
            if (!pad.stick_y) pad.stick_y = (s8)ty;
        }
        publish(p, pad, -1);
    }
    refresh_presence();
    mouse_dx = mouse_dy = 0;
    n64c_controls_update();
}
/* ---- labels ---- */
void n64c_bind_label(const N64cBind *b, char *out, size_t cap)
{
    static const char *const buttons[] = {"A", "B", "X", "Y", "BACK", "GUIDE", "START", "LSTICK", "RSTICK", "LB", "RB",
                                          "DPAD UP", "DPAD DOWN", "DPAD LEFT", "DPAD RIGHT", "MISC", "PADDLE1",
                                          "PADDLE2", "PADDLE3", "PADDLE4", "TOUCHPAD"};
    static const char *const axes[] = {"LS X", "LS Y", "RS X", "RS Y", "LT", "RT"};
    switch (b->kind) {
    case N64C_BIND_KEY: {
        const char *name = SDL_GetScancodeName((SDL_Scancode)b->code);
        snprintf(out, cap, "%s", name && *name ? name : "KEY ?");
        break;
    }
    case N64C_BIND_PAD_BUTTON:
        if ((size_t)b->code < sizeof buttons / sizeof *buttons) snprintf(out, cap, "%s", buttons[b->code]);
        else snprintf(out, cap, "BTN %d", b->code);
        break;
    case N64C_BIND_PAD_AXIS:
        if ((size_t)b->code < sizeof axes / sizeof *axes)
            snprintf(out, cap, b->code >= 4 ? "%s" : "%s%c", axes[b->code], b->dir > 0 ? '+' : '-');
        else snprintf(out, cap, "AXIS %d%c", b->code, b->dir > 0 ? '+' : '-');
        break;
    case N64C_BIND_MOUSE_BUTTON: snprintf(out, cap, "MOUSE %d", b->code); break;
    case N64C_BIND_MOUSE_LOOK:
        snprintf(out, cap, "LOOK %s", b->code ? (b->dir > 0 ? "DOWN" : "UP") : (b->dir > 0 ? "RIGHT" : "LEFT"));
        break;
    default: snprintf(out, cap, "-"); break;
    }
}
#endif
