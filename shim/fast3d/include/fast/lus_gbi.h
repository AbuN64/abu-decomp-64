/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Renderer-facing display-list interface for the pinned Fast3D interpreter.
 * Interface facts only, written for rage-ports:
 * - RDP command numbers, combiner and blender selectors, other-mode fields, image
 *   formats and render-mode bit compositions: n64brew wiki "Reality Display Processor/
 *   Commands" (Set Combine Mode, Set Other Modes, Set Tile, Set Texture Image) and
 *   the RDP blender description (P*A + M*B over 1/2 cycles).
 * - Geometry-mode bits shared by the F3D family, G_MOVEWORD indices and the in-memory
 *   layout of vertices, viewports, lights and S2DEX background/sprite objects:
 *   n64brew wiki "F3DEX2" and "S2DEX" pages.
 * - The LUS extension commands (OTR_G_*, *_WIDE, *_INTERP, G_EX_*, G_IM_SIZ_DD,
 *   G_MW_SEGMENT_INTERP) are LUS's own MIT interface; their numbers are the values the
 *   pinned interpreter dispatches on (LUS c6ecfdb include/fast/lus_gbi.h:39-76,147,
 *   201-227,980,1029).
 * Replaces the grey LUS header of the same name (AUDIT.md); it is never derived from gbi.h. */
#pragma once
#include <stdint.h>
#include "f3dex.h"
#include "f3dex2.h"
#ifndef OPCODE
#define OPCODE(x) (int8_t)(x)
#endif
#ifndef _LANGUAGE_C
#define _LANGUAGE_C
#endif

