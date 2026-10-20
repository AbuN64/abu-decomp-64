/* SPDX-License-Identifier: GPL-3.0-or-later */
/* F3D/F3DEX command numbers and parameter bits for the pinned Fast3D interpreter.
 * Interface facts only, from public microcode documentation (n64brew wiki "F3D" and
 * "F3DEX" command tables, matrix flags, geometry-mode bits, MOVEMEM/MOVEWORD
 * indices). Replaces the grey LUS header of the same name (AUDIT.md). */
#pragma once
#include <stdint.h>
#ifndef OPCODE
#define OPCODE(x) (int8_t)(x)
#endif

constexpr int8_t F3DEX_G_SPNOOP = OPCODE(0x00);
constexpr int8_t F3DEX_G_MTX = OPCODE(0x01);
constexpr int8_t F3DEX_G_RESERVED0 = OPCODE(0x02);
constexpr int8_t F3DEX_G_MOVEMEM = OPCODE(0x03);
constexpr int8_t F3DEX_G_VTX = OPCODE(0x04);
constexpr int8_t F3DEX_G_RESERVED1 = OPCODE(0x05);
constexpr int8_t F3DEX_G_DL = OPCODE(0x06);
constexpr int8_t F3DEX_G_RESERVED2 = OPCODE(0x07);
constexpr int8_t F3DEX_G_RESERVED3 = OPCODE(0x08);
constexpr int8_t F3DEX_G_SPRITE2D_BASE = OPCODE(0x09);
constexpr int8_t F3DEX_G_TRI2 = OPCODE(0xb1);
constexpr int8_t F3DEX_G_MODIFYVTX = OPCODE(0xb2);
constexpr int8_t F3DEX_G_RDPHALF_2 = OPCODE(0xb3);
constexpr int8_t F3DEX_G_RDPHALF_1 = OPCODE(0xb4);
constexpr int8_t F3DEX_G_QUAD = OPCODE(0xb5);
constexpr int8_t F3DEX_G_CLEARGEOMETRYMODE = OPCODE(0xb6);
constexpr int8_t F3DEX_G_SETGEOMETRYMODE = OPCODE(0xb7);
constexpr int8_t F3DEX_G_ENDDL = OPCODE(0xb8);
constexpr int8_t F3DEX_G_SETOTHERMODE_L = OPCODE(0xb9);
constexpr int8_t F3DEX_G_SETOTHERMODE_H = OPCODE(0xba);
constexpr int8_t F3DEX_G_TEXTURE = OPCODE(0xbb);
constexpr int8_t F3DEX_G_MOVEWORD = OPCODE(0xbc);
constexpr int8_t F3DEX_G_POPMTX = OPCODE(0xbd);
constexpr int8_t F3DEX_G_CULLDL = OPCODE(0xbe);
constexpr int8_t F3DEX_G_TRI1 = OPCODE(0xbf);
constexpr int8_t F3DEX_G_NOOP = OPCODE(0xc0);

/* G_MTX parameter bits. */
constexpr int8_t F3DEX_G_MTX_MODELVIEW = 0x00;
constexpr int8_t F3DEX_G_MTX_PROJECTION = 0x01;
constexpr int8_t F3DEX_G_MTX_MUL = 0x00;
constexpr int8_t F3DEX_G_MTX_LOAD = 0x02;
constexpr int8_t F3DEX_G_MTX_NOPUSH = 0x00;
constexpr int8_t F3DEX_G_MTX_PUSH = 0x04;

/* Geometry-mode bits that differ from F3DEX2. */
constexpr uint32_t F3DEX_G_TEXTURE_ENABLE = 0x00000002;
constexpr uint32_t F3DEX_G_SHADING_SMOOTH = 0x00000200;
constexpr uint32_t F3DEX_G_CULL_FRONT = 0x00001000;
constexpr uint32_t F3DEX_G_CULL_BACK = 0x00002000;
constexpr uint32_t F3DEX_G_CULL_BOTH = 0x00003000;
constexpr uint32_t F3DEX_G_CLIPPING = 0x00800000;

/* G_MOVEMEM indices: viewport, look-at, eight lights, texture attributes, matrix rows. */
constexpr uint8_t F3DEX_G_MV_VIEWPORT = 0x80;
constexpr uint8_t F3DEX_G_MV_LOOKATY = 0x82;
constexpr uint8_t F3DEX_G_MV_LOOKATX = 0x84;
constexpr uint8_t F3DEX_G_MV_L0 = 0x86;
constexpr uint8_t F3DEX_G_MV_L1 = 0x88;
constexpr uint8_t F3DEX_G_MV_L2 = 0x8a;
constexpr uint8_t F3DEX_G_MV_L3 = 0x8c;
constexpr uint8_t F3DEX_G_MV_L4 = 0x8e;
constexpr uint8_t F3DEX_G_MV_L5 = 0x90;
constexpr uint8_t F3DEX_G_MV_L6 = 0x92;
constexpr uint8_t F3DEX_G_MV_L7 = 0x94;
constexpr uint8_t F3DEX_G_MV_TXTATT = 0x96;
constexpr uint8_t F3DEX_G_MV_MATRIX_2 = 0x98;
constexpr uint8_t F3DEX_G_MV_MATRIX_3 = 0x9a;
constexpr uint8_t F3DEX_G_MV_MATRIX_4 = 0x9c;
constexpr uint8_t F3DEX_G_MV_MATRIX_1 = 0x9e;

/* G_MOVEWORD index specific to the F3D family. */
constexpr int8_t F3DEX_G_MW_POINTS = 0x0c;
