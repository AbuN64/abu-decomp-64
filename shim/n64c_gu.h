/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_GU_H
#define N64C_GU_H
#include "n64c_os.h"
/* Public libultra gu contracts, written from the documented behaviour. A float matrix is mf[row][column]
 * (a point is a row vector, translation in row 3). A fixed-point Mtx is 16 words (64 bytes, host word
 * order): words 0..7 hold the signed integer halves of two elements each (even element high), words 8..15
 * the unsigned fractions, element = integer + fraction / 65536. */
void guMtxIdentF(f32 mf[4][4]);
void guMtxF2L(f32 mf[4][4], void *mtx);
void guMtxL2F(f32 mf[4][4], const void *mtx);
/* Right-handed look-at view matrix; the look axis is stored negated (z toward the viewer). */
void guLookAtF(f32 mf[4][4], f32 xEye, f32 yEye, f32 zEye, f32 xAt, f32 yAt, f32 zAt, f32 xUp, f32 yUp, f32 zUp);
void guLookAt(void *mtx, f32 xEye, f32 yEye, f32 zEye, f32 xAt, f32 yAt, f32 zAt, f32 xUp, f32 yUp, f32 zUp);
/* fovy in degrees. perspNorm (may be NULL) receives 65536 * 2 / (near + far) as a u16 (0xFFFF when near + far <= 2). */
void guPerspectiveF(f32 mf[4][4], u16 *perspNorm, f32 fovy, f32 aspect, f32 near, f32 far, f32 scale);
void guPerspective(void *mtx, u16 *perspNorm, f32 fovy, f32 aspect, f32 near, f32 far, f32 scale);
/* libm sinf, cosf and sqrtf with the console library contract: sin and cos return 0 for |x| >= 2^28 and NaN. */
f32 n64c_sinf(f32 x);
f32 n64c_cosf(f32 x);
f32 n64c_sqrtf(f32 x);
#endif
