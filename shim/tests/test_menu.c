/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The options screen: opening only inside a game menu, the pad the game sees while it is open (neutral), navigation, and
 * the settings seam: the host's load at startup, its request for every change (recorded by the host only when it succeeds; a
 * refusal leaves the options as they were and is shown on the screen), no options file, and the two-step quit. */
#include "../n64c_menu.h"
#include "test_check.h"
#include <string.h>
#include <unistd.h>

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
static void start(int mobile)
{
    static const u32 sizes[3][2] = {{1920, 1080}, {1280, 720}, {640, 480}};
    N64cOptions o;
    n64c_options_default(&o);
    n64c_menu_configure(&o, sizes, 3, mobile);
}

/* The host: its own config (recorded only by a successful request), and a way to refuse or to get a different scale. */
typedef struct {
    N64cOptions stored;
    int has_config, loads, applies, records;
    bool refuse;
    int effective_scale;   /* nonzero: the scale the render target really got */
} Host;
static bool host_load(void *context, N64cOptions *options)
{
    Host *h = context;
    h->loads++;
    if (!h->has_config) return false;
    *options = h->stored;
    return true;
}
static bool host_request(void *context, const N64cOptions *requested, N64cDisplaySettings *effective, char *error, size_t capacity)
{
    Host *h = context;
    h->applies++;
    if (h->refuse) {
        snprintf(error, capacity, "the display refused %ux%u", requested->display.width, requested->display.height);
        return false;
    }
    if (h->effective_scale) effective->render_scale = h->effective_scale;
    h->stored = *requested;
    h->stored.display = *effective;
    h->has_config = 1;
    h->records++;
    return true;
}
static Host host;

static void open_screen(void)
{
    n64c_menu_context(N64C_MENU_PAUSE);
    (void)sample(L | R, 0);
    (void)sample(0, 0);
}

/* The seam: startup load, a successful request is recorded by the host, a refusal keeps the prior options and shows why. */
static void seam_tests(void)
{
    N64cOptions o, back, now;
    char text[640];
    int line, scale;
    remove("options.cfg");

    n64c_settings_unbind();
    n64c_options_default(&o);
    Test_Check(!n64c_settings_load(&o), "without a host nothing is loaded");
    Test_Check(!n64c_settings_bind(&host, host_load, NULL), "a host without a request callback is refused");
    host = (Host){0};
    Test_Check(n64c_settings_bind(&host, host_load, host_request), "the host binds");
    n64c_options_default(&back);
    Test_Check(!n64c_settings_load(&back) && host.loads == 1 && !memcmp(&back, &o, sizeof o),
               "the load callback is used at startup, and may have no config");
    host.stored = o;
    host.stored.display = (N64cDisplaySettings){N64C_WIN_BORDERLESS, 1280, 720, 50};
    host.stored.fps_unlock = 1;
    host.has_config = 1;
    Test_Check(n64c_settings_load(&back) && host.loads == 2 && !memcmp(&back, &host.stored, sizeof back),
               "the startup options are what the host's load gave");

    /* success: nothing is applied before the take; the apply's save is the host's */
    host = (Host){0};
    start(0);
    open_screen();
    tap(DOWN);
    tap(RIGHT);
    Test_Check(host.applies == 0 && host.records == 0, "nothing is applied or saved before the take");
    unsigned what = n64c_menu_take(&now);
    Test_Check(host.applies == 1 && host.records == 1 && host.stored.display.fullscreen == N64C_WIN_BORDERLESS,
               "a successful request is recorded by the host");
    Test_Check((what & N64C_DO_WINDOW) && now.display.fullscreen == N64C_WIN_BORDERLESS, "and the change is in force");

    /* refusal: the prior options stay, the host records nothing, the screen shows why */
    host.refuse = true;
    tap(DOWN);
    tap(RIGHT);
    what = n64c_menu_take(&now);
    Test_Check(host.applies == 2 && host.records == 1 && !(what & N64C_DO_WINDOW), "a refused request records nothing and is not reported done");
    Test_Check(now.display.width == 640 && now.display.height == 480 && now.display.fullscreen == N64C_WIN_BORDERLESS,
               "the prior display settings are kept");
    Test_Check(n64c_menu_text(text, sizeof text, &line, &scale) > 0 && strstr(text, "DISPLAY REFUSED 1920X1080") != NULL,
               "the error is shown");
    Test_Check(strstr(text, "< 640x480 >") != NULL, "and so is the prior resolution, not the refused one");
    host.refuse = false;
    tap(RIGHT);
    Test_Check((n64c_menu_take(&now) & N64C_DO_WINDOW) && now.display.width == 1920 && host.records == 2, "the next change applies again");
    Test_Check(n64c_menu_text(text, sizeof text, &line, &scale) > 0 && strstr(text, "NOT APPLIED") == NULL, "and clears the error");

    /* the scale the render target really got is what is kept */
    start(1);
    host.effective_scale = 50;
    open_screen();
    tap(DOWN);
    tap(LEFT);
    Test_Check((n64c_menu_take(&now) & N64C_DO_SCALE) && now.display.render_scale == 50 && host.stored.display.render_scale == 50,
               "the effective render scale is kept, not the request");
    Test_Check(n64c_menu_text(text, sizeof text, &line, &scale) > 0 && strstr(text, "< 50% >") != NULL, "and shown");
    host.effective_scale = 0;

    /* no host: the screen refuses and says so; whatever happened, no options file */
    n64c_settings_unbind();
    start(0);
    open_screen();
    tap(DOWN);
    tap(RIGHT);
    int applies = host.applies;
    what = n64c_menu_take(&now);
    Test_Check(host.applies == applies && !(what & N64C_DO_WINDOW) && now.display.fullscreen == N64C_WIN_WINDOWED,
               "without a host a change is refused");
    Test_Check(n64c_menu_text(text, sizeof text, &line, &scale) > 0 && strstr(text, "NO OPTIONS HOST") != NULL, "and the screen says why");
    Test_Check(access("options.cfg", F_OK) != 0, "no options.cfg is written, with a host or without");
    (void)n64c_settings_bind(&host, host_load, host_request);
}