namespace Fast {

/* RDP commands (opcode byte of word 0). */
constexpr int8_t RDP_G_TEXRECT = OPCODE(0xe4);
constexpr int8_t RDP_G_TEXRECTFLIP = OPCODE(0xe5);
constexpr int8_t RDP_G_RDPLOADSYNC = OPCODE(0xe6);
constexpr int8_t RDP_G_RDPPIPESYNC = OPCODE(0xe7);
constexpr int8_t RDP_G_RDPTILESYNC = OPCODE(0xe8);
constexpr int8_t RDP_G_RDPFULLSYNC = OPCODE(0xe9);
constexpr int8_t RDP_G_SETKEYGB = OPCODE(0xea);
constexpr int8_t RDP_G_SETKEYR = OPCODE(0xeb);
constexpr int8_t RDP_G_SETCONVERT = OPCODE(0xec);
constexpr int8_t RDP_G_SETSCISSOR = OPCODE(0xed);
constexpr int8_t RDP_G_SETPRIMDEPTH = OPCODE(0xee);
constexpr int8_t RDP_G_RDPSETOTHERMODE = OPCODE(0xef);
constexpr int8_t RDP_G_LOADTLUT = OPCODE(0xf0);
constexpr int8_t RDP_G_SETTILESIZE = OPCODE(0xf2);
constexpr int8_t RDP_G_LOADBLOCK = OPCODE(0xf3);
constexpr int8_t RDP_G_LOADTILE = OPCODE(0xf4);
constexpr int8_t RDP_G_SETTILE = OPCODE(0xf5);
constexpr int8_t RDP_G_FILLRECT = OPCODE(0xf6);
constexpr int8_t RDP_G_SETFILLCOLOR = OPCODE(0xf7);
constexpr int8_t RDP_G_SETFOGCOLOR = OPCODE(0xf8);
constexpr int8_t RDP_G_SETBLENDCOLOR = OPCODE(0xf9);
constexpr int8_t RDP_G_SETPRIMCOLOR = OPCODE(0xfa);
constexpr int8_t RDP_G_SETENVCOLOR = OPCODE(0xfb);
constexpr int8_t RDP_G_SETCOMBINE = OPCODE(0xfc);
constexpr int8_t RDP_G_SETTIMG = OPCODE(0xfd);
constexpr int8_t RDP_G_SETZIMG = OPCODE(0xfe);
constexpr int8_t RDP_G_SETCIMG = OPCODE(0xff);

/* LUS extension commands (LUS's own interface, values as dispatched by the pin). */
constexpr int8_t OTR_G_SETTIMG_OTR_HASH = OPCODE(0x20);
constexpr int8_t OTR_G_SETFB = OPCODE(0x21);
constexpr int8_t OTR_G_RESETFB = OPCODE(0x22);
constexpr int8_t OTR_G_SETTIMG_FB = OPCODE(0x23);
constexpr int8_t OTR_G_VTX_OTR_FILEPATH = OPCODE(0x24);
constexpr int8_t OTR_G_SETTIMG_OTR_FILEPATH = OPCODE(0x25);
constexpr int8_t OTR_G_TRI1_OTR = OPCODE(0x26);
constexpr int8_t OTR_G_DL_OTR_FILEPATH = OPCODE(0x27);
constexpr int8_t OTR_G_PUSHCD = OPCODE(0x28);
constexpr int8_t OTR_G_MTX_OTR_FILEPATH = OPCODE(0x29);
constexpr int8_t OTR_G_DL_OTR_HASH = OPCODE(0x31);
constexpr int8_t OTR_G_VTX_OTR_HASH = OPCODE(0x32);
constexpr int8_t OTR_G_MARKER = OPCODE(0x33);
constexpr int8_t OTR_G_INVALTEXCACHE = OPCODE(0x34);
constexpr int8_t OTR_G_BRANCH_Z_OTR = OPCODE(0x35);
constexpr int8_t OTR_G_MTX_OTR = OPCODE(0x36);
constexpr int8_t OTR_G_TEXRECT_WIDE = OPCODE(0x37);
constexpr int8_t OTR_G_FILLWIDERECT = OPCODE(0x38);
constexpr int8_t OTR_G_SETGRAYSCALE = OPCODE(0x39);
constexpr int8_t OTR_G_EXTRAGEOMETRYMODE = OPCODE(0x3a);
constexpr int8_t OTR_G_COPYFB = OPCODE(0x3b);
constexpr int8_t OTR_G_IMAGERECT = OPCODE(0x3c);
constexpr int8_t OTR_G_DL_INDEX = OPCODE(0x3d);
constexpr int8_t OTR_G_READFB = OPCODE(0x3e);
constexpr int8_t OTR_G_REGBLENDEDTEX = OPCODE(0x3f);
constexpr int8_t OTR_G_SETINTENSITY = OPCODE(0x40);
constexpr int8_t OTR_G_MOVEMEM_HASH = OPCODE(0x42);
constexpr int8_t OTR_G_PUSH_SHADER = OPCODE(0x43);
constexpr int8_t OTR_G_POP_SHADER = OPCODE(0x44);
constexpr int8_t RDP_G_SETTILESIZE_INTERP = OPCODE(0x45);
constexpr int8_t RDP_G_SETTARGETINTERPINDEX = OPCODE(0x46);
constexpr int8_t RDP_G_LOADBLOCK_WIDE = OPCODE(0x47);
constexpr int8_t RDP_G_VTX_WIDE = OPCODE(0x48);
constexpr int8_t RDP_G_TRI1_WIDE = OPCODE(0x49);

/* Geometry-mode bits common to F3D and F3DEX2 (the differing ones live in f3dex*.h). */
#define G_ZBUFFER 0x00000001
#define G_SHADE 0x00000004
#define G_FOG 0x00010000
#define G_LIGHTING 0x00020000
#define G_TEXTURE_GEN 0x00040000
#define G_TEXTURE_GEN_LINEAR 0x00080000
#define G_LIGHTING_POSITIONAL 0x00400000
/* LUS extra geometry mode (OTR_G_EXTRAGEOMETRYMODE). */
#define G_EX_INVERT_CULLING 0x00000001
#define G_EX_ALWAYS_EXECUTE_BRANCH 0x00000002
/* G_TEXTURE: texture coordinates carry this many fraction bits beyond s10.5. */
#define G_TEXTURE_IMAGE_FRAC 2

/* Texel formats and sizes (Set Tile / Set Texture Image fields). */
#define G_IM_FMT_RGBA 0
#define G_IM_FMT_YUV 1
#define G_IM_FMT_CI 2
#define G_IM_FMT_IA 3
#define G_IM_FMT_I 4
#define G_IM_SIZ_4b 0
#define G_IM_SIZ_8b 1
#define G_IM_SIZ_16b 2
#define G_IM_SIZ_32b 3
#define G_IM_SIZ_DD 5
/* Load Block moves 16-bit words; 8-bit and 16-bit textures load as 16b, a 16b line is 2 bytes/texel. */
#define G_IM_SIZ_8b_LOAD_BLOCK G_IM_SIZ_16b
#define G_IM_SIZ_16b_LOAD_BLOCK G_IM_SIZ_16b
#define G_IM_SIZ_16b_LINE_BYTES 2

/* Tile descriptor fields. */
#define G_TX_NOMIRROR 0
#define G_TX_WRAP 0
#define G_TX_MIRROR 0x1
#define G_TX_CLAMP 0x2
#define G_TX_NOMASK 0
#define G_TX_LOADTILE 7
#define G_TX_RENDERTILE 0

/* Color combiner inputs: (A - B) * C + D. Several numbers mean different inputs per slot. */
#define G_CCMUX_COMBINED 0
#define G_CCMUX_TEXEL0 1
#define G_CCMUX_TEXEL1 2
#define G_CCMUX_PRIMITIVE 3
#define G_CCMUX_SHADE 4
#define G_CCMUX_ENVIRONMENT 5
#define G_CCMUX_CENTER 6
#define G_CCMUX_SCALE 6
#define G_CCMUX_COMBINED_ALPHA 7
#define G_CCMUX_TEXEL0_ALPHA 8
#define G_CCMUX_TEXEL1_ALPHA 9
#define G_CCMUX_PRIMITIVE_ALPHA 10
#define G_CCMUX_SHADE_ALPHA 11
#define G_CCMUX_ENV_ALPHA 12
#define G_CCMUX_LOD_FRACTION 13
#define G_CCMUX_PRIM_LOD_FRAC 14
#define G_CCMUX_NOISE 7
#define G_CCMUX_K4 7
#define G_CCMUX_K5 15
#define G_CCMUX_1 6
#define G_CCMUX_0 31
/* The interpreter's own selector numbers for inputs that share a mux code (LUS c6ecfdb
 * include/fast/lus_gbi.h, the KEY_ and CONVERT_ entries): they never appear in a command. */
#define G_CCMUX_KEY_CENTER 20
#define G_CCMUX_KEY_SCALE 21
#define G_CCMUX_CONVERT_K4 22
#define G_CCMUX_CONVERT_K5 23
/* Alpha combiner inputs. */
#define G_ACMUX_COMBINED 0
#define G_ACMUX_TEXEL0 1
#define G_ACMUX_TEXEL1 2
#define G_ACMUX_PRIMITIVE 3
#define G_ACMUX_SHADE 4
#define G_ACMUX_ENVIRONMENT 5
#define G_ACMUX_LOD_FRACTION 0
#define G_ACMUX_PRIM_LOD_FRAC 6
#define G_ACMUX_1 6
#define G_ACMUX_0 7

/* Other modes, low word: field shifts. */
#define G_MDSFT_ALPHACOMPARE 0
#define G_MDSFT_ZSRCSEL 2
#define G_MDSFT_RENDERMODE 3
#define G_MDSFT_BLENDER 16
/* Other modes, high word: field shifts. */
#define G_MDSFT_BLENDMASK 0
#define G_MDSFT_ALPHADITHER 4
#define G_MDSFT_RGBDITHER 6
#define G_MDSFT_COMBKEY 8
#define G_MDSFT_TEXTCONV 9
#define G_MDSFT_TEXTFILT 12
#define G_MDSFT_TEXTLUT 14
#define G_MDSFT_TEXTLOD 16
#define G_MDSFT_TEXTDETAIL 17
#define G_MDSFT_TEXTPERSP 19
#define G_MDSFT_CYCLETYPE 20
#define G_MDSFT_COLORDITHER 22
#define G_MDSFT_PIPELINE 23
/* Other-mode field values (already shifted into place). */
#define G_PM_NPRIMITIVE (0 << G_MDSFT_PIPELINE)
#define G_PM_1PRIMITIVE (1 << G_MDSFT_PIPELINE)
#define G_CYC_1CYCLE (0 << G_MDSFT_CYCLETYPE)
#define G_CYC_2CYCLE (1 << G_MDSFT_CYCLETYPE)
#define G_CYC_COPY (2 << G_MDSFT_CYCLETYPE)
#define G_CYC_FILL (3 << G_MDSFT_CYCLETYPE)
#define G_TP_NONE (0 << G_MDSFT_TEXTPERSP)
#define G_TP_PERSP (1 << G_MDSFT_TEXTPERSP)
#define G_TD_CLAMP (0 << G_MDSFT_TEXTDETAIL)
#define G_TD_SHARPEN (1 << G_MDSFT_TEXTDETAIL)
#define G_TD_DETAIL (2 << G_MDSFT_TEXTDETAIL)
#define G_TL_TILE (0 << G_MDSFT_TEXTLOD)
#define G_TL_LOD (1 << G_MDSFT_TEXTLOD)
#define G_TT_NONE (0 << G_MDSFT_TEXTLUT)
#define G_TT_RGBA16 (2 << G_MDSFT_TEXTLUT)
#define G_TT_IA16 (3 << G_MDSFT_TEXTLUT)
#define G_TF_POINT (0 << G_MDSFT_TEXTFILT)
#define G_TF_AVERAGE (3 << G_MDSFT_TEXTFILT)
#define G_TF_BILERP (2 << G_MDSFT_TEXTFILT)
#define G_TC_CONV (0 << G_MDSFT_TEXTCONV)
#define G_TC_FILTCONV (5 << G_MDSFT_TEXTCONV)
#define G_TC_FILT (6 << G_MDSFT_TEXTCONV)
#define G_CK_NONE (0 << G_MDSFT_COMBKEY)
#define G_CK_KEY (1 << G_MDSFT_COMBKEY)
#define G_CD_MAGICSQ (0 << G_MDSFT_RGBDITHER)
#define G_CD_BAYER (1 << G_MDSFT_RGBDITHER)
#define G_CD_NOISE (2 << G_MDSFT_RGBDITHER)
#define G_AD_PATTERN (0 << G_MDSFT_ALPHADITHER)
#define G_AD_NOTPATTERN (1 << G_MDSFT_ALPHADITHER)
#define G_AD_NOISE (2 << G_MDSFT_ALPHADITHER)
#define G_AD_DISABLE (3 << G_MDSFT_ALPHADITHER)
#define G_AC_NONE (0 << G_MDSFT_ALPHACOMPARE)
#define G_AC_THRESHOLD (1 << G_MDSFT_ALPHACOMPARE)
#define G_AC_DITHER (3 << G_MDSFT_ALPHACOMPARE)
#define G_ZS_PIXEL (0 << G_MDSFT_ZSRCSEL)
#define G_ZS_PRIM (1 << G_MDSFT_ZSRCSEL)

/* Render mode: blender inputs. Color P/M: input, memory, blend, fog. Alpha A: input, fog,
 * shade, 0; alpha B: 1-A, memory, 1, 0. */
#define G_BL_CLR_IN 0
#define G_BL_CLR_MEM 1
#define G_BL_CLR_BL 2
#define G_BL_CLR_FOG 3
#define G_BL_1MA 0
#define G_BL_A_MEM 1
#define G_BL_A_IN 0
#define G_BL_A_FOG 1
#define G_BL_A_SHADE 2
#define G_BL_1 2
#define G_BL_0 3
/* Blender words for cycle 1 and cycle 2 (P, A, M, B). */
#define GBL_c1(m1a, m1b, m2a, m2b) \
    ((uint32_t)(m1a) << 30 | (uint32_t)(m1b) << 26 | (uint32_t)(m2a) << 22 | (uint32_t)(m2b) << 18)
#define GBL_c2(m1a, m1b, m2a, m2b) \
    ((uint32_t)(m1a) << 28 | (uint32_t)(m1b) << 24 | (uint32_t)(m2a) << 20 | (uint32_t)(m2b) << 16)
/* Render mode: coverage and depth control bits. */
#define AA_EN 0x8
#define Z_CMP 0x10
#define Z_UPD 0x20
#define IM_RD 0x40
#define CLR_ON_CVG 0x80
#define CVG_DST_CLAMP 0
#define CVG_DST_WRAP 0x100
#define CVG_DST_FULL 0x200
#define CVG_DST_SAVE 0x300
#define ZMODE_OPA 0
#define ZMODE_INTER 0x400
#define ZMODE_XLU 0x800
#define ZMODE_DEC 0xc00
#define CVG_X_ALPHA 0x1000
#define ALPHA_CVG_SEL 0x2000
#define FORCE_BL 0x4000
#define TEX_EDGE 0x0000

/* Render modes composed from those bits (clk = blender cycle 1 or 2). Opaque surfaces keep
 * memory weighted by memory alpha; translucent, terrain and point-sampled modes weight it by 1 - A. */
#define N64C_RM_OPAQUE_MEM(clk) GBL_c##clk(G_BL_CLR_IN, G_BL_A_IN, G_BL_CLR_MEM, G_BL_A_MEM)
#define N64C_RM_BLEND_MEM(clk) GBL_c##clk(G_BL_CLR_IN, G_BL_A_IN, G_BL_CLR_MEM, G_BL_1MA)
#define RM_AA_ZB_OPA_SURF(clk) \
    (AA_EN | Z_CMP | Z_UPD | IM_RD | CVG_DST_CLAMP | ZMODE_OPA | ALPHA_CVG_SEL | N64C_RM_OPAQUE_MEM(clk))
#define RM_AA_ZB_XLU_SURF(clk) \
    (AA_EN | Z_CMP | IM_RD | CVG_DST_WRAP | CLR_ON_CVG | FORCE_BL | ZMODE_XLU | N64C_RM_BLEND_MEM(clk))
#define RM_AA_ZB_OPA_DECAL(clk) \
    (AA_EN | Z_CMP | IM_RD | CVG_DST_WRAP | ALPHA_CVG_SEL | ZMODE_DEC | N64C_RM_OPAQUE_MEM(clk))
#define RM_AA_ZB_XLU_DECAL(clk) \
    (AA_EN | Z_CMP | IM_RD | CVG_DST_WRAP | CLR_ON_CVG | FORCE_BL | ZMODE_DEC | N64C_RM_BLEND_MEM(clk))
#define RM_AA_ZB_OPA_INTER(clk) \
    (AA_EN | Z_CMP | Z_UPD | IM_RD | CVG_DST_CLAMP | ALPHA_CVG_SEL | ZMODE_INTER | N64C_RM_OPAQUE_MEM(clk))
#define RM_AA_ZB_XLU_INTER(clk) \
    (AA_EN | Z_CMP | IM_RD | CVG_DST_WRAP | CLR_ON_CVG | FORCE_BL | ZMODE_INTER | N64C_RM_BLEND_MEM(clk))
#define RM_AA_ZB_XLU_LINE(clk)                                                                        \
    (AA_EN | Z_CMP | IM_RD | CVG_DST_CLAMP | CVG_X_ALPHA | ALPHA_CVG_SEL | FORCE_BL | ZMODE_XLU | \
     N64C_RM_BLEND_MEM(clk))
#define RM_AA_ZB_DEC_LINE(clk)                                                                       \
    (AA_EN | Z_CMP | IM_RD | CVG_DST_SAVE | CVG_X_ALPHA | ALPHA_CVG_SEL | FORCE_BL | ZMODE_DEC | \
     N64C_RM_BLEND_MEM(clk))
#define RM_AA_ZB_TEX_EDGE(clk)                                                                           \
    (AA_EN | Z_CMP | Z_UPD | IM_RD | CVG_DST_CLAMP | CVG_X_ALPHA | ALPHA_CVG_SEL | ZMODE_OPA | TEX_EDGE | \
     N64C_RM_OPAQUE_MEM(clk))
#define RM_AA_ZB_TEX_INTER(clk)                                                                            \
    (AA_EN | Z_CMP | Z_UPD | IM_RD | CVG_DST_CLAMP | CVG_X_ALPHA | ALPHA_CVG_SEL | ZMODE_INTER | TEX_EDGE | \
     N64C_RM_OPAQUE_MEM(clk))
#define RM_AA_ZB_SUB_SURF(clk) \
    (AA_EN | Z_CMP | Z_UPD | IM_RD | CVG_DST_FULL | ZMODE_OPA | ALPHA_CVG_SEL | N64C_RM_OPAQUE_MEM(clk))
#define RM_AA_ZB_PCL_SURF(clk) \
    (AA_EN | Z_CMP | Z_UPD | IM_RD | CVG_DST_CLAMP | ZMODE_OPA | G_AC_DITHER | N64C_RM_BLEND_MEM(clk))
#define RM_AA_ZB_OPA_TERR(clk) \
    (AA_EN | Z_CMP | Z_UPD | IM_RD | CVG_DST_CLAMP | ZMODE_OPA | ALPHA_CVG_SEL | N64C_RM_BLEND_MEM(clk))
#define RM_AA_ZB_TEX_TERR(clk)                                                                           \
    (AA_EN | Z_CMP | Z_UPD | IM_RD | CVG_DST_CLAMP | CVG_X_ALPHA | ALPHA_CVG_SEL | ZMODE_OPA | TEX_EDGE | \
     N64C_RM_BLEND_MEM(clk))
#define RM_AA_ZB_SUB_TERR(clk) \
    (AA_EN | Z_CMP | Z_UPD | IM_RD | CVG_DST_FULL | ZMODE_OPA | ALPHA_CVG_SEL | N64C_RM_BLEND_MEM(clk))
#define RM_ZB_OPA_SURF(clk) (Z_CMP | Z_UPD | CVG_DST_FULL | ALPHA_CVG_SEL | ZMODE_OPA | N64C_RM_OPAQUE_MEM(clk))
#define RM_ZB_XLU_SURF(clk) (Z_CMP | IM_RD | CVG_DST_FULL | FORCE_BL | ZMODE_XLU | N64C_RM_BLEND_MEM(clk))
#define RM_ZB_OPA_DECAL(clk) (Z_CMP | CVG_DST_FULL | ALPHA_CVG_SEL | ZMODE_DEC | N64C_RM_OPAQUE_MEM(clk))
#define RM_ZB_XLU_DECAL(clk) (Z_CMP | IM_RD | CVG_DST_FULL | FORCE_BL | ZMODE_DEC | N64C_RM_BLEND_MEM(clk))
#define RM_ZB_CLD_SURF(clk) (Z_CMP | IM_RD | CVG_DST_SAVE | FORCE_BL | ZMODE_XLU | N64C_RM_BLEND_MEM(clk))
#define RM_OPA_SURF(clk) \
    (CVG_DST_CLAMP | FORCE_BL | ZMODE_OPA | GBL_c##clk(G_BL_CLR_IN, G_BL_0, G_BL_CLR_IN, G_BL_1))
#define RM_ADD(clk) \
    (IM_RD | CVG_DST_SAVE | FORCE_BL | ZMODE_OPA | GBL_c##clk(G_BL_CLR_IN, G_BL_A_FOG, G_BL_CLR_MEM, G_BL_1))
#define RM_NOOP(clk) GBL_c##clk(0, 0, 0, 0)

#define G_RM_AA_ZB_OPA_SURF RM_AA_ZB_OPA_SURF(1)
#define G_RM_AA_ZB_OPA_SURF2 RM_AA_ZB_OPA_SURF(2)
#define G_RM_AA_ZB_XLU_SURF RM_AA_ZB_XLU_SURF(1)
#define G_RM_AA_ZB_XLU_SURF2 RM_AA_ZB_XLU_SURF(2)
#define G_RM_AA_ZB_OPA_DECAL RM_AA_ZB_OPA_DECAL(1)
#define G_RM_AA_ZB_OPA_DECAL2 RM_AA_ZB_OPA_DECAL(2)
#define G_RM_AA_ZB_XLU_DECAL RM_AA_ZB_XLU_DECAL(1)
#define G_RM_AA_ZB_XLU_DECAL2 RM_AA_ZB_XLU_DECAL(2)
#define G_RM_AA_ZB_OPA_INTER RM_AA_ZB_OPA_INTER(1)
#define G_RM_AA_ZB_OPA_INTER2 RM_AA_ZB_OPA_INTER(2)
#define G_RM_AA_ZB_XLU_INTER RM_AA_ZB_XLU_INTER(1)
#define G_RM_AA_ZB_XLU_INTER2 RM_AA_ZB_XLU_INTER(2)
#define G_RM_AA_ZB_XLU_LINE RM_AA_ZB_XLU_LINE(1)
#define G_RM_AA_ZB_XLU_LINE2 RM_AA_ZB_XLU_LINE(2)
#define G_RM_AA_ZB_DEC_LINE RM_AA_ZB_DEC_LINE(1)
#define G_RM_AA_ZB_DEC_LINE2 RM_AA_ZB_DEC_LINE(2)
#define G_RM_AA_ZB_TEX_EDGE RM_AA_ZB_TEX_EDGE(1)
#define G_RM_AA_ZB_TEX_EDGE2 RM_AA_ZB_TEX_EDGE(2)
#define G_RM_AA_ZB_TEX_INTER RM_AA_ZB_TEX_INTER(1)
#define G_RM_AA_ZB_TEX_INTER2 RM_AA_ZB_TEX_INTER(2)
#define G_RM_AA_ZB_SUB_SURF RM_AA_ZB_SUB_SURF(1)
#define G_RM_AA_ZB_SUB_SURF2 RM_AA_ZB_SUB_SURF(2)
#define G_RM_AA_ZB_PCL_SURF RM_AA_ZB_PCL_SURF(1)
#define G_RM_AA_ZB_PCL_SURF2 RM_AA_ZB_PCL_SURF(2)
#define G_RM_AA_ZB_OPA_TERR RM_AA_ZB_OPA_TERR(1)
#define G_RM_AA_ZB_OPA_TERR2 RM_AA_ZB_OPA_TERR(2)
#define G_RM_AA_ZB_TEX_TERR RM_AA_ZB_TEX_TERR(1)
#define G_RM_AA_ZB_TEX_TERR2 RM_AA_ZB_TEX_TERR(2)
#define G_RM_AA_ZB_SUB_TERR RM_AA_ZB_SUB_TERR(1)
#define G_RM_AA_ZB_SUB_TERR2 RM_AA_ZB_SUB_TERR(2)
#define G_RM_ZB_OPA_SURF RM_ZB_OPA_SURF(1)
#define G_RM_ZB_OPA_SURF2 RM_ZB_OPA_SURF(2)
#define G_RM_ZB_XLU_SURF RM_ZB_XLU_SURF(1)
#define G_RM_ZB_XLU_SURF2 RM_ZB_XLU_SURF(2)
#define G_RM_ZB_OPA_DECAL RM_ZB_OPA_DECAL(1)
#define G_RM_ZB_OPA_DECAL2 RM_ZB_OPA_DECAL(2)
#define G_RM_ZB_XLU_DECAL RM_ZB_XLU_DECAL(1)
#define G_RM_ZB_XLU_DECAL2 RM_ZB_XLU_DECAL(2)
#define G_RM_ZB_CLD_SURF RM_ZB_CLD_SURF(1)
#define G_RM_ZB_CLD_SURF2 RM_ZB_CLD_SURF(2)
#define G_RM_OPA_SURF RM_OPA_SURF(1)
#define G_RM_OPA_SURF2 RM_OPA_SURF(2)
#define G_RM_ADD RM_ADD(1)
#define G_RM_ADD2 RM_ADD(2)
#define G_RM_NOOP RM_NOOP(1)
#define G_RM_NOOP2 RM_NOOP(2)
/* First-cycle blends that feed the second cycle. */
#define G_RM_FOG_SHADE_A GBL_c1(G_BL_CLR_FOG, G_BL_A_SHADE, G_BL_CLR_IN, G_BL_1MA)
#define G_RM_FOG_PRIM_A GBL_c1(G_BL_CLR_FOG, G_BL_A_FOG, G_BL_CLR_IN, G_BL_1MA)
#define G_RM_PASS GBL_c1(G_BL_CLR_IN, G_BL_0, G_BL_CLR_IN, G_BL_1)

/* G_MOVEWORD indices and offsets. */
#define G_MW_NUMLIGHT 0x02
#define G_MW_SEGMENT 0x06
#define G_MW_SEGMENT_INTERP 0x07
#define G_MW_FOG 0x08
#define G_MWO_POINT_ST 0x14

/* S2DEX background load method and flip flag (uObjBg.imageLoad / imageFlip). */
#define G_BGLT_LOADBLOCK 0x0033
#define G_BGLT_LOADTILE 0xfff4
#define G_BG_FLAG_FLIPS 0x01
#define G_BG_FLAG_FLIPT 0x10

/* In-memory shapes read by the interpreter (16-byte vertex, 16-byte viewport,
 * 16-byte light record, 8-byte command on a 32-bit process). */
typedef struct {
    short ob[3];          /* model-space position */
    unsigned short flag;
    short tc[2];          /* texture coordinate, s10.5 */
    unsigned char cn[4];  /* vertex color and alpha */
} F3DVtx_t;
typedef struct {
    short ob[3];
    unsigned short flag;
    short tc[2];
    signed char n[3];     /* normal */
    unsigned char a;      /* alpha */
} F3DVtx_tn;
typedef union F3DVtx {
    F3DVtx_t v;
    F3DVtx_tn n;
    long long int force_structure_alignment;
} F3DVtx;

typedef struct {
    short vscale[4];      /* x, y, z scale, 2 fraction bits */
    short vtrans[4];      /* x, y, z translation, 2 fraction bits */
} F3DVp_t;
typedef union {
    F3DVp_t vp;
    long long int force_structure_alignment;
} F3DVp;

typedef struct {
    unsigned char col[3];
    char pad1;
    unsigned char colc[3]; /* copy of col */
    char pad2;
    signed char dir[3];    /* normalized direction */
    char pad3;
} F3DLight_t;
typedef struct {
    unsigned char col[3];
    unsigned char unk3;
    unsigned char colc[3];
    unsigned char unk7;
    short pos[3];          /* point-light position */
    unsigned char unkE;
} F3DPointLight_t;
typedef struct {
    unsigned char col[3];
    char pad1;
    unsigned char colc[3];
    char pad2;
} F3DAmbient_t;
typedef union {
    F3DLight_t l;
    F3DPointLight_t p;
    long long int force_structure_alignment[2];
} F3DLight;
typedef union {
    F3DAmbient_t l;
    long long int force_structure_alignment[1];
} F3DAmbient;

/* One display-list command: two pointer-width words (8 bytes on the 32-bit port). */
typedef struct {
    uintptr_t w0;
    uintptr_t w1;
} F3DGwords;
typedef union F3DGfx {
    F3DGwords words;
    long long int force_structure_alignment;
} F3DGfx;

/* S2DEX background (unscaled and scaled forms share the first 32 bytes). */
typedef struct {
    unsigned short imageX;  /* texture origin x, u10.5 */
    unsigned short imageW;  /* texture width, u10.2 */
    short frameX;           /* frame origin x, s10.2 */
    unsigned short frameW;  /* frame width, u10.2 */
    unsigned short imageY;
    unsigned short imageH;
    short frameY;
    unsigned short frameH;
    unsigned long long int* imagePtr;
    unsigned short imageLoad; /* G_BGLT_* */
    unsigned char imageFmt;   /* G_IM_FMT_* */
    unsigned char imageSiz;   /* G_IM_SIZ_* */
    unsigned short imagePal;
    unsigned short imageFlip; /* G_BG_FLAG_FLIPS */
    unsigned short tmemW;
    unsigned short tmemH;
    unsigned short tmemLoadSH;
    unsigned short tmemLoadTH;
    unsigned short tmemSizeW;
    unsigned short tmemSize;
} F3DuObjBg_t;
typedef struct {
    unsigned short imageX;
    unsigned short imageW;
    short frameX;
    unsigned short frameW;
    unsigned short imageY;
    unsigned short imageH;
    short frameY;
    unsigned short frameH;
    unsigned long long int* imagePtr;
    unsigned short imageLoad;
    unsigned char imageFmt;
    unsigned char imageSiz;
    unsigned short imagePal;
    unsigned short imageFlip;
    unsigned short scaleW;  /* u5.10 */
    unsigned short scaleH;  /* u5.10 */
    int imageYorig;         /* s20.5 */
    unsigned char padding[4];
} F3DuObjScaleBg_t;
typedef union {
    F3DuObjBg_t b;
    F3DuObjScaleBg_t s;
    long long int force_structure_alignment;
} F3DuObjBg;

/* S2DEX sprite object. */
typedef struct {
    short objX;             /* s10.2 */
    unsigned short scaleW;  /* u5.10 */
    unsigned short imageW;  /* u10.5 */
    unsigned short paddingX;
    short objY;
    unsigned short scaleH;
    unsigned short imageH;
    unsigned short paddingY;
    unsigned short imageStride; /* 64-bit words per texture line */
    unsigned short imageAdrs;   /* TMEM address, 64-bit words */
    unsigned char imageFmt;
    unsigned char imageSiz;
    unsigned char imagePal;
    unsigned char imageFlags;   /* G_OBJ_FLAG_* */
} F3DuObjSprite_t;
typedef union {
    F3DuObjSprite_t s;
    long long int force_structure_alignment;
} F3DuObjSprite;

static_assert(sizeof(F3DVtx) == 16, "vertex is 16 bytes");
static_assert(sizeof(F3DVp) == 16, "viewport is 16 bytes");
static_assert(sizeof(F3DLight) == 16, "light is 16 bytes");
static_assert(sizeof(F3DuObjSprite) == 24, "sprite object is 24 bytes");

} // namespace Fast
