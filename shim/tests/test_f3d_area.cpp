// SPDX-License-Identifier: GPL-3.0-or-later
#include "../fast3d/f3d_area.h"
#include <cstdio>
int main()
{
    struct Case { float y, height, fb_height; bool inverted; float row; };
    const Case cases[] = {
        {94, 79, 222, true, 15},    // weapon-select portrait scissor 18,15-52,94: its own top row
        {94, 79, 222, false, 128},  // a bottom-up framebuffer starts at the lower edge's mirror
        {222, 222, 222, true, 0},   // full-screen areas are the same either way
        {222, 222, 222, false, 0},
        {111, 111, 222, true, 0},   // top half of a split screen stays on top
        {222, 111, 222, true, 111}, // bottom half stays below
    };
    for (const auto& c : cases)
        if (N64c::FramebufferAreaY(c.y, c.height, c.fb_height, c.inverted) != c.row) return 1;
    std::puts("framebuffer area contract: 6 cases passed");
    return 0;
}
