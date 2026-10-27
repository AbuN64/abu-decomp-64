/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The options screen (n64c_menu.h): model, settings file and pad steering. Host layer only; no SDL, no guest state. */
#include "n64c_menu.h"
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum {
    PAD_A = 0x8000, PAD_B = 0x4000, PAD_START = 0x1000, PAD_UP = 0x0800, PAD_DOWN = 0x0400,
    PAD_LEFT = 0x0200, PAD_RIGHT = 0x0100, PAD_L = 0x0020, PAD_R = 0x0010
};
enum { ROW_RESUME, ROW_WINDOW, ROW_RESOLUTION, ROW_SCALE, ROW_UNLOCK, ROW_QUIT };
enum { STICK_ON = 40, REPEAT_DELAY = 20, REPEAT_EVERY = 4, MAX_SIZES = 64, SCALE_STEP = 25 };

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static N64cOptions opts;
static char save_path[1024];
static u32 sizes[MAX_SIZES][2];
static size_t size_count;
static int mobile, is_open, selected, quit_armed;
static unsigned menus, todo;
static unsigned held, suppress;   /* buttons down at the previous sample; buttons the game must not see until released */
static int suppress_stick, hotkey_was, repeat_count;

void n64c_options_default(N64cOptions *o)
{
    *o = (N64cOptions){.window_mode = N64C_WIN_WINDOWED, .width = 640, .height = 480, .fps_unlock = 0, .render_scale = 100};
}

static const char *const mode_names[] = {"windowed", "borderless", "fullscreen"};

int n64c_options_load(const char *path, N64cOptions *o)
{
    FILE *f = path ? fopen(path, "r") : NULL;
    if (!f) return 0;
    char line[128];
    while (fgets(line, sizeof line, f)) {
        char *eq = strchr(line, '=');
        if (!eq) continue;
        *eq = 0;
        char *value = eq + 1;
        value[strcspn(value, "\r\n")] = 0;
        char *end;
        long number = strtol(value, &end, 10);
        const int numeric = *value && !*end;
        if (!strcmp(line, "window_mode")) {
            for (int m = 0; m < 3; m++) if (!strcmp(value, mode_names[m])) o->window_mode = m;
        } else if (!strcmp(line, "width") && numeric && number >= 160 && number <= 16384) o->width = (u32)number;
        else if (!strcmp(line, "height") && numeric && number >= 120 && number <= 16384) o->height = (u32)number;
        else if (!strcmp(line, "fps_unlock") && numeric) o->fps_unlock = number != 0;
        else if (!strcmp(line, "render_scale") && numeric && number >= 25 && number <= 100) o->render_scale = (int)number;
    }
    fclose(f);
    return 1;
}

int n64c_options_save(const char *path, const N64cOptions *o)
{
    char tmp[1100];
    if (!path || !*path || snprintf(tmp, sizeof tmp, "%s.new", path) >= (int)sizeof tmp) return 0;
    FILE *f = fopen(tmp, "w");
    if (!f) return 0;
    int ok = fprintf(f, "window_mode=%s\nwidth=%u\nheight=%u\nfps_unlock=%d\nrender_scale=%d\n", mode_names[o->window_mode],
                     o->width, o->height, o->fps_unlock, o->render_scale) > 0;
    ok = fclose(f) == 0 && ok;
    if (ok && rename(tmp, path) != 0) ok = 0;
    if (!ok) remove(tmp);
    return ok;
}

void n64c_menu_configure(const N64cOptions *now, const char *path, const u32 (*list)[2], size_t count, int phone)
{
    pthread_mutex_lock(&lock);
    opts = *now;
    snprintf(save_path, sizeof save_path, "%s", path ? path : "");
    size_count = count > MAX_SIZES ? MAX_SIZES : count;
    if (size_count) memcpy(sizes, list, size_count * sizeof sizes[0]);
    mobile = phone;
    is_open = selected = quit_armed = 0;
    menus = todo = held = suppress = 0;
    suppress_stick = hotkey_was = repeat_count = 0;
    pthread_mutex_unlock(&lock);
}

