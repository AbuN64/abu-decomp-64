// SPDX-License-Identifier: GPL-3.0-or-later
// The display apply on a real window and render target (SDL's offscreen video driver, software GL): a request changes
// the window's pixels and the size the game renders at, a refused one leaves both exactly as they were with the reason
// returned, and the options screen shows that reason and keeps the prior options. The host's apply here is the real
// n64c_f3d_display; only its config is a variable.
#include "n64c_f3d.h"
#include "n64c_menu.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>

static void check(bool ok, const char* what)
{
    if (!ok) {
        std::fprintf(stderr, "FAIL: %s\n", what);
        std::exit(EXIT_FAILURE);
    }
}

static N64cF3d* f3d;
static int records;
static N64cOptions stored;

static void expect_sizes(uint32_t window_w, uint32_t window_h, uint32_t render_w, uint32_t render_h, const char* what)
{
    uint32_t ww, wh, rw, rh;
    n64c_f3d_sizes(f3d, &ww, &wh, &rw, &rh);
    if (ww != window_w || wh != window_h || rw != render_w || rh != render_h) {
        std::fprintf(stderr, "FAIL: %s: window %ux%u render %ux%u, expected window %ux%u render %ux%u\n", what, ww, wh, rw, rh,
                     window_w, window_h, render_w, render_h);
        std::exit(EXIT_FAILURE);
    }
}

static bool host_load(void*, N64cOptions*) { return false; }
static bool host_request(void*, const N64cOptions* requested, N64cDisplaySettings* effective, char* error, size_t capacity)
{
    int scale = requested->display.render_scale;
    if (!n64c_f3d_display(f3d, requested->display.fullscreen, requested->display.width, requested->display.height, scale,
                          &scale, error, capacity))
        return false;
    effective->render_scale = scale;
    stored = *requested;
    stored.display = *effective;
    records++;
    return true;
}

static void tap(unsigned button)
{
    OSContPad pad = {};
    pad.button = (u16)button;
    n64c_menu_filter(&pad, 0);
    pad = {};
    n64c_menu_filter(&pad, 0);
}

int main(int argc, char** argv)
{
    check(argc == 2, "usage: test_display SETTINGS_JSON");
    const N64cF3dConfig config = {nullptr, argv[1], 640, 480, 284, 222, 1, 1};
    f3d = n64c_f3d_open(&config);
    check(f3d != nullptr, "the offscreen window opens");
    expect_sizes(640, 480, 640, 480, "start");

    char error[160] = "";
    int effective = 0;
    const bool applied = n64c_f3d_display(f3d, 0, 800, 600, 50, &effective, error, sizeof error);
    if (!applied) std::fprintf(stderr, "apply error: %s\n", error);
    check(applied && effective == 50, "a display change applies");
    expect_sizes(800, 600, 400, 300, "after the change the window and the render target are the new size");

    // A scale the renderer refuses comes after the window already took its new size: all of it is undone.
    error[0] = 0;
    check(!n64c_f3d_display(f3d, 0, 1024, 768, 10, &effective, error, sizeof error), "a refused scale fails the apply");
    check(std::strstr(error, "render scale 10%") != nullptr, "the reason is returned");
    expect_sizes(800, 600, 400, 300, "a refused apply rolls the window and the render target back");
    error[0] = 0;
    check(!n64c_f3d_display(f3d, 7, 1024, 768, 100, &effective, error, sizeof error) && std::strstr(error, "window mode 7"),
          "an unknown window mode is refused with its reason");
    expect_sizes(800, 600, 400, 300, "and changes nothing");
    check(n64c_f3d_display(f3d, 0, 640, 480, 100, &effective, error, sizeof error) && effective == 100, "back to the start");
    expect_sizes(640, 480, 640, 480, "start again");

    // The options screen over the real window: the second size is one no window can have.
    static const u32 sizes[2][2] = {{1024, 768}, {0, 0}};
    check(n64c_settings_bind(nullptr, host_load, host_request), "the host binds");
    N64cOptions options;
    n64c_options_default(&options);
    n64c_menu_configure(&options, sizes, 2, 0);
    n64c_menu_context(N64C_MENU_PAUSE);
    OSContPad pad = {};
    pad.button = 0x0020 | 0x0010; // L+R opens
    n64c_menu_filter(&pad, 0);
    pad = {};
    n64c_menu_filter(&pad, 0);
    check(n64c_menu_is_open(), "the screen opens");
    tap(0x0400);
    tap(0x0400); // resolution
    tap(0x0100); // the first size
    N64cOptions now;
    check((n64c_menu_take(&now) & N64C_DO_WINDOW) && now.display.width == 1024 && records == 1, "the screen's change reaches the real window");
    expect_sizes(1024, 768, 1024, 768, "the window has the size the screen asked for");
    tap(0x0100); // the second size: refused
    check((n64c_menu_take(&now) & N64C_DO_WINDOW) == 0 && now.display.width == 1024 && now.display.height == 768 && records == 1,
          "a refused change keeps the prior options and records nothing");
    expect_sizes(1024, 768, 1024, 768, "and the window as it was");
    char text[640];
    int line, scale;
    check(n64c_menu_text(text, sizeof text, &line, &scale) > 0 && std::strstr(text, "WINDOW SIZE 0X0 IS EMPTY") != nullptr,
          "the window's reason is on the screen");
    tap(0x0400); // render scale
    tap(0x0200); // down a step
    check((n64c_menu_take(&now) & N64C_DO_SCALE) && now.display.render_scale == 75 && records == 2, "the scale change is applied");
    expect_sizes(1024, 768, 768, 576, "and the render target is the scaled size");
    n64c_settings_unbind();
    n64c_f3d_close(f3d);
    std::puts("display ok");
    return 0;
}
