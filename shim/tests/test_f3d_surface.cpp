// SPDX-License-Identifier: GPL-3.0-or-later
#include "../fast3d/f3d_surface.h"
#include <cmath>
#include <cstdio>

static int failures, checks;
#define CHECK(c) do { ++checks; if (!(c)) { ++failures; std::fprintf(stderr, "FAIL %d: %s\n", __LINE__, #c); } } while (0)
struct Info {
    uint32_t orig_width, orig_height, applied_width, applied_height, native_width, native_height;
};
struct Renderer {
    int updates = 0, id = 0;
    uint32_t width = 0, height = 0;
    void UpdateFramebufferParameters(int fb, uint32_t w, uint32_t h, int samples, bool color, bool depth,
                                     bool readable, bool inverted)
    {
        CHECK(samples == 1 && color && depth && readable && inverted);
        updates++; id = fb; width = w; height = h;
    }
};
struct Interpreter {
    std::map<int, Info> mFrameBuffers;
    int created = 0;
    void AdjustWidthHeightForScale(uint32_t& w, uint32_t& h, uint32_t nw, uint32_t nh)
    {
        w = (uint32_t)std::round(w * (640.0f / (2 * (nw / 2))));
        h = (uint32_t)std::round(h * (480.0f / (2 * (nh / 2))));
    }
    int CreateFrameBuffer(uint32_t w, uint32_t h, uint32_t nw, uint32_t nh, int resize)
    {
        CHECK(resize == 1);
        uint32_t sw = w, sh = h;
        AdjustWidthHeightForScale(sw, sh, nw, nh);
        mFrameBuffers[++created] = {w, h, sw, sh, nw, nh};
        return created;
    }
};
int main()
{
    Interpreter gfx;
    Renderer renderer;
    std::map<uint32_t, int> surfaces;
    const int first = N64c::ColorImageSurface(gfx, renderer, surfaces, 0x500000, 480, 222);
    const int second = N64c::ColorImageSurface(gfx, renderer, surfaces, 0x554600, 480, 222);
    CHECK(first != second && gfx.created == 2);
    for (int i = 0; i < 4; i++) {
        CHECK(N64c::ColorImageSurface(gfx, renderer, surfaces, 0x500000, 284, 222) == first);
        const auto& info = gfx.mFrameBuffers.at(first);
        CHECK(info.orig_width == 284 && info.native_width == 284);
        CHECK(info.orig_height == 222 && info.applied_width == 640 && info.applied_height == 480);
        CHECK(142.0f / info.orig_width * info.applied_width == 320.0f);
    }
    CHECK(gfx.created == 2 && renderer.updates == 0);
    CHECK(gfx.mFrameBuffers.at(second).orig_width == 480); // other VI surface is untouched
    CHECK(N64c::ColorImageSurface(gfx, renderer, surfaces, 0x500000, 321, 241) == first);
    const auto& resized = gfx.mFrameBuffers.at(first);
    CHECK(resized.orig_height == 241 && resized.native_height == 241);
    CHECK(resized.applied_width == 642 && resized.applied_height == 482);
    CHECK(renderer.updates == 1 && renderer.id == first && renderer.width == 642 && renderer.height == 482);
    CHECK(N64c::ColorImageSurface(gfx, renderer, surfaces, 0x500000, 480, 222) == first);
    CHECK(renderer.updates == 2 && gfx.created == 2 && surfaces.size() == 2);
    CHECK(N64c::ColorImageSurface(gfx, renderer, surfaces, 0x500000, 480, 360) == first); // grows
    CHECK(gfx.mFrameBuffers.at(first).orig_height == 360);
    CHECK(N64c::ColorImageSurface(gfx, renderer, surfaces, 0x500000, 480, 120) == first); // part-screen scissor
    CHECK(gfx.mFrameBuffers.at(first).orig_height == 360);
    CHECK(N64c::ColorImageSurface(gfx, renderer, surfaces, 0x500000, 284, 222) == first); // new mode
    CHECK(gfx.mFrameBuffers.at(first).orig_height == 222 && gfx.mFrameBuffers.at(first).orig_width == 284);
    std::printf("f3d_surface: %d checks, %d failures\n", checks, failures);
    return failures != 0;
}
