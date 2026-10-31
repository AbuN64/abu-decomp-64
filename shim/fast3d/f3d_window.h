// SPDX-License-Identifier: GPL-3.0-or-later
// The port's window for the Fast3D renderer: one SDL3 window with an OpenGL context.
// It replaces LUS's SDL backend and Fast3dWindow, which wire in LUS's GUI and debugger windows;
// the port drives the interpreter itself and has no GUI layer. Input stays with the port's own
// controller code, so the keyboard and mouse hooks are inert.
#pragma once
#include "fast/backends/gfx_window_manager_api.h"
#include "f3d_context.h"
#include "f3d_controls_overlay.h"
#include <SDL3/SDL.h>
#include <string>

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
    // The options screen: N64C_WIN_* (n64c_menu.h), the display's sizes (largest first), the render scale in percent.
    // The setters return false with the reason in error and leave the window as it was.
    bool SetMode(int mode, uint32_t width, uint32_t height, std::string& error);
    bool SetRenderScale(int percent, std::string& error); // 25..100
    // Mode and scale together: both take, or the window is back at its prior mode, size and scale.
    bool SetDisplay(int mode, uint32_t width, uint32_t height, int percent, std::string& error);
    int RenderScale() const { return mRenderScale; }
    // The scale the render target really has against the window's pixels (whole pixels, at least one).
    int EffectiveRenderScale() const;
    size_t DisplaySizes(uint32_t (*sizes)[2], size_t max) const;
    void GetOutputDimensions(uint32_t* width, uint32_t* height) const; // the window's own pixels, whatever the scale

  private:
    bool ApplyMode(int mode, uint32_t width, uint32_t height, std::string& error);
    int mRenderScale = 100;
    int mMode = 0;                                // N64C_WIN_* in force
    uint32_t mModeWidth = 0, mModeHeight = 0;     // the size chosen for windowed / fullscreen mode
    SDL_Window* mWindow = nullptr;
    SDL_GLContext mContext = nullptr;
    bool mVideo = false;                          // Init started SDL's video subsystem
    uint32_t mWidth = 0, mHeight = 0;
};
} // namespace N64c
