// SPDX-License-Identifier: GPL-3.0-or-later
// n64c_f3d: wiring of the pinned Fast3D interpreter for the port (see n64c_f3d.h). The interpreter
// takes its collaborators by injection (Interpreter::Init): the port's window, LUS's OpenGL backend,
// a console-variable store and a resource manager that holds only Fast3D's shader archive. Colour
// images become Fast3D framebuffers through n64c_dl's colour-image hook (OTR_G_SETFB after each
// G_SETCIMG); the root list runs inside [G_DL root][OTR_G_RESETFB][G_ENDDL] so no framebuffer stays bound.
#include "n64c_f3d.h"
#include "f3d_window.h"
#include "fast/interpreter.h"
#include "fast/backends/gfx_opengl.h"
#include "fast/debug/GfxDebugger.h"
#include "ship/config/Config.h"
#include "ship/config/ConsoleVariable.h"
#include "ship/resource/ResourceManager.h"
#include "ship/thread/ThreadPool.h"
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <SDL3/SDL_hints.h>
#include <SDL3/SDL_opengl.h>
#include <cstdio>
#include <algorithm>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <map>
#include <memory>
#include <string>
#include <vector>

namespace Fast {
void GfxSetInstance(std::shared_ptr<Interpreter> gfx);
}

struct N64cF3d {
    N64cF3dConfig config;
    std::shared_ptr<Ship::Config> settings;
    std::shared_ptr<Ship::ConsoleVariable> cvars;
    std::shared_ptr<Ship::ResourceManager> resources;
    std::shared_ptr<Fast::Interpreter> interpreter;
    N64c::F3dWindow* window = nullptr;            // the interpreter keeps raw pointers to both;
    Fast::GfxRenderingAPIOGL* renderer = nullptr; // they are freed after it in n64c_f3d_close
    N64cDlArena arena = {};
    std::map<uint32_t, int> surfaces;           // colour image physical address -> Fast3D framebuffer
    uint32_t surface = 0;                       // latest colour image bound by the latest run
    uintptr_t wrapper[6] = {};
    std::string gl_renderer;
};
// n64c_dl hook: the framebuffer of this colour image, created on first use at the game's size.
static int color_image(void* user, uint32_t phys, uint32_t width, uintptr_t extra[2])
{
    auto* f3d = static_cast<N64cF3d*>(user);
    auto it = f3d->surfaces.find(phys);
    if (it == f3d->surfaces.end()) {
        const uint32_t h = f3d->config.fb_h;
        const int fb = f3d->interpreter->CreateFrameBuffer(width, h, width, h, 1);
        it = f3d->surfaces.emplace(phys, fb).first;
    }
    f3d->surface = phys;
    extra[0] = (uintptr_t)(uint8_t)Fast::OTR_G_SETFB << 24;
    extra[1] = (uintptr_t)it->second;
    return 1;
}

// SDL's offscreen driver takes the first EGL device that initialises; pick Mesa's software device
// instead so results do not depend on the host GPU.
static void prefer_software_device()
{
    auto query = (PFNEGLQUERYDEVICESEXTPROC)eglGetProcAddress("eglQueryDevicesEXT");
    auto query_string = (PFNEGLQUERYDEVICESTRINGEXTPROC)eglGetProcAddress("eglQueryDeviceStringEXT");
    EGLDeviceEXT devices[16];
    EGLint count = 0;
    if (!query || !query_string || !query(16, devices, &count))
        return;
    for (EGLint i = 0; i < count; i++) {
        const char* extensions = query_string(devices[i], EGL_EXTENSIONS);
        if (extensions && std::strstr(extensions, "EGL_MESA_device_software")) {
            SDL_SetHint("SDL_HINT_EGL_DEVICE", std::to_string(i).c_str());
            return;
        }
    }
}

