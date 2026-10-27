/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifdef N64C_INPUT_SDL3
#include "n64c_controls.h"
#include "n64c_input.h"
#include <stdio.h>
#include <string.h>

enum { ROWS = N64C_ACT_COUNT + 2, ROW_RESET_PLAYER = N64C_ACT_COUNT, ROW_RESET_ALL, FIRST_ACTION_LINE = 4 };
static struct {
    int open, player, device, row, col, waiting, armed;
    char message[N64C_CONTROLS_COLUMNS + 1];
} page;
static const char *const device_names[N64C_DEV_COUNT] = {"KEYBOARD", "GAMEPAD", "MOUSE"};

static void say(const char *text) { snprintf(page.message, sizeof page.message, "%.*s", N64C_CONTROLS_COLUMNS, text); }
void n64c_controls_open(void)
{
    if (page.open) return;
    page.open = 1;
    page.waiting = page.armed = 0;
    say("");
}
void n64c_controls_close(void)
{
    if (!page.open) return;
    n64c_input_capture_cancel();
    page.open = page.waiting = 0;
    n64c_input_binds_save();
}
int n64c_controls_active(void) { return page.open; }
static int slot_of(void) { return page.device * N64C_SLOTS_PER_DEVICE + page.col; }
static void conflict_message(int player, int action, int slot)
{
    int op, oa;
    char text[64];
    if (n64c_binds_conflict(n64c_input_binds(), player, action, slot, &op, &oa)) {
        snprintf(text, sizeof text, "CONFLICT: ALSO P%d %s", op + 1, n64c_action_name(oa));
        say(text);
    } else say("");
}
static void activate(void)
{
    if (page.row < N64C_ACT_COUNT) {
        if (n64c_input_capture_begin(page.device)) {
            page.waiting = 1;
            say("PRESS AN INPUT, ESC CANCELS");
        }
        return;
    }
    if (!page.armed) {
        page.armed = 1;
        say("ENTER AGAIN TO CONFIRM RESET");
        return;
    }
    page.armed = 0;
    n64c_input_binds_reset(page.row == ROW_RESET_ALL ? -1 : page.player);
    say(page.row == ROW_RESET_ALL ? "ALL PLAYERS RESET" : "PLAYER RESET");
}
static void clear_slot(void)
{
    if (page.row >= N64C_ACT_COUNT) return;
    n64c_binds_set(n64c_input_binds(), page.player, page.row, slot_of(), (N64cBind){0});
    n64c_input_binds_save();
    say("CLEARED");
}
static void move(int rows, int cols)
{
    page.armed = 0;
    page.row = (page.row + rows + ROWS) % ROWS;
    if (cols) page.col ^= 1;
    if (page.row < N64C_ACT_COUNT) conflict_message(page.player, page.row, slot_of());
    else say("");
}
static void set_player(int p) { page.player = (p + N64C_PLAYERS) % N64C_PLAYERS; page.armed = 0; move(0, 0); }
static void set_device(int d) { page.device = (d + N64C_DEV_COUNT) % N64C_DEV_COUNT; page.armed = 0; move(0, 0); }
int n64c_controls_event(const SDL_Event *e)
{
    if (e->type == SDL_EVENT_KEY_DOWN && e->key.scancode == SDL_SCANCODE_F10 && !e->key.repeat) {
        if (page.open) n64c_controls_close();
        else n64c_controls_open();
        return 1;
    }
    if (!page.open) return 0;
    if (page.waiting) return 1;
    if (e->type == SDL_EVENT_KEY_DOWN) {
        switch (e->key.scancode) {
        case SDL_SCANCODE_ESCAPE: n64c_controls_close(); break;
        case SDL_SCANCODE_UP: move(-1, 0); break;
        case SDL_SCANCODE_DOWN: move(1, 0); break;
        case SDL_SCANCODE_LEFT: case SDL_SCANCODE_RIGHT: move(0, 1); break;
        case SDL_SCANCODE_TAB: set_device(page.device + ((e->key.mod & SDL_KMOD_SHIFT) ? -1 : 1)); break;
        case SDL_SCANCODE_1: case SDL_SCANCODE_2: case SDL_SCANCODE_3: case SDL_SCANCODE_4:
            set_player(e->key.scancode - SDL_SCANCODE_1);
            break;
        case SDL_SCANCODE_RETURN: case SDL_SCANCODE_SPACE: case SDL_SCANCODE_KP_ENTER:
            if (!e->key.repeat) activate();
            break;
        case SDL_SCANCODE_DELETE: case SDL_SCANCODE_BACKSPACE: clear_slot(); break;
        default: break;
        }
    } else if (e->type == SDL_EVENT_GAMEPAD_BUTTON_DOWN) {
        switch (e->gbutton.button) {
        case SDL_GAMEPAD_BUTTON_DPAD_UP: move(-1, 0); break;
        case SDL_GAMEPAD_BUTTON_DPAD_DOWN: move(1, 0); break;
        case SDL_GAMEPAD_BUTTON_DPAD_LEFT: case SDL_GAMEPAD_BUTTON_DPAD_RIGHT: move(0, 1); break;
        case SDL_GAMEPAD_BUTTON_SOUTH: activate(); break;
        case SDL_GAMEPAD_BUTTON_WEST: clear_slot(); break;
        case SDL_GAMEPAD_BUTTON_EAST: n64c_controls_close(); break;
        case SDL_GAMEPAD_BUTTON_NORTH: set_player(page.player + 1); break;
        case SDL_GAMEPAD_BUTTON_LEFT_SHOULDER: set_device(page.device - 1); break;
        case SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER: set_device(page.device + 1); break;
        default: break;
        }
    }
    return 1;
}
void n64c_controls_update(void)
{
    N64cBind bind;
    if (!page.open || !page.waiting) return;
    int state = n64c_input_capture_poll(&bind);
    if (!state) return;
    page.waiting = 0;
    if (state < 0) { say("CANCELLED"); return; }
    n64c_binds_set(n64c_input_binds(), page.player, page.row, slot_of(), bind);
    n64c_input_binds_save();
    conflict_message(page.player, page.row, slot_of());
    if (!page.message[0]) say("BOUND");
}
int n64c_controls_lines(N64cControlsLine *lines, int max, int *cursor_line)
{
    N64cControlsLine all[N64C_CONTROLS_MAX_LINES];
    int n = 0;
    memset(all, 0, sizeof all);
#define LINE(...) do { if (n < N64C_CONTROLS_MAX_LINES) snprintf(all[n].text, sizeof all[n].text, __VA_ARGS__); ++n; } while (0)
    const N64cBinds *binds = n64c_input_binds();
    const char *pad = n64c_input_pad_name(page.player);
    LINE("CONTROLS  PLAYER %d OF 4", page.player + 1);
    LINE("GAMEPAD: %.29s", pad ? pad : (page.player ? "NONE PLUGGED IN" : "NONE (KEYBOARD)"));
    LINE("%c%s%c %c%s%c %c%s%c", page.device == 0 ? '[' : ' ', device_names[0], page.device == 0 ? ']' : ' ',
         page.device == 1 ? '[' : ' ', device_names[1], page.device == 1 ? ']' : ' ',
         page.device == 2 ? '[' : ' ', device_names[2], page.device == 2 ? ']' : ' ');
    LINE(" ");
    for (int a = 0; a < N64C_ACT_COUNT; ++a) {
        char cell[N64C_SLOTS_PER_DEVICE][13];
        for (int c = 0; c < N64C_SLOTS_PER_DEVICE; ++c) {
            int slot = page.device * N64C_SLOTS_PER_DEVICE + c, selected = page.row == a && page.col == c;
            char label[24];
            n64c_bind_label(&binds->player[page.player].slot[a][slot], label, sizeof label);
            snprintf(cell[c], sizeof cell[c], "%c%-9.9s%c%c", selected ? '[' : ' ', label,
                     n64c_binds_conflict(binds, page.player, a, slot, NULL, NULL) ? '!' : ' ', selected ? ']' : ' ');
        }
        LINE("%c%-11s%s%s", page.row == a ? '>' : ' ', n64c_action_name(a), cell[0], cell[1]);
    }
    LINE("%cRESET THIS PLAYER", page.row == ROW_RESET_PLAYER ? '>' : ' ');
    LINE("%cRESET ALL PLAYERS", page.row == ROW_RESET_ALL ? '>' : ' ');
    int conflicts = n64c_binds_conflict_count(binds);
    if (conflicts) LINE("%d BIND%s IN CONFLICT (MARKED !)", conflicts, conflicts == 1 ? "" : "S");
    else LINE(" ");
    LINE("%s", page.message);
    LINE("ENTER REBIND DEL CLEAR TAB DEVICE");
    LINE("1-4 PLAYER ARROWS MOVE ESC CLOSE");
    LINE("PAD: A REBIND X CLEAR Y PLAYER B BACK");
#undef LINE
    if (n > N64C_CONTROLS_MAX_LINES) n = N64C_CONTROLS_MAX_LINES;
    if (n > max) n = max;
    memcpy(lines, all, (size_t)n * sizeof *lines);
    if (cursor_line) *cursor_line = FIRST_ACTION_LINE + page.row;
    return n;
}
#endif
