// SPDX-License-Identifier: GPL-3.0-or-later
#include "f3d_window.h"
#include "n64c_input.h"
#include <SDL3/SDL_opengl.h>
#include <cstdio>
#include <cstdlib>

namespace N64c {
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
    if (!n64c_input_start(mWindow)) {
        std::fprintf(stderr, "f3d input: %s\n", SDL_GetError());
        std::exit(1);
    }
    SDL_GL_MakeCurrent(mWindow, mContext);
    // Which GPU or software rasterizer serves the picture is part of every run log (perf and bug reports need it).
    if (auto getString = reinterpret_cast<decltype(&glGetString)>(SDL_GL_GetProcAddress("glGetString"))) {
        const char *renderer = reinterpret_cast<const char *>(getString(0x1F01));
        const char *version = reinterpret_cast<const char *>(getString(0x1F02));
        std::fprintf(stderr, "boot-gl: renderer=%s version=%s\n", renderer ? renderer : "?", version ? version : "?");
    }
    // The port's VI paces presentation; a vsync wait per flip would hold back the guest clock.
    mVsyncEnabled = false;
    SDL_GL_SetSwapInterval(0);
    mWidth = width;
    mHeight = height;
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
    if (mWindow && SDL_SetWindowFullscreen(mWindow, fullscreen))
        mFullScreen = fullscreen;
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
void F3dWindow::GetDimensions(uint32_t* width, uint32_t* height, int32_t* posX, int32_t* posY)
{
    int w = (int)mWidth, h = (int)mHeight;
    if (mWindow)
        SDL_GetWindowSizeInPixels(mWindow, &w, &h);
    *width = (uint32_t)w;
    *height = (uint32_t)h;
    *posX = *posY = 0;
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
    if (mWindow)
        SDL_GL_SwapWindow(mWindow);
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