extern "C" N64cF3d* n64c_f3d_open(const N64cF3dConfig* config)
{
    if (!config || !config->window_w || !config->window_h || !config->fb_w || !config->fb_h)
        return nullptr;
    auto f3d = std::make_unique<N64cF3d>();
    f3d->config = *config;
    std::string shaders = config->shaders ? config->shaders : "", settings = config->settings ? config->settings : "";
    try {
        const std::filesystem::path exe = std::filesystem::canonical("/proc/self/exe").parent_path();
        if (shaders.empty())
            shaders = (exe / "fast3d-shaders.o2r").string();
        if (settings.empty())
            settings = (exe / "fast3d-settings.json").string();
        f3d->settings = std::make_shared<Ship::Config>(settings);
        f3d->cvars = std::make_shared<Ship::ConsoleVariable>(f3d->settings);
        // The resource manager loads through a thread pool even for synchronous loads.
        f3d->resources = std::make_shared<Ship::ResourceManager>(std::make_shared<Ship::ThreadPool>(1));
        f3d->resources->Init(nlohmann::json{{"archivePaths", std::vector<std::string>{shaders}}});
    } catch (const std::exception& e) {
        std::fprintf(stderr, "n64c_f3d: %s: %s\n", shaders.c_str(), e.what());
        return nullptr;
    }
    if (config->offscreen)
        SDL_SetHint(SDL_HINT_VIDEO_DRIVER, "offscreen");
    if (config->software)
        prefer_software_device();
    f3d->interpreter = std::make_shared<Fast::Interpreter>();
    Fast::GfxSetInstance(f3d->interpreter);
    f3d->interpreter->SetGfxDebugger(std::make_shared<Fast::GfxDebugger>());
    f3d->window = new N64c::F3dWindow();
    f3d->renderer = new Fast::GfxRenderingAPIOGL(f3d->cvars, f3d->resources);
    f3d->interpreter->Init(f3d->window, f3d->renderer, "Rage Wars", false, config->window_w, config->window_h, 0, 0,
                           f3d->cvars, f3d->resources);
    // The VI scales the game's colour image onto the screen: map that size onto the window.
    f3d->interpreter->mNativeDimensions.width = config->fb_w;
    f3d->interpreter->mNativeDimensions.height = config->fb_h;
    const char* name = (const char*)glGetString(GL_RENDERER);
    f3d->gl_renderer = name ? name : "";
    return f3d.release();
}

