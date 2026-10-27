// SPDX-License-Identifier: GPL-3.0-or-later
// The Controls page and touch zones drawn into a real context (desktop GL or OpenGL ES, argv[1] "gl" / "es") on the
// offscreen video driver: pixels are read back, and the GL state around the draw must come back unchanged.
#include "../fast3d/f3d_controls_overlay.h"
#include "../n64c_controls.h"
#include "../n64c_touch.h"
#include <SDL3/SDL_opengl.h>
#include <cstdio>
#include <cstring>
#include <vector>

static int failures, checks;
#define CHECK(c) do { ++checks; if (!(c)) { ++failures; std::fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #c); } } while (0)
constexpr int kW = 640, kH = 480;
static void (*clearColor)(GLfloat, GLfloat, GLfloat, GLfloat);
static void (*clearBuffers)(GLbitfield);
static void (*readPixels)(GLint, GLint, GLsizei, GLsizei, GLenum, GLenum, void *);
static void (*getIntegerv)(GLenum, GLint *);
static GLenum (*getError)();
static void (*enable)(GLenum);
static void (*disable)(GLenum);
static GLboolean (*isEnabled)(GLenum);
static void (*viewport)(GLint, GLint, GLsizei, GLsizei);
static void (*blendFunc)(GLenum, GLenum);
static void (*activeTexture)(GLenum);
template <typename T> static void get(T &fn, const char *name) { fn = reinterpret_cast<T>(SDL_GL_GetProcAddress(name)); }

struct Shot { std::vector<unsigned char> px = std::vector<unsigned char>(kW * kH * 4); const unsigned char *at(int x, int y) const { return &px[(y * kW + x) * 4]; } };
static Shot frame(float r, float g, float b)
{
    clearColor(r, g, b, 1.f);
    clearBuffers(GL_COLOR_BUFFER_BIT);
    N64c::DrawControlsOverlay(SDL_GL_GetCurrentWindow());
    Shot s;
    readPixels(0, 0, kW, kH, GL_RGBA, GL_UNSIGNED_BYTE, s.px.data());
    return s;
}
template <typename F> static int count(const Shot &s, F match)
{
    int n = 0;
    for (int i = 0; i < kW * kH; ++i) n += match(&s.px[i * 4]);
    return n;
}

int main(int argc, char **argv)
{
    const bool es = argc > 1 && !std::strcmp(argv[1], "es");
    if (!SDL_Init(SDL_INIT_VIDEO)) { std::fprintf(stderr, "SDL_Init: %s\n", SDL_GetError()); return 1; }
    if (es) {
        SDL_GL_SetAttribute(SDL_GL_CONTEXT_PROFILE_MASK, SDL_GL_CONTEXT_PROFILE_ES);
        SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION, 3);
        SDL_GL_SetAttribute(SDL_GL_CONTEXT_MINOR_VERSION, 0);
    }
    SDL_Window *window = SDL_CreateWindow("overlay", kW, kH, SDL_WINDOW_OPENGL);
    SDL_GLContext context = window ? SDL_GL_CreateContext(window) : nullptr;
    if (!context) { std::fprintf(stderr, "%s context: %s\n", es ? "ES" : "GL", SDL_GetError()); return 1; }
    get(clearColor, "glClearColor"); get(clearBuffers, "glClear"); get(readPixels, "glReadPixels");
    get(getIntegerv, "glGetIntegerv"); get(getError, "glGetError"); get(enable, "glEnable"); get(disable, "glDisable");
    get(isEnabled, "glIsEnabled"); get(viewport, "glViewport"); get(blendFunc, "glBlendFunc"); get(activeTexture, "glActiveTexture");
    int profile = 0;
    SDL_GL_GetAttribute(SDL_GL_CONTEXT_PROFILE_MASK, &profile);
    CHECK(((profile & SDL_GL_CONTEXT_PROFILE_ES) != 0) == es);
    const auto getString = reinterpret_cast<const unsigned char *(*)(GLenum)>(SDL_GL_GetProcAddress("glGetString"));
    std::printf("context: %s\n", getString(GL_VERSION));

    // Page closed, no finger: nothing is drawn.
    Shot closed = frame(0.f, 0.5f, 0.f);
    CHECK(count(closed, [](const unsigned char *p) { return p[0] != 0 || p[1] < 120 || p[1] > 135 || p[2] != 0; }) == 0);

    // Touch zones: discs over a black frame, state around the draw unchanged.
    n64c_touch_down(1, 0.5f, 0.5f);
    n64c_touch_up(1);
    enable(GL_DEPTH_TEST); disable(GL_BLEND); viewport(3, 5, 100, 60); blendFunc(GL_SRC_COLOR, GL_DST_COLOR); activeTexture(GL_TEXTURE1);
    Shot touch = frame(0.f, 0.f, 0.f);
    GLint vp[4] = {0}, unit = 0, src = 0, dst = 0;
    getIntegerv(GL_VIEWPORT, vp); getIntegerv(GL_ACTIVE_TEXTURE, &unit);
    getIntegerv(GL_BLEND_SRC_RGB, &src); getIntegerv(GL_BLEND_DST_RGB, &dst);
    CHECK(isEnabled(GL_DEPTH_TEST) && !isEnabled(GL_BLEND));
    CHECK(vp[0] == 3 && vp[1] == 5 && vp[2] == 100 && vp[3] == 60);
    CHECK(unit == (GLint)GL_TEXTURE1 && src == (GLint)GL_SRC_COLOR && dst == (GLint)GL_DST_COLOR);
    CHECK(getError() == GL_NO_ERROR);
    activeTexture(GL_TEXTURE0); viewport(0, 0, kW, kH); disable(GL_DEPTH_TEST);
    const int lit = count(touch, [](const unsigned char *p) { return p[0] > 30 && p[1] > 30; });
    CHECK(lit > 2000); // 11 zones, each a translucent disc
    const N64cTouchLayout *layout = n64c_touch_layout();
    const N64cTouchZone &a = layout->zone[N64C_ACT_A];
    const unsigned char *centre = touch.at((int)(a.x * kW), (int)((1.f - a.y) * kH - 1)); // GL rows run bottom up
    CHECK(centre[0] > 30 && centre[1] > 30);

    // The page: dims a red frame, and its title (yellow, not in the frame) is drawn from the font texture.
    n64c_controls_open();
    Shot page = frame(1.f, 0.f, 0.f);
    CHECK(page.at(2, 2)[0] < 120); // dimmed and under the page background
    CHECK(count(page, [](const unsigned char *p) { return p[0] > 200 && p[1] > 150 && p[2] < 150; }) > 50);
    CHECK(count(page, [](const unsigned char *p) { return p[2] > 100 && p[0] < 100; }) > 500); // the cursor bar
    CHECK(getError() == GL_NO_ERROR);
    n64c_controls_close();

    std::printf("%s: %d checks, %d failures\n", es ? "es" : "gl", checks, failures);
    SDL_GL_DestroyContext(context);
    SDL_DestroyWindow(window);
    SDL_Quit();
    return failures != 0;
}
