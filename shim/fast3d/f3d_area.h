// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
namespace N64c {
// Fast3D converts a viewport or scissor to the bound framebuffer's rows when the game sets it. Inside a framebuffer
// it took GL's bottom-up rows, but the framebuffers it creates for colour images are inverted (top row first), so an
// area off the vertical centre landed mirrored: a scissor at rows 15-94 of 222 cut rows 128-207. y is the area's lower
// edge in N64 rows (top-down), height its height; the result is the first framebuffer row it covers.
inline float FramebufferAreaY(float y, float height, float fb_height, bool inverted)
{
    return inverted ? y - height : fb_height - y;
}
}
