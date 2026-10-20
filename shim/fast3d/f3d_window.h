// SPDX-License-Identifier: GPL-3.0-or-later
// The port's window for the Fast3D renderer: one SDL3 window with an OpenGL context.
// It replaces LUS's SDL backend and Fast3dWindow, which wire in LUS's GUI and debugger windows;
// the port drives the interpreter itself and has no GUI layer. Input stays with the port's own
// controller code, so the keyboard and mouse hooks are inert.
#pragma once
#include "fast/backends/gfx_window_manager_api.h"
#include "f3d_context.h"
#include <SDL3/SDL.h>

namespace N64c {
class F3dWindow final : public Fast::GfxWindowBackend {
  public:
    ~F3dWindow() override;
    F3dContextScope UseContext() const { return F3dContextScope(mWindow, mContext); }
    void Init(const char* gameName, const char* apiName, bool startFullScreen, uint32_t width, uint32_t height,
              int32_t posX, int32_t posY) override;
    void Close() override;
    void SetKeyboardCallbacks(bool (*onKeyDown)(int), bool (*onKeyUp)(int), void (*onAllKeysUp)()) override;
    void SetMouseCallbacks(bool (*onMouseButtonDown)(int), bool (*onMouseButtonUp)(int)) override;
    void SetFullscreenChangedCallback(void (*onFullscreenChanged)(bool)) override;
    void SetFullscreen(bool fullscreen) override;
    void GetActiveWindowRefreshRate(uint32_t* refreshRate) override;
    void SetCursorVisibility(bool visible) override;
    void SetMousePos(int32_t posX, int32_t posY) override;
    void GetMousePos(int32_t* x, int32_t* y) override;
    void GetMouseDelta(int32_t* x, int32_t* y) override;
    void GetMouseWheel(float* x, float* y) override;
    bool GetMouseState(uint32_t btn) override;
    void SetMouseCapture(bool capture) override;
    bool IsMouseCaptured() override;
    void GetDimensions(uint32_t* width, uint32_t* height, int32_t* posX, int32_t* posY) override;
    void SetDimensions(uint32_t width, uint32_t height, int32_t posX, int32_t posY) override;
    Ship::WindowRect GetPrimaryMonitorRect() override;
    void HandleEvents() override;
    bool IsFrameReady() override;
    void SwapBuffersBegin() override;
    void SwapBuffersEnd() override;
    double GetTime() override;
    int GetTargetFps() override;
    void SetTargetFps(int fps) override;
    void SetMaxFrameLatency(int latency) override;
    const char* GetKeyName(int scancode) override;
    bool CanDisableVsync() override;
    bool IsRunning() override;
    void Destroy() override;
    bool IsFullscreen() override;

  private:
    SDL_Window* mWindow = nullptr;
    SDL_GLContext mContext = nullptr;
    uint32_t mWidth = 0, mHeight = 0;
};
} // namespace N64c
