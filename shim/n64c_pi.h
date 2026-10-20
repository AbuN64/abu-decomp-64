/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_PI_H
#define N64C_PI_H
#include "n64c_os.h"
#ifdef __cplusplus
extern "C" {
#endif
typedef enum { N64C_RES_RAW, N64C_RES_SWAP16, N64C_RES_SWAP32, N64C_RES_STRUCT, N64C_RES_TAGGED } N64cResKind;
/* Resolve an original pointer to its native32-bit value. The binding owner
 * supplies function/data identities; PI never guesses from address bit patterns.
 * A zero pointer stays zero without calling the resolver. */
typedef int (*N64cRelocateFn)(u32 original,u32 *native,void *user);
void n64c_pi_set_relocator(N64cRelocateFn resolve,void *user);
/* TAGGED fields spec: "offset/width;tag=fields;tag=fields" in decimal (e.g. "14/2;1=4 4 4 2 2;2=..."): back-to-back
 * records of different sizes; the big-endian tag of width 1, 2 or 4 bytes at offset selects the record's field string
 * (its widths sum to the record size). A DMA subrange must start and end inside whole fields of whole records.
 * Register the ROM range [lo, hi) as one resource type. fields is used by STRUCT: widths in bytes, e.g. "222 2 22 1111"; p is a relocated32-bit pointer.
 * Resources must be disjoint whole records. DMA subranges must contain whole
 * multibyte fields. Failed reads have no completion event; discard their buffer. */
int n64c_pi_register(u32 lo, u32 hi, N64cResKind kind, const char *fields);
#ifdef __cplusplus
}
#endif
#endif
