// SPDX-License-Identifier: GPL-3.0-or-later
// Native rendering and presentation are serialized by the scheduler but may run on different
// host threads. Bind only for the public adapter call, then restore that thread's prior context.
#pragma once
#include <SDL3/SDL.h>
#include <cstdio>
#include <cstdlib>

namespace N64c {
class F3dContextScope final {
  public:
    F3dContextScope() : previous_window(SDL_GL_GetCurrentWindow()),
                        previous_context(SDL_GL_GetCurrentContext()) {}
    F3dContextScope(SDL_Window* window, SDL_GLContext context) : F3dContextScope()
    {
        active_context = context;
        make_current(window, context);
    }
    ~F3dContextScope()
    {
        if (SDL_GL_GetCurrentWindow() != previous_window || SDL_GL_GetCurrentContext() != previous_context)
            make_current(previous_window, previous_context);
    }
    F3dContextScope(const F3dContextScope&) = delete;
    F3dContextScope& operator=(const F3dContextScope&) = delete;
    // Closing the renderer must not restore a context it just destroyed.
    void Retire()
    {
        if (previous_context == active_context) {
            previous_window = nullptr;
            previous_context = nullptr;
        }
    }

  private:
    static void make_current(SDL_Window* window, SDL_GLContext context)
    {
        if (SDL_GL_MakeCurrent(window, context)) return;
        std::fprintf(stderr, "f3d context: %s\n", SDL_GetError());
        std::_Exit(78);
    }
    SDL_Window* previous_window;
    SDL_GLContext previous_context;
    SDL_GLContext active_context = nullptr;
};
} // namespace N64c
