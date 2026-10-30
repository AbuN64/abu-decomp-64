/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_GUEST_GU_H
#define N64C_GUEST_GU_H
/* Matrix storage crosses the original C ABI as an untyped byte pointer.
 * n64c_gu.c supplies the hand-written native implementation. */
void guMtxIdentF(unsigned char *matrix);
#endif
