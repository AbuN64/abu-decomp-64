// SPDX-License-Identifier: GPL-3.0-or-later
#include "f3d_window.h"
#include "f3d_mesa_bundle.h"
#include "n64c_input.h"
#include <SDL3/SDL_opengl.h>
#include <cstdio>
#include <algorithm>
#include <cstdlib>
#include <utility>
#include <vector>
#ifdef _WIN32
#include <windows.h>
#endif

namespace N64c {
// An SDL GL call that returns false leaves only SDL's error string: name the call (the first few per run).
static void gl_call_failed(const char* call)
{
    static int budget = 8;
    if (budget > 0) {
        budget--;
        std::fprintf(stderr, "boot-gl: %s failed: %s\n", call, SDL_GetError());
    }
}
#ifdef _WIN32
static bool file_exists(const std::string &path) { return GetFileAttributesA(path.c_str()) != INVALID_FILE_ATTRIBUTES; }

// Mesa's D3D12 driver _exit()s at the first draw when its sibling DLLs are absent: refuse before that, naming the file.
static void require_mesa_bundle()
{
    HMODULE gallium = GetModuleHandleA("libgallium_wgl.dll");
    char path[MAX_PATH];
    if (!gallium || !GetModuleFileNameA(gallium, path, sizeof path))
        return;
    if (const char *missing = f3d_mesa_missing_dll(path, file_exists)) {
        std::fprintf(stderr, "boot-gl: %s is missing beside %s; Mesa's D3D12 driver cannot create pipeline states without it\n",
                     missing, path);
        std::exit(1);
    }
}
#endif
F3dWindow::~F3dWindow() { Destroy(); }

void F3dWindow::Init(const char* gameName, const char*, bool startFullScreen, uint32_t width, uint32_t height,
                     int32_t, int32_t)
{
    if (!SDL_Init(SDL_INIT_VIDEO)) {
        std::fprintf(stderr, "f3d window: SDL_Init: %s\n", SDL_GetError());
        std::exit(1);
    }
    // Default (compatibility) context: the OpenGL backend draws without a vertex array object
    // outside Apple/GLES builds, which a core profile rejects. Depth for the game's z-buffer.
#ifdef __ANDROID__
    // Android has OpenGL ES only (LUS USE_OPENGLES, shaders #version 300 es).
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_PROFILE_MASK, SDL_GL_CONTEXT_PROFILE_ES);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION, 3);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MINOR_VERSION, 0);
#endif
    SDL_GL_SetAttribute(SDL_GL_DEPTH_SIZE, 24);
    SDL_GL_SetAttribute(SDL_GL_DOUBLEBUFFER, 1);
    mFullScreen = startFullScreen;
    mWindow = SDL_CreateWindow(gameName, (int)width, (int)height,
                               SDL_WINDOW_OPENGL | SDL_WINDOW_RESIZABLE | (startFullScreen ? SDL_WINDOW_FULLSCREEN : 0));
    if (mWindow)
        mContext = SDL_GL_CreateContext(mWindow);
    if (!mWindow || !mContext) {
        std::fprintf(stderr, "f3d window: %s\n", SDL_GetError());
        std::exit(1);
    }
#ifdef _WIN32
    require_mesa_bundle();
#endif
    if (!n64c_input_start(mWindow)) {
        std::fprintf(stderr, "f3d input: %s\n", SDL_GetError());
        std::exit(1);
    }
    if (!SDL_GL_MakeCurrent(mWindow, mContext))
        gl_call_failed("SDL_GL_MakeCurrent");
    // Which GPU or software rasterizer serves the picture is part of every run log (perf and bug reports need it).
    if (auto getString = reinterpret_cast<decltype(&glGetString)>(SDL_GL_GetProcAddress("glGetString"))) {
        const char *renderer = reinterpret_cast<const char *>(getString(0x1F01));
        const char *version = reinterpret_cast<const char *>(getString(0x1F02));
        std::fprintf(stderr, "boot-gl: renderer=%s version=%s\n", renderer ? renderer : "?", version ? version : "?");
    }
    // The port's VI paces presentation; a vsync wait per flip would hold back the guest clock.
    mVsyncEnabled = false;
    if (!SDL_GL_SetSwapInterval(0))
        gl_call_failed("SDL_GL_SetSwapInterval");
    mWidth = width;
    mHeight = height;
    mMode = startFullScreen ? 1 : 0;
    mModeWidth = width;
    mModeHeight = height;
}