void n64c_menu_context(unsigned flags)
{
#ifdef N64C_NO_OPTIONS /* the A/B build: the screen never learns it could open, so it never opens, draws or takes input */
    (void)flags;
#else
    pthread_mutex_lock(&lock);
    menus = flags;
    pthread_mutex_unlock(&lock);
#endif
}

static int row_at(int index)
{
    static const int desktop[] = {ROW_RESUME, ROW_WINDOW, ROW_RESOLUTION, ROW_UNLOCK, ROW_QUIT};
    static const int phone[] = {ROW_RESUME, ROW_SCALE, ROW_UNLOCK, ROW_QUIT};
    return mobile ? phone[index] : desktop[index];
}
static int row_count(void) { return mobile ? 4 : 5; }

static void changed(unsigned what)
{
    todo |= what;
    n64c_options_save(save_path, &opts);
}

static int size_index(void)
{
    for (size_t i = 0; i < size_count; i++)
        if (sizes[i][0] == opts.width && sizes[i][1] == opts.height) return (int)i;
    return -1;
}

/* Left/right on a row: delta -1 or +1. */
static void adjust(int row, int delta)
{
    quit_armed = 0;
    if (row == ROW_WINDOW) {
        opts.window_mode = (opts.window_mode + delta + 3) % 3;
        changed(N64C_DO_WINDOW);
    } else if (row == ROW_RESOLUTION && size_count) {
        int i = size_index();
        i = i < 0 ? (delta > 0 ? 0 : (int)size_count - 1) : (i + delta + (int)size_count) % (int)size_count;
        opts.width = sizes[i][0];
        opts.height = sizes[i][1];
        changed(N64C_DO_WINDOW);
    } else if (row == ROW_SCALE) {
        int s = opts.render_scale + delta * SCALE_STEP;
        opts.render_scale = s < 25 ? 25 : s > 100 ? 100 : s;
        changed(N64C_DO_SCALE);
    } else if (row == ROW_UNLOCK) {
        opts.fps_unlock = !opts.fps_unlock;
        changed(N64C_DO_UNLOCK);
    }
}

static void close_screen(void)
{
    is_open = quit_armed = 0;
    suppress = held;   /* whatever closed it is still down: the game must not see it */
    suppress_stick = 1;
}

static void press(unsigned button)
{
    const int row = row_at(selected);
    if (button == PAD_UP || button == PAD_DOWN) {
        quit_armed = 0;
        selected = (selected + (button == PAD_DOWN ? 1 : row_count() - 1)) % row_count();
    } else if (button == PAD_LEFT || button == PAD_RIGHT) {
        adjust(row, button == PAD_RIGHT ? 1 : -1);
    } else if (button == PAD_A) {
        if (row == ROW_RESUME) close_screen();
        else if (row == ROW_QUIT) {
            if (quit_armed) { todo |= N64C_DO_QUIT; close_screen(); }
            else quit_armed = 1;
        } else if (row == ROW_UNLOCK || row == ROW_WINDOW || row == ROW_RESOLUTION || row == ROW_SCALE) adjust(row, 1);
    } else if (button == PAD_B || button == PAD_START) {
        if (quit_armed) quit_armed = 0;
        else close_screen();
    }
}

