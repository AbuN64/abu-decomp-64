// SPDX-License-Identifier: GPL-3.0-or-later
// The order a Fast3D window is taken down in. Mesa's D3D12 GL driver (Windows) keeps a D3D12 device and its WARP worker
// threads behind the context; if the process ends first, the driver's DLLs detach inside ExitProcess while those
// threads still call into freed objects and the run dies with an access violation. So the context leaves the thread and
// is destroyed while the window still exists, the window goes next, and the video subsystem, which unloads the GL
// driver, goes last, here on the owning thread.
#pragma once
#include <SDL3/SDL.h>
#include <cstdio>
#include <cstdlib>

namespace N64c {
inline void F3dReleaseDisplay(SDL_Window* window, SDL_GLContext context, bool unloadDriver)
{
    if (context) {
        if (!SDL_GL_MakeCurrent(window, nullptr)) {
            std::fprintf(stderr, "f3d teardown: release the context: %s\n", SDL_GetError());
            std::_Exit(78);
        }
        SDL_GL_DestroyContext(context);
    }
    if (window)
        SDL_DestroyWindow(window);
    if (unloadDriver)
        SDL_QuitSubSystem(SDL_INIT_VIDEO);
}
} // namespace N64c
