// SPDX-License-Identifier: GPL-3.0-or-later
// The Controls page and the touch zones over the finished frame (f3d_controls_overlay.cpp); draws nothing while the
// page is closed and no finger has touched the window.
#pragma once
#include <SDL3/SDL.h>

namespace N64c {
void DrawControlsOverlay(SDL_Window* window);
} // namespace N64c