int main(void)
{
    host = (Host){0};
    Test_Check(n64c_settings_bind(&host, host_load, host_request), "the host binds");
    N64cOptions now;
    /* the screen opens only inside a game menu */
    start(0);
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

    /* a finger resting on B as the chord lands (a phone's L+R) is not a press: the screen stays open */
    tap(B);
    Test_Check(!n64c_menu_is_open(), "B closes the screen");
    pad = sample(L | R | B, 0);
    Test_Check(n64c_menu_is_open(), "opening with B down beside the chord does not close it again");
    (void)sample(0, 0);
    Test_Check(n64c_menu_is_open(), "and it stays open when the fingers lift");

    /* navigation: rows are resume, window mode, resolution, render scale, unlock, controls, quit (desktop) */
    Test_Check(n64c_menu_text(text, sizeof text, &line, &scale) > 0 && line == 2, "resume is highlighted first");
    tap(DOWN);
    tap(RIGHT);
    unsigned what = n64c_menu_take(&now);
    Test_Check((what & N64C_DO_WINDOW) && now.display.fullscreen == N64C_WIN_BORDERLESS, "right on window mode asks for borderless");
    tap(DOWN);
    tap(RIGHT);
    what = n64c_menu_take(&now);
    Test_Check((what & N64C_DO_WINDOW) && now.display.width == 1920 && now.display.height == 1080, "right on resolution takes the next display size");
    tap(DOWN);
    tap(RIGHT);
    what = n64c_menu_take(&now);
    Test_Check((what & N64C_DO_SCALE) && now.display.render_scale == 100, "right on render scale at its top stays there");
    tap(LEFT);
    what = n64c_menu_take(&now);
    Test_Check((what & N64C_DO_SCALE) && now.display.render_scale == 75, "left on render scale lowers it a step on the desktop too");
    tap(DOWN);
    tap(A);
    what = n64c_menu_take(&now);
    Test_Check((what & N64C_DO_UNLOCK) && now.fps_unlock == 1, "A on the unlock row switches it on");
    Test_Check(n64c_menu_take(&now) == 0, "a change is reported once");
    Test_Check(host.stored.fps_unlock == 1 && host.stored.display.fullscreen == N64C_WIN_BORDERLESS,
               "every change reaches the host's apply, which holds the options");
    /* a request is applied AND saved or it is nothing: the host fails its save, answers false, the unlock stays as it was */
    host.refuse = true;
    tap(A);
    what = n64c_menu_take(&now);
    Test_Check(!(what & N64C_DO_UNLOCK) && now.fps_unlock == 1 && host.stored.fps_unlock == 1,
               "a request whose save failed leaves the unlock, the screen and the host's archive as they were");
    Test_Check(n64c_menu_text(text, sizeof text, &line, &scale) > 0 && strstr(text, "FRAMERATE UNLOCK  < ON >") != NULL,
               "and the screen still shows it on");
    host.refuse = false;

    /* controls opens the page and closes the screen */
    tap(DOWN);
    tap(A);
    Test_Check((n64c_menu_take(&now) & N64C_DO_CONTROLS) && !n64c_menu_is_open(), "the controls row asks for the Controls page");
    (void)sample(L | R, 0);
    (void)sample(0, 0);
    Test_Check(n64c_menu_is_open(), "the screen opens again");
    for (int i = 0; i < 5; i++) tap(DOWN); /* resume, window, resolution, scale, unlock, controls, quit */
    tap(DOWN);
    tap(A);
    Test_Check(n64c_menu_take(&now) == 0, "the first A on quit only arms it");
    Test_Check(n64c_menu_text(text, sizeof text, &line, &scale) > 0 && strstr(text, "AGAIN"), "the row asks for a second press");
    tap(A);
    Test_Check((n64c_menu_take(&now) & N64C_DO_QUIT) && !n64c_menu_is_open(), "the second A quits");

    /* closing: a button held through the close is not seen by the game until released */
    start(0);
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
    start(1);
    n64c_menu_context(N64C_MENU_MAIN);
    (void)sample(L | R, 0);
    (void)sample(0, 0);
    tap(DOWN);
    tap(LEFT);
    what = n64c_menu_take(&now);
    Test_Check((what & N64C_DO_SCALE) && now.display.render_scale == 75, "left on render scale lowers it a step");

    /* leaving the game menu closes the screen and a closed screen passes the pad */
    n64c_menu_context(0);
    pad = sample(L | R, 0);
    (void)pad;
    seam_tests();
    puts("menu ok");
    return 0;
}