extern "C" int n64c_f3d_run(N64cF3d* f3d, const N64cGfxMem* mem, uint32_t dl, N64cDlStats* stats)
{
    n64c_dl_arena_reset(&f3d->arena);
    f3d->surface = 0;
    const N64cDlHooks hooks = {color_image, f3d};
    const uintptr_t* root = n64c_dl_translate(mem, dl, &f3d->arena, stats, &hooks);
    if (!root)
        return -1;
    f3d->wrapper[0] = (uintptr_t)0xDE << 24; // G_DL (push)
    f3d->wrapper[1] = (uintptr_t)root;
    f3d->wrapper[2] = (uintptr_t)(uint8_t)Fast::OTR_G_RESETFB << 24;
    f3d->wrapper[4] = (uintptr_t)0xDF << 24; // G_ENDDL
    auto& gfx = *f3d->interpreter;
    gfx.StartFrame();
    const std::unordered_map<Mtx*, MtxF> no_replacements;
    gfx.Run((Gfx*)f3d->wrapper, no_replacements);
    // No flip here: a task draws into the game's colour images; the window shows what the VI presents.
    f3d->renderer->EndFrame();
    f3d->renderer->FinishRender();
    return (int)stats->commands;
}
extern "C" uint32_t n64c_f3d_surface(const N64cF3d* f3d) { return f3d->surface; }
extern "C" int n64c_f3d_read(N64cF3d* f3d, uint32_t phys, uint32_t* rgba, uint32_t w, uint32_t h)
{
    const auto it = f3d->surfaces.find(phys);
    if (it == f3d->surfaces.end() || !rgba || !w || !h)
        return 0;
    const auto info = f3d->interpreter->mFrameBuffers.find(it->second);
    if (info == f3d->interpreter->mFrameBuffers.end())
        return 0;
    const uint32_t sw = info->second.applied_width, sh = info->second.applied_height;
    std::vector<uint16_t> src((size_t)sw * sh);
    f3d->renderer->ReadFramebufferToCPU(it->second, sw, sh, src.data()); // RGBA 5551, top row first
    auto channel = [&](uint32_t x, uint32_t y, int c) {
        const uint16_t p = src[(size_t)y * sw + x];
        const uint32_t v = c == 3 ? (p & 1) * 255u : (p >> (11 - 5 * c) & 31);
        return c == 3 ? (float)v : (float)(v << 3 | v >> 2);
    };
    for (uint32_t y = 0; y < h; y++) {
        const float fy = std::max(0.0f, ((float)y + 0.5f) * sh / h - 0.5f);
        const uint32_t y0 = std::min((uint32_t)fy, sh - 1), y1 = std::min(y0 + 1, sh - 1);
        const float ty = fy - (float)y0;
        for (uint32_t x = 0; x < w; x++) {
            const float fx = std::max(0.0f, ((float)x + 0.5f) * sw / w - 0.5f);
            const uint32_t x0 = std::min((uint32_t)fx, sw - 1), x1 = std::min(x0 + 1, sw - 1);
            const float tx = fx - (float)x0;
            uint32_t out = 0;
            for (int c = 0; c < 4; c++) {
                const float top = channel(x0, y0, c) * (1 - tx) + channel(x1, y0, c) * tx;
                const float bottom = channel(x0, y1, c) * (1 - tx) + channel(x1, y1, c) * tx;
                out |= (uint32_t)std::lround(top * (1 - ty) + bottom * ty) << (8 * c);
            }
            rgba[(size_t)y * w + x] = out;
        }
    }
    return 1;
}
// The port's own 5x7 status font: printable ASCII 0x20-0x5f, one byte per row, bit 4 the leftmost
// column. Lower case is drawn upper case. Hand-drawn for this file.
static const uint8_t status_font[64][7] = {
    {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}, {0x04, 0x04, 0x04, 0x04, 0x04, 0x00, 0x04}, // space !
    {0x0a, 0x0a, 0x00, 0x00, 0x00, 0x00, 0x00}, {0x0a, 0x0a, 0x1f, 0x0a, 0x1f, 0x0a, 0x0a}, // " #
    {0x04, 0x0f, 0x14, 0x0e, 0x05, 0x1e, 0x04}, {0x18, 0x19, 0x02, 0x04, 0x08, 0x13, 0x03}, // $ %
    {0x0c, 0x12, 0x14, 0x08, 0x15, 0x12, 0x0d}, {0x04, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00}, // & '
    {0x02, 0x04, 0x08, 0x08, 0x08, 0x04, 0x02}, {0x08, 0x04, 0x02, 0x02, 0x02, 0x04, 0x08}, // ( )
    {0x00, 0x04, 0x15, 0x0e, 0x15, 0x04, 0x00}, {0x00, 0x04, 0x04, 0x1f, 0x04, 0x04, 0x00}, // * +
    {0x00, 0x00, 0x00, 0x00, 0x0c, 0x04, 0x08}, {0x00, 0x00, 0x00, 0x1f, 0x00, 0x00, 0x00}, // , -
    {0x00, 0x00, 0x00, 0x00, 0x00, 0x0c, 0x0c}, {0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x00}, // . /
    {0x0e, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0e}, {0x04, 0x0c, 0x04, 0x04, 0x04, 0x04, 0x0e}, // 0 1
    {0x0e, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1f}, {0x1f, 0x02, 0x04, 0x02, 0x01, 0x11, 0x0e}, // 2 3
    {0x02, 0x06, 0x0a, 0x12, 0x1f, 0x02, 0x02}, {0x1f, 0x10, 0x1e, 0x01, 0x01, 0x11, 0x0e}, // 4 5
    {0x06, 0x08, 0x10, 0x1e, 0x11, 0x11, 0x0e}, {0x1f, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08}, // 6 7
    {0x0e, 0x11, 0x11, 0x0e, 0x11, 0x11, 0x0e}, {0x0e, 0x11, 0x11, 0x0f, 0x01, 0x02, 0x0c}, // 8 9
    {0x00, 0x0c, 0x0c, 0x00, 0x0c, 0x0c, 0x00}, {0x00, 0x0c, 0x0c, 0x00, 0x0c, 0x04, 0x08}, // : ;
    {0x02, 0x04, 0x08, 0x10, 0x08, 0x04, 0x02}, {0x00, 0x00, 0x1f, 0x00, 0x1f, 0x00, 0x00}, // < =
    {0x08, 0x04, 0x02, 0x01, 0x02, 0x04, 0x08}, {0x0e, 0x11, 0x01, 0x02, 0x04, 0x00, 0x04}, // > ?
    {0x0e, 0x11, 0x01, 0x0d, 0x15, 0x15, 0x0e}, {0x0e, 0x11, 0x11, 0x11, 0x1f, 0x11, 0x11}, // @ A
    {0x1e, 0x11, 0x11, 0x1e, 0x11, 0x11, 0x1e}, {0x0e, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0e}, // B C
    {0x1c, 0x12, 0x11, 0x11, 0x11, 0x12, 0x1c}, {0x1f, 0x10, 0x10, 0x1e, 0x10, 0x10, 0x1f}, // D E
    {0x1f, 0x10, 0x10, 0x1e, 0x10, 0x10, 0x10}, {0x0e, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0f}, // F G
    {0x11, 0x11, 0x11, 0x1f, 0x11, 0x11, 0x11}, {0x0e, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0e}, // H I
    {0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0c}, {0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11}, // J K
    {0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1f}, {0x11, 0x1b, 0x15, 0x15, 0x11, 0x11, 0x11}, // L M
    {0x11, 0x11, 0x19, 0x15, 0x13, 0x11, 0x11}, {0x0e, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0e}, // N O
    {0x1e, 0x11, 0x11, 0x1e, 0x10, 0x10, 0x10}, {0x0e, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0d}, // P Q
    {0x1e, 0x11, 0x11, 0x1e, 0x14, 0x12, 0x11}, {0x0f, 0x10, 0x10, 0x0e, 0x01, 0x01, 0x1e}, // R S
    {0x1f, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04}, {0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0e}, // T U
    {0x11, 0x11, 0x11, 0x11, 0x11, 0x0a, 0x04}, {0x11, 0x11, 0x11, 0x15, 0x15, 0x15, 0x0a}, // V W
    {0x11, 0x11, 0x0a, 0x04, 0x0a, 0x11, 0x11}, {0x11, 0x11, 0x11, 0x0a, 0x04, 0x04, 0x04}, // X Y
    {0x1f, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1f}, {0x0e, 0x08, 0x08, 0x08, 0x08, 0x08, 0x0e}, // Z [
    {0x00, 0x10, 0x08, 0x04, 0x02, 0x01, 0x00}, {0x0e, 0x02, 0x02, 0x02, 0x02, 0x02, 0x0e}, // backslash ]
    {0x04, 0x0a, 0x11, 0x00, 0x00, 0x00, 0x00}, {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x1f}, // ^ _
};
// One solid rectangle in window pixels (top-left origin) on the bound window framebuffer.
static void status_fill(uint32_t wh, int x, int y, int w, int h, float r, float g, float b)
{
    glScissor(x, (int)wh - y - h, w, h);
    glClearColor(r, g, b, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);
}
// text (lines split by newlines) on a dark panel at the top left, scaled to fit the window.
static void status_text(uint32_t ww, uint32_t wh, const char* text)
{
    int lines = 1, cols = 0, col = 0;
    for (const char* c = text; *c; c++) {
        if (*c == '\n') {
            lines++;
            col = 0;
            continue;
        }
        cols = std::max(cols, ++col);
    }
    if (!cols)
        return;
    // A cell is 6x9 font pixels (glyph plus spacing); the panel has one cell of margin all round.
    const int scale = std::max(1, std::min({ 4, (int)ww / ((cols + 2) * 6), (int)wh / ((lines + 2) * 9) }));
    const int cw = 6 * scale, ch = 9 * scale;
    const GLboolean scissor = glIsEnabled(GL_SCISSOR_TEST);
    GLint box[4];
    glGetIntegerv(GL_SCISSOR_BOX, box);
    glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
    glEnable(GL_SCISSOR_TEST);
    status_fill(wh, 0, 0, (cols + 2) * cw, (lines + 2) * ch, 0.08f, 0.08f, 0.12f);
    int x = cw, y = ch;
    for (const char* c = text; *c; c++) {
        if (*c == '\n') {
            x = cw;
            y += ch;
            continue;
        }
        unsigned code = (unsigned char)*c;
        if (code >= 'a' && code <= 'z')
            code -= 'a' - 'A';
        if (code < 0x20 || code > 0x5f)
            code = '?';
        for (int row = 0; row < 7; row++)
            for (int bit = 0; bit < 5; bit++)
                if (status_font[code - 0x20][row] & (0x10 >> bit))
                    status_fill(wh, x + bit * scale, y + row * scale, scale, scale, 1.0f, 0.75f, 0.2f);
        x += cw;
    }
    glScissor(box[0], box[1], box[2], box[3]);
    if (!scissor)
        glDisable(GL_SCISSOR_TEST);
}
static int present(N64cF3d* f3d, uint32_t phys, const char* text)
{
    f3d->window->HandleEvents();
    uint32_t ww = 0, wh = 0;
    int32_t x = 0, y = 0;
    f3d->window->GetDimensions(&ww, &wh, &x, &y);
    f3d->renderer->UpdateFramebufferParameters(0, ww, wh, 1, false, true, true, true);
    f3d->renderer->StartDrawToFramebuffer(0, 1);
    f3d->renderer->ClearFramebuffer(true, true);
    const auto it = phys ? f3d->surfaces.find(phys) : f3d->surfaces.end();
    if (it != f3d->surfaces.end()) {
        const auto info = f3d->interpreter->mFrameBuffers.find(it->second);
        if (info != f3d->interpreter->mFrameBuffers.end())
            f3d->renderer->CopyFramebuffer(0, it->second, 0, 0, (int)info->second.applied_width,
                                           (int)info->second.applied_height, 0, 0, (int)ww, (int)wh);
    }
    if (text) {
        f3d->renderer->StartDrawToFramebuffer(0, 1); // CopyFramebuffer may leave its source bound
        status_text(ww, wh, text);
    }
    f3d->renderer->EndFrame();
    f3d->window->SwapBuffersBegin();
    return f3d->window->IsRunning() ? 1 : 0;
}
extern "C" int n64c_f3d_present(N64cF3d* f3d, uint32_t phys)
{
    return present(f3d, phys, nullptr);
}
extern "C" int n64c_f3d_present_text(N64cF3d* f3d, uint32_t phys, const char* text)
{
    return present(f3d, phys, text ? text : "");
}
extern "C" const char* n64c_f3d_renderer(const N64cF3d* f3d) { return f3d->gl_renderer.c_str(); }

extern "C" void n64c_f3d_close(N64cF3d* f3d)
{
    if (!f3d)
        return;
    n64c_dl_arena_free(&f3d->arena);
    f3d->interpreter->Destroy();
    f3d->interpreter.reset();
    delete f3d->renderer;
    delete f3d->window;
    delete f3d;
}