void n64c_menu_filter(OSContPad *pad, int hotkey)
{
    pthread_mutex_lock(&lock);
    const unsigned raw = pad->button;
    const int stick_up = pad->stick_y > STICK_ON, stick_down = pad->stick_y < -STICK_ON;
    const int stick_left = pad->stick_x < -STICK_ON, stick_right = pad->stick_x > STICK_ON;
    unsigned now = raw & (PAD_A | PAD_B | PAD_START | PAD_UP | PAD_DOWN | PAD_LEFT | PAD_RIGHT | PAD_L | PAD_R);
    if (stick_up) now |= PAD_UP;
    if (stick_down) now |= PAD_DOWN;
    if (stick_left) now |= PAD_LEFT;
    if (stick_right) now |= PAD_RIGHT;
    const unsigned chord = PAD_L | PAD_R;
    const int open_now = (hotkey && !hotkey_was) || ((now & chord) == chord && (held & chord) != chord);
    hotkey_was = hotkey != 0;
    const int was_open = is_open;
    if (!is_open) {
        if (menus && open_now) {
            is_open = 1;
            selected = quit_armed = repeat_count = 0;
        } else {
            pad->button = (u16)(raw & ~suppress);
            suppress &= raw;
            if (suppress_stick && !stick_up && !stick_down && !stick_left && !stick_right) suppress_stick = 0;
            if (suppress_stick) pad->stick_x = pad->stick_y = 0;
            held = now;
            pthread_mutex_unlock(&lock);
            return;
        }
    } else if (open_now) {
        close_screen();
    }
    if (is_open) {
        static const unsigned nav[] = {PAD_UP, PAD_DOWN, PAD_LEFT, PAD_RIGHT, PAD_A, PAD_B, PAD_START};
        const unsigned fresh = now & ~held;
        const unsigned moves = PAD_UP | PAD_DOWN | PAD_LEFT | PAD_RIGHT;
        int repeat = 0;
        if (now & moves) {
            repeat_count++;
            repeat = repeat_count > REPEAT_DELAY && (repeat_count - REPEAT_DELAY) % REPEAT_EVERY == 0;
        } else repeat_count = 0;
        for (size_t i = 0; i < sizeof nav / sizeof nav[0] && is_open; i++)
            if ((fresh & nav[i]) || (repeat && (now & nav[i]) && (nav[i] & moves))) press(nav[i]);
        *pad = (OSContPad){.errno_ = pad->errno_};
    } else {
        pad->button = 0;
        pad->stick_x = pad->stick_y = 0;
    }
    held = now;
    if (was_open && !is_open) suppress = now; /* what closed it (and anything else down) stays hidden until released */
    pthread_mutex_unlock(&lock);
}

int n64c_menu_is_open(void)
{
    pthread_mutex_lock(&lock);
    const int open = is_open;
    pthread_mutex_unlock(&lock);
    return open;
}

unsigned n64c_menu_current(void)
{
    pthread_mutex_lock(&lock);
    const unsigned now = menus;
    pthread_mutex_unlock(&lock);
    return now;
}

static const char *const mode_labels[] = {"WINDOWED", "BORDERLESS", "FULLSCREEN"};

size_t n64c_menu_text(char *buf, size_t cap, int *selected_line, int *scale)
{
    pthread_mutex_lock(&lock);
    size_t n = 0;
    *selected_line = -1;
    if (!is_open) { /* inert while closed: nothing drawn */
        pthread_mutex_unlock(&lock);
        return n >= cap ? cap - 1 : n;
    }
    *scale = 3;
    n = (size_t)snprintf(buf, cap, "OPTIONS\n\n");
    for (int i = 0; i < row_count() && n < cap; i++) {
        char value[48] = "";
        const char *label = "";
        switch (row_at(i)) {
        case ROW_RESUME: label = "RESUME"; break;
        case ROW_WINDOW: label = "WINDOW MODE"; snprintf(value, sizeof value, "< %s >", mode_labels[opts.window_mode]); break;
        case ROW_RESOLUTION:
            label = "RESOLUTION";
            snprintf(value, sizeof value, "< %ux%u >", opts.width, opts.height);
            break;
        case ROW_SCALE: label = "RENDER SCALE"; snprintf(value, sizeof value, "< %d%% >", opts.render_scale); break;
        case ROW_UNLOCK: label = "FRAMERATE UNLOCK"; snprintf(value, sizeof value, "< %s >", opts.fps_unlock ? "ON" : "OFF"); break;
        case ROW_QUIT: label = quit_armed ? "PRESS A AGAIN TO QUIT" : "QUIT GAME"; break;
        }
        if (i == selected) *selected_line = 2 + i;
        n += (size_t)snprintf(buf + n, cap - n, "%s%s%s%s\n", i == selected ? "> " : "  ", label, *value ? "  " : "", value);
    }
    if (n < cap) n += (size_t)snprintf(buf + n, cap - n, "\nUP/DOWN SELECT  LEFT/RIGHT CHANGE\nA OK  B BACK");
    pthread_mutex_unlock(&lock);
    return n >= cap ? cap - 1 : n;
}

unsigned n64c_menu_take(N64cOptions *now)
{
    pthread_mutex_lock(&lock);
    const unsigned what = todo;
    todo = 0;
    *now = opts;
    pthread_mutex_unlock(&lock);
    return what;
}
