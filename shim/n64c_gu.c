/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Public guMtxIdent/gSPMatrix format: the first eight words hold paired
 * signed integer halves, the next eight paired unsigned fractional halves.
 * The float helpers are written from the documented gu contracts (matrix
 * layout, view and projection algebra) and the host libm, without an SDK implementation. */
#include "n64c_gu.h"
#include <math.h>
#include <string.h>
void guMtxIdent(void *matrix)
{
    const u32 identity[16] = {
        0x00010000u, 0, 0x00000001u, 0,
        0, 0x00010000u, 0, 0x00000001u,
        0, 0, 0, 0, 0, 0, 0, 0
    };
    memcpy(matrix, identity, sizeof identity);
}
void guMtxIdentF(f32 mf[4][4])
{
    for (int r = 0; r < 4; r++)
        for (int c = 0; c < 4; c++) mf[r][c] = r == c ? 1.0f : 0.0f;
}
/* Truncate toward zero and saturate, as the console FPU converts. */
static s32 fixed16(f32 value)
{
    f64 scaled = (f64)value * 65536.0;
    if (!(scaled > -2147483648.0)) return scaled != scaled ? INT32_MAX : INT32_MIN;
    if (scaled >= 2147483647.0) return INT32_MAX;
    return (s32)scaled;
}
void guMtxF2L(f32 mf[4][4], void *mtx)
{
    u32 *words = mtx;
    for (int index = 0; index < 16; index += 2) {
        u32 even = (u32)fixed16(mf[index / 4][index % 4]);
        u32 odd = (u32)fixed16(mf[index / 4][index % 4 + 1]);
        words[index / 2] = (even & 0xFFFF0000u) | (odd >> 16);
        words[index / 2 + 8] = (even << 16) | (odd & 0xFFFFu);
    }
}
void guMtxL2F(f32 mf[4][4], const void *mtx)
{
    const u32 *words = mtx;
    for (int index = 0; index < 16; index += 2) {
        u32 whole = words[index / 2], fraction = words[index / 2 + 8];
        s32 even = (s32)((whole & 0xFFFF0000u) | (fraction >> 16));
        s32 odd = (s32)((whole << 16) | (fraction & 0xFFFFu));
        mf[index / 4][index % 4] = (f32)even / 65536.0f;
        mf[index / 4][index % 4 + 1] = (f32)odd / 65536.0f;
    }
}
void guLookAtF(f32 mf[4][4], f32 xEye, f32 yEye, f32 zEye, f32 xAt, f32 yAt, f32 zAt, f32 xUp, f32 yUp, f32 zUp)
{
    guMtxIdentF(mf);
    /* Look axis, negated and normalised. */
    f32 xLook = xAt - xEye, yLook = yAt - yEye, zLook = zAt - zEye;
    f32 length = -1.0f / sqrtf(xLook * xLook + yLook * yLook + zLook * zLook);
    xLook *= length; yLook *= length; zLook *= length;
    /* Right axis = up x look, normalised. */
    f32 xRight = yUp * zLook - zUp * yLook;
    f32 yRight = zUp * xLook - xUp * zLook;
    f32 zRight = xUp * yLook - yUp * xLook;
    length = 1.0f / sqrtf(xRight * xRight + yRight * yRight + zRight * zRight);
    xRight *= length; yRight *= length; zRight *= length;
    /* Up axis = look x right, normalised. */
    xUp = yLook * zRight - zLook * yRight;
    yUp = zLook * xRight - xLook * zRight;
    zUp = xLook * yRight - yLook * xRight;
    length = 1.0f / sqrtf(xUp * xUp + yUp * yUp + zUp * zUp);
    xUp *= length; yUp *= length; zUp *= length;
    mf[0][0] = xRight; mf[1][0] = yRight; mf[2][0] = zRight;
    mf[3][0] = -(xEye * xRight + yEye * yRight + zEye * zRight);
    mf[0][1] = xUp; mf[1][1] = yUp; mf[2][1] = zUp;
    mf[3][1] = -(xEye * xUp + yEye * yUp + zEye * zUp);
    mf[0][2] = xLook; mf[1][2] = yLook; mf[2][2] = zLook;
    mf[3][2] = -(xEye * xLook + yEye * yLook + zEye * zLook);
    mf[0][3] = 0.0f; mf[1][3] = 0.0f; mf[2][3] = 0.0f; mf[3][3] = 1.0f;
}
void guLookAt(void *mtx, f32 xEye, f32 yEye, f32 zEye, f32 xAt, f32 yAt, f32 zAt, f32 xUp, f32 yUp, f32 zUp)
{
    f32 mf[4][4];
    guLookAtF(mf, xEye, yEye, zEye, xAt, yAt, zAt, xUp, yUp, zUp);
    guMtxF2L(mf, mtx);
}
void guPerspectiveF(f32 mf[4][4], u16 *perspNorm, f32 fovy, f32 aspect, f32 near, f32 far, f32 scale)
{
    guMtxIdentF(mf);
    f32 angle = fovy * 3.14159265358979f / 180.0f * 0.5f;
    f32 cot = n64c_cosf(angle) / n64c_sinf(angle);
    mf[0][0] = cot / aspect;
    mf[1][1] = cot;
    mf[2][2] = (near + far) / (near - far);
    mf[2][3] = -1.0f;
    mf[3][2] = 2.0f * near * far / (near - far);
    mf[3][3] = 0.0f;
    for (int r = 0; r < 4; r++)
        for (int c = 0; c < 4; c++) mf[r][c] *= scale;
    if (perspNorm) {
        if (near + far <= 2.0f) *perspNorm = 0xFFFFu;
        else {
            f32 norm = 131072.0f / (near + far);
            u16 value = (u16)(u32)norm;
            *perspNorm = value ? value : 1u;
        }
    }
}
void guPerspective(void *mtx, u16 *perspNorm, f32 fovy, f32 aspect, f32 near, f32 far, f32 scale)
{
    f32 mf[4][4];
    guPerspectiveF(mf, perspNorm, fovy, aspect, near, far, scale);
    guMtxF2L(mf, mtx);
}
/* The console library returns zero beyond 2^28 (and for NaN) instead of reducing a huge argument. */
f32 n64c_sinf(f32 x) { return fabsf(x) < 268435456.0f ? sinf(x) : 0.0f; }
f32 n64c_cosf(f32 x) { return fabsf(x) < 268435456.0f ? cosf(x) : 0.0f; }
f32 n64c_sqrtf(f32 x) { return sqrtf(x); }
