// SPDX-License-Identifier: GPL-3.0-or-later
// A color-image address can be reused at a different resolution. Keep its framebuffer ID,
// but refresh the native coordinate system and, when needed, its scaled allocation.
#pragma once
#include <cstdint>
#include <map>

namespace N64c {
template <class Interpreter, class Renderer>
int ColorImageSurface(Interpreter& gfx, Renderer& renderer, std::map<uint32_t, int>& surfaces,
                      uint32_t phys, uint32_t width, uint32_t height)
{
    auto it = surfaces.find(phys);
    if (it == surfaces.end()) {
        const int fb = gfx.CreateFrameBuffer(width, height, width, height, 1);
        return surfaces.emplace(phys, fb).first->second;
    }
    auto& info = gfx.mFrameBuffers.at(it->second);
    if (info.orig_width != width || info.orig_height != height) {
        info.orig_width = info.native_width = width;
        info.orig_height = info.native_height = height;
        uint32_t scaled_width = width, scaled_height = height;
        gfx.AdjustWidthHeightForScale(scaled_width, scaled_height, width, height);
        if (info.applied_width != scaled_width || info.applied_height != scaled_height) {
            renderer.UpdateFramebufferParameters(it->second, scaled_width, scaled_height, 1, true, true, true, true);
            info.applied_width = scaled_width;
            info.applied_height = scaled_height;
        }
    }
    return it->second;
}
} // namespace N64c
