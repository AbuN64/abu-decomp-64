/* SPDX-License-Identifier: GPL-3.0-or-later */
/* F3DEX2 (and S2DEX2) command numbers and parameter bits for the pinned Fast3D
 * interpreter. Interface facts only, written for rage-ports from public microcode
 * documentation: n64brew wiki "F3DEX2" command table and "S2DEX" object commands
 * (opcode byte = bits 56-63 of the first command word), geometry-mode and matrix
 * parameter bit tables, MOVEMEM/MOVEWORD index tables. Replaces the grey LUS header
 * of the same name (AUDIT.md); it is never derived from gbi.h. */
#pragma once
#include <stdint.h>
#ifndef OPCODE
#define OPCODE(x) (int8_t)(x)
#endif

/* RSP geometry commands (low opcode range). */
constexpr int8_t F3DEX2_G_NOOP = OPCODE(0x00);
constexpr int8_t F3DEX2_G_VTX = OPCODE(0x01);
constexpr int8_t F3DEX2_G_MODIFYVTX = OPCODE(0x02);
constexpr int8_t F3DEX2_G_CULLDL = OPCODE(0x03);
constexpr int8_t F3DEX2_G_BRANCH_Z = OPCODE(0x04);
constexpr int8_t F3DEX2_G_TRI1 = OPCODE(0x05);
constexpr int8_t F3DEX2_G_TRI2 = OPCODE(0x06);
constexpr int8_t F3DEX2_G_QUAD = OPCODE(0x07);
constexpr int8_t F3DEX2_G_LINE3D = OPCODE(0x08);
/* RSP control commands (high opcode range). */
constexpr int8_t F3DEX2_G_SPECIAL_3 = OPCODE(0xd3);
constexpr int8_t F3DEX2_G_SPECIAL_2 = OPCODE(0xd4);
constexpr int8_t F3DEX2_G_SPECIAL_1 = OPCODE(0xd5);
constexpr int8_t F3DEX2_G_DMA_IO = OPCODE(0xd6);
constexpr int8_t F3DEX2_G_TEXTURE = OPCODE(0xd7);
constexpr int8_t F3DEX2_G_POPMTX = OPCODE(0xd8);
constexpr int8_t F3DEX2_G_GEOMETRYMODE = OPCODE(0xd9);
constexpr int8_t F3DEX2_G_MTX = OPCODE(0xda);
constexpr int8_t F3DEX2_G_MOVEWORD = OPCODE(0xdb);
constexpr int8_t F3DEX2_G_MOVEMEM = OPCODE(0xdc);
constexpr int8_t F3DEX2_G_LOAD_UCODE = OPCODE(0xdd);
constexpr int8_t F3DEX2_G_DL = OPCODE(0xde);
constexpr int8_t F3DEX2_G_ENDDL = OPCODE(0xdf);
constexpr int8_t F3DEX2_G_SPNOOP = OPCODE(0xe0);
constexpr int8_t F3DEX2_G_RDPHALF_1 = OPCODE(0xe1);
constexpr int8_t F3DEX2_G_SETOTHERMODE_L = OPCODE(0xe2);
constexpr int8_t F3DEX2_G_SETOTHERMODE_H = OPCODE(0xe3);
constexpr int8_t F3DEX2_G_RDPHALF_2 = OPCODE(0xf1);
/* S2DEX2 object commands in the F3DEX2 numbering. */
constexpr int8_t F3DEX2_G_OBJ_RECTANGLE = OPCODE(0x01);
constexpr int8_t F3DEX2_G_OBJ_SPRITE = OPCODE(0x02);
constexpr int8_t F3DEX2_G_SELECT_DL = OPCODE(0x04);
constexpr int8_t F3DEX2_G_OBJ_LOADTXTR = OPCODE(0x05);
constexpr int8_t F3DEX2_G_OBJ_LDTX_SPRITE = OPCODE(0x06);
constexpr int8_t F3DEX2_G_OBJ_LDTX_RECT = OPCODE(0x07);
constexpr int8_t F3DEX2_G_OBJ_LDTX_RECT_R = OPCODE(0x08);
constexpr int8_t F3DEX2_G_BG_1CYC = OPCODE(0x09);
constexpr int8_t F3DEX2_G_BG_COPY = OPCODE(0x0a);
constexpr int8_t F3DEX2_G_OBJ_RENDERMODE = OPCODE(0x0b);
constexpr int8_t F3DEX2_G_OBJ_RECTANGLE_R = OPCODE(0xda);
constexpr int8_t F3DEX2_G_OBJ_MOVEMEM = OPCODE(0xdc);
constexpr int8_t F3DEX2_G_RDPHALF_0 = OPCODE(0xe4);

/* G_MTX parameter bits (second byte of word 0, XOR 0x01 on push in the command). */
constexpr int8_t F3DEX2_G_MTX_MODELVIEW = 0x00;
constexpr int8_t F3DEX2_G_MTX_PROJECTION = 0x04;
constexpr int8_t F3DEX2_G_MTX_MUL = 0x00;
constexpr int8_t F3DEX2_G_MTX_LOAD = 0x02;
constexpr int8_t F3DEX2_G_MTX_NOPUSH = 0x00;
constexpr int8_t F3DEX2_G_MTX_PUSH = 0x01;

/* Geometry-mode bits that differ from the F3D family. */
constexpr uint32_t F3DEX2_G_TEXTURE_ENABLE = 0x00000000;
constexpr uint32_t F3DEX2_G_SHADING_SMOOTH = 0x00200000;
constexpr uint32_t F3DEX2_G_CULL_FRONT = 0x00000200;
constexpr uint32_t F3DEX2_G_CULL_BACK = 0x00000400;
constexpr uint32_t F3DEX2_G_CULL_BOTH = 0x00000600;
constexpr uint32_t F3DEX2_G_CLIPPING = 0x00800000;

/* G_MOVEMEM indices (DMEM table entries) and light offsets. */
constexpr int8_t F3DEX2_G_MV_MMTX = 2;
constexpr int8_t F3DEX2_G_MV_PMTX = 6;
constexpr int8_t F3DEX2_G_MV_VIEWPORT = 8;
constexpr int8_t F3DEX2_G_MV_LIGHT = 10;
constexpr int8_t F3DEX2_G_MV_POINT = 12;
constexpr int8_t F3DEX2_G_MV_MATRIX = 14;
constexpr int8_t F3DEX2_G_MVO_LOOKATX = 0 * 24;
constexpr int8_t F3DEX2_G_MVO_LOOKATY = 1 * 24;
constexpr int8_t F3DEX2_G_MVO_L0 = 2 * 24;
constexpr int8_t F3DEX2_G_MVO_L1 = 3 * 24;
constexpr int8_t F3DEX2_G_MVO_L2 = 4 * 24;
constexpr int8_t F3DEX2_G_MVO_L3 = 5 * 24;
constexpr uint8_t F3DEX2_G_MVO_L4 = 6 * 24;
constexpr uint8_t F3DEX2_G_MVO_L5 = 7 * 24;
constexpr uint8_t F3DEX2_G_MVO_L6 = 8 * 24;
constexpr uint8_t F3DEX2_G_MVO_L7 = 9 * 24;

/* G_MOVEWORD index specific to F3DEX2. */
constexpr int8_t F3DEX2_G_MW_FORCEMTX = 0x0c;
