// SPDX-License-Identifier: GPL-3.0-or-later
// Force-included in LUS's OpenGL backend on OpenGL ES (cmake/fast3d.cmake). The backend makes its framebuffer colour
// attachments GL_RGB8, which desktop GL renders into; in OpenGL ES 3 only RGBA8 (not RGB8) is guaranteed
// colour-renderable, and the Android emulator's driver leaves such a framebuffer incomplete, so nothing is drawn.
// Every GL_RGB / GL_RGB8 use in that file is such an attachment (textures and read-backs already use RGBA).
#pragma once
#include <GLES3/gl3.h>
#undef GL_RGB8
#define GL_RGB8 GL_RGBA8
#undef GL_RGB
#define GL_RGB GL_RGBA