void F3dWindow::Close() { mIsRunning = false; }
void F3dWindow::SetKeyboardCallbacks(bool (*)(int), bool (*)(int), void (*)()) {}
void F3dWindow::SetMouseCallbacks(bool (*)(int), bool (*)(int)) {}
void F3dWindow::SetFullscreenChangedCallback(void (*onFullscreenChanged)(bool))
{
    mOnFullscreenChanged = onFullscreenChanged;
}
void F3dWindow::SetFullscreen(bool fullscreen)
{
    if (mWindow && SDL_SetWindowFullscreen(mWindow, fullscreen)) {
        mFullScreen = fullscreen;
        mMode = fullscreen ? 1 : 0;
    }
}
void F3dWindow::GetActiveWindowRefreshRate(uint32_t* refreshRate)
{
    const SDL_DisplayMode* mode = mWindow ? SDL_GetCurrentDisplayMode(SDL_GetDisplayForWindow(mWindow)) : nullptr;
    *refreshRate = mode && mode->refresh_rate > 0 ? (uint32_t)mode->refresh_rate : 60;
}
void F3dWindow::SetCursorVisibility(bool visible) { visible ? SDL_ShowCursor() : SDL_HideCursor(); }
void F3dWindow::SetMousePos(int32_t, int32_t) {}
void F3dWindow::GetMousePos(int32_t* x, int32_t* y) { *x = *y = 0; }
void F3dWindow::GetMouseDelta(int32_t* x, int32_t* y) { *x = *y = 0; }
void F3dWindow::GetMouseWheel(float* x, float* y) { *x = *y = 0; }
bool F3dWindow::GetMouseState(uint32_t) { return false; }
void F3dWindow::SetMouseCapture(bool) {}
bool F3dWindow::IsMouseCaptured() { return false; }
void F3dWindow::GetOutputDimensions(uint32_t* width, uint32_t* height) const
{
    int w = (int)mWidth, h = (int)mHeight;
    if (mWindow)
        SDL_GetWindowSizeInPixels(mWindow, &w, &h);
    *width = (uint32_t)w;
    *height = (uint32_t)h;
}
// What the game renders at: the window's pixels scaled by the render scale (the picture is then stretched to the window).
void F3dWindow::GetDimensions(uint32_t* width, uint32_t* height, int32_t* posX, int32_t* posY)
{
    GetOutputDimensions(width, height);
    *width = std::max<uint32_t>(1, *width * (uint32_t)mRenderScale / 100);
    *height = std::max<uint32_t>(1, *height * (uint32_t)mRenderScale / 100);
    *posX = *posY = 0;
}
static bool Step(bool ok, const char* what, std::string& error)
{
    if (!ok)
        error = std::string(what) + ": " + SDL_GetError();
    return ok;
}
bool F3dWindow::ApplyMode(int mode, uint32_t width, uint32_t height, std::string& error)
{
    const SDL_DisplayID display = SDL_GetDisplayForWindow(mWindow);
    if (mode == 0) { // windowed: bordered, the chosen size
        if (!Step(SDL_SetWindowFullscreen(mWindow, false), "leave fullscreen", error))
            return false;
        SDL_SyncWindow(mWindow);
        if (!Step(SDL_SetWindowBordered(mWindow, true), "set the border", error) ||
            !Step(SDL_SetWindowSize(mWindow, (int)width, (int)height), "set the window size", error))
            return false;
        SDL_SetWindowPosition(mWindow, SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED); // placement only: some platforms (Wayland, offscreen) have none
    } else if (mode == 1) { // borderless: the desktop's own mode, the window covers the display
        if (!Step(SDL_SetWindowFullscreenMode(mWindow, nullptr), "use the desktop mode", error) ||
            !Step(SDL_SetWindowFullscreen(mWindow, true), "enter borderless fullscreen", error))
            return false;
    } else { // fullscreen: the display mode closest to the chosen size
        SDL_DisplayMode closest;
        if (!Step(SDL_GetClosestFullscreenDisplayMode(display, (int)width, (int)height, 0.0f, true, &closest),
                  "find a display mode", error) ||
            !Step(SDL_SetWindowFullscreenMode(mWindow, &closest), "use the display mode", error) ||
            !Step(SDL_SetWindowFullscreen(mWindow, true), "enter fullscreen", error))
            return false;
    }
    SDL_SyncWindow(mWindow);
    return true;
}
bool F3dWindow::SetMode(int mode, uint32_t width, uint32_t height, std::string& error)
{
    if (!mWindow) {
        error = "the window is not open";
        return false;
    }
    if (mode < 0 || mode > 2) {
        error = "window mode " + std::to_string(mode) + " is not 0, 1 or 2";
        return false;
    }
    if (width == 0 || height == 0) {
        error = "window size " + std::to_string(width) + "x" + std::to_string(height) + " is empty";
        return false;
    }
    if (ApplyMode(mode, width, height, error)) {
        mMode = mode;
        mModeWidth = width;
        mModeHeight = height;
        mFullScreen = mode != 0;
        return true;
    }
    std::string ignored; // back to the mode and size it had
    ApplyMode(mMode, mModeWidth, mModeHeight, ignored);
    return false;
}
bool F3dWindow::SetRenderScale(int percent, std::string& error)
{
    if (percent < 25 || percent > 100) {
        error = "render scale " + std::to_string(percent) + "% is outside 25..100";
        return false;
    }
    mRenderScale = percent;
    return true;
}
bool F3dWindow::SetDisplay(int mode, uint32_t width, uint32_t height, int percent, std::string& error)
{
    const int priorMode = mMode, priorScale = mRenderScale;
    const uint32_t priorWidth = mModeWidth, priorHeight = mModeHeight;
    if (!SetMode(mode, width, height, error))
        return false;
    if (SetRenderScale(percent, error))
        return true;
    std::string ignored;
    SetMode(priorMode, priorWidth, priorHeight, ignored);
    mRenderScale = priorScale;
    return false;
}
int F3dWindow::EffectiveRenderScale() const
{
    uint32_t outW, outH;
    GetOutputDimensions(&outW, &outH);
    const uint32_t w = std::max<uint32_t>(1, outW * (uint32_t)mRenderScale / 100); // as GetDimensions
    return outW ? std::min(100, std::max(1, (int)(((uint64_t)w * 100 + outW / 2) / outW))) : mRenderScale;
}
size_t F3dWindow::DisplaySizes(uint32_t (*sizes)[2], size_t max) const
{
    static const uint32_t common[][2] = { { 640, 480 }, { 800, 600 }, { 1024, 768 }, { 1280, 720 }, { 1920, 1080 } };
    std::vector<std::pair<uint32_t, uint32_t>> found;
    const SDL_DisplayID display = mWindow ? SDL_GetDisplayForWindow(mWindow) : SDL_GetPrimaryDisplay();
    int count = 0;
    SDL_DisplayMode** modes = SDL_GetFullscreenDisplayModes(display, &count);
    for (int i = 0; modes && i < count; i++)
        found.emplace_back((uint32_t)modes[i]->w, (uint32_t)modes[i]->h);
    SDL_free(modes);
    SDL_Rect bounds = { 0, 0, 0, 0 };
    SDL_GetDisplayBounds(display, &bounds);
    for (const auto& c : common)
        if (bounds.w <= 0 || ((int)c[0] <= bounds.w && (int)c[1] <= bounds.h))
            found.emplace_back(c[0], c[1]);
    std::sort(found.begin(), found.end(), [](const auto& a, const auto& b) { return a > b; });
    found.erase(std::unique(found.begin(), found.end()), found.end());
    size_t n = 0;
    for (const auto& f : found)
        if (n < max) {
            sizes[n][0] = f.first;
            sizes[n][1] = f.second;
            n++;
        }
    return n;
}
void F3dWindow::SetDimensions(uint32_t width, uint32_t height, int32_t, int32_t)
{
    if (mWindow)
        SDL_SetWindowSize(mWindow, (int)width, (int)height);
    mWidth = width;
    mHeight = height;
}
Ship::WindowRect F3dWindow::GetPrimaryMonitorRect()
{
    SDL_Rect r = {0, 0, (int)mWidth, (int)mHeight};
    SDL_GetDisplayBounds(SDL_GetPrimaryDisplay(), &r);
    return {r.x, r.y, r.x + r.w, r.y + r.h};
}
void F3dWindow::HandleEvents()
{
    SDL_Event event;
    while (SDL_PollEvent(&event)) {
        n64c_input_event(&event);
        if (event.type == SDL_EVENT_QUIT || event.type == SDL_EVENT_WINDOW_CLOSE_REQUESTED)
            mIsRunning = false;
    }
    n64c_input_update();
}
bool F3dWindow::IsFrameReady() { return true; } // the port's VI paces frames
void F3dWindow::SwapBuffersBegin()
{
    DrawControlsOverlay(mWindow);
    if (mWindow && !SDL_GL_SwapWindow(mWindow))
        gl_call_failed("SDL_GL_SwapWindow");
}
void F3dWindow::SwapBuffersEnd() {}
double F3dWindow::GetTime() { return (double)SDL_GetTicksNS() / 1e9; }
int F3dWindow::GetTargetFps() { return (int)mTargetFps; }
void F3dWindow::SetTargetFps(int fps) { mTargetFps = (uint32_t)fps; }
void F3dWindow::SetMaxFrameLatency(int) {}
const char* F3dWindow::GetKeyName(int scancode) { return SDL_GetScancodeName((SDL_Scancode)scancode); }
bool F3dWindow::CanDisableVsync() { return true; }
bool F3dWindow::IsRunning() { return mIsRunning; }
void F3dWindow::Destroy()
{
    n64c_input_stop();
    if (mContext)
        SDL_GL_DestroyContext(mContext);
    if (mWindow)
        SDL_DestroyWindow(mWindow);
    mContext = nullptr;
    mWindow = nullptr;
}
bool F3dWindow::IsFullscreen() { return mFullScreen; }
} // namespace N64c
