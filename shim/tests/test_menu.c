/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The options screen: settings file round trip, opening only inside a game menu, the pad the game sees while it is open
 * (neutral), navigation and the changes it asks the host for, and the two-step quit. */
#include "../n64c_menu.h"
#include "test_check.h"
#include <string.h>

enum { A = 0x8000, B = 0x4000, UP = 0x0800, DOWN = 0x0400, LEFT = 0x0200, RIGHT = 0x0100, L = 0x0020, R = 0x0010 };

/* One input sample with the given buttons; returns the pad the game would read. */
static OSContPad sample(unsigned buttons, int hotkey)
{
    OSContPad pad = {.button = (u16)buttons};
    n64c_menu_filter(&pad, hotkey);
    return pad;
}
static void tap(unsigned button)
{
    (void)sample(button, 0);
    (void)sample(0, 0);
}
static void start(const char *path, int mobile)
{
    static const u32 sizes[3][2] = {{1920, 1080}, {1280, 720}, {640, 480}};
    N64cOptions o;
    n64c_options_default(&o);
    n64c_menu_configure(&o, path, sizes, 3, mobile);
}

int main(void)
{
    const char *path = "n64c_test_menu.cfg";
    remove(path);

    /* file: defaults, round trip, bad values keep the default */
    N64cOptions o, back;
    n64c_options_default(&o);
    Test_Check(!n64c_options_load(path, &o), "no file is not an error and changes nothing");
    o.window_mode = N64C_WIN_FULLSCREEN; o.width = 1280; o.height = 720; o.fps_unlock = 1; o.render_scale = 50;
    Test_Check(n64c_options_save(path, &o), "save");
    n64c_options_default(&back);
    Test_Check(n64c_options_load(path, &back), "load");
    Test_Check(!memcmp(&o, &back, sizeof o), "the file keeps every option");
    FILE *f = fopen(path, "w");
    Test_Check(f != NULL, "write the bad file");
    fputs("window_mode=sideways\nwidth=7\nheight=abc\nfps_unlock=1\nrender_scale=500\n", f);
    fclose(f);
    n64c_options_default(&back);
    (void)n64c_options_load(path, &back);
    Test_Check(back.window_mode == N64C_WIN_WINDOWED && back.width == 640 && back.height == 480 && back.render_scale == 100,
               "invalid values are ignored");
    Test_Check(back.fps_unlock == 1, "a valid line beside invalid ones still applies");

    /* the screen opens only inside a game menu */
    remove(path);
    start(path, 0);
    OSContPad pad = sample(L | R, 0);
    Test_Check(!n64c_menu_is_open() && pad.button == (L | R), "L+R outside a game menu belongs to the game");
    (void)sample(0, 0);
    n64c_menu_context(N64C_MENU_PAUSE);
    char text[512];
    int line, scale;
    Test_Check(n64c_menu_text(text, sizeof text, &line, &scale) == 0, "a closed screen draws nothing, even inside a game menu");
    pad = sample(L | R, 0);
    Test_Check(n64c_menu_is_open(), "L+R inside the pause menu opens it");
    Test_Check(pad.button == 0 && pad.stick_x == 0 && pad.stick_y == 0, "the game reads a neutral pad while it is open");
    (void)sample(0, 0);
    pad = sample(L | 0x0002, 0); /* L and C-down: not navigation */
    Test_Check(pad.button == 0, "buttons do not reach the game while it is open");
    (void)sample(0, 0);

    /* navigation: rows are resume, window mode, resolution, unlock, quit (desktop) */
    Test_Check(n64c_menu_text(text, sizeof text, &line, &scale) > 0 && line == 2, "resume is highlighted first");
    tap(DOWN);
    tap(RIGHT);
    N64cOptions now;
    unsigned what = n64c_menu_take(&now);
    Test_Check((what & N64C_DO_WINDOW) && now.window_mode == N64C_WIN_BORDERLESS, "right on window mode asks for borderless");
    tap(DOWN);
    tap(RIGHT);
    what = n64c_menu_take(&now);
    Test_Check((what & N64C_DO_WINDOW) && now.width == 1920 && now.height == 1080, "right on resolution takes the next display size");
    tap(DOWN);
    tap(A);
    what = n64c_menu_take(&now);
    Test_Check((what & N64C_DO_UNLOCK) && now.fps_unlock == 1, "A on the unlock row switches it on");
    Test_Check(n64c_menu_take(&now) == 0, "a change is reported once");
    n64c_options_default(&back);
    Test_Check(n64c_options_load(path, &back) && back.fps_unlock == 1 && back.window_mode == N64C_WIN_BORDERLESS,
               "every change is saved to the file at once");

    /* quit asks twice */
    tap(DOWN);
    tap(A);
    Test_Check(n64c_menu_take(&now) == 0, "the first A on quit only arms it");
    Test_Check(n64c_menu_text(text, sizeof text, &line, &scale) > 0 && strstr(text, "AGAIN"), "the row asks for a second press");
    tap(A);
    Test_Check((n64c_menu_take(&now) & N64C_DO_QUIT) && !n64c_menu_is_open(), "the second A quits");

    /* closing: a button held through the close is not seen by the game until released */
    start(path, 0);
    n64c_menu_context(N64C_MENU_MAIN);
    (void)sample(0, 1); /* the keyboard hotkey opens too */
    Test_Check(n64c_menu_is_open(), "the hotkey opens it");
    (void)sample(0, 0);
    (void)sample(B, 0); /* B closes, still held */
    Test_Check(!n64c_menu_is_open(), "B closes it");
    pad = sample(B, 0);
    Test_Check(pad.button == 0, "the held B is hidden from the game");
    pad = sample(0, 0);
    pad = sample(B, 0);
    Test_Check(pad.button == B, "a new B press reaches the game");

    /* a phone has render scale instead of window mode and resolution */
    start(path, 1);
    n64c_menu_context(N64C_MENU_MAIN);
    (void)sample(L | R, 0);
    (void)sample(0, 0);
    tap(DOWN);
    tap(LEFT);
    what = n64c_menu_take(&now);
    Test_Check((what & N64C_DO_SCALE) && now.render_scale == 75, "left on render scale lowers it a step");

    /* leaving the game menu closes the screen and a closed screen passes the pad */
    n64c_menu_context(0);
    pad = sample(L | R, 0);
    (void)pad;
    remove(path);
    puts("menu ok");
    return 0;
}
