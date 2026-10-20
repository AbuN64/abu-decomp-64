/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_RNC_H
#define N64C_RNC_H
#include "n64c_os.h"

/* RNC method 1 (Rob Northen Compression, public format) unpacker. src is the
 * packed data after the 18-byte header ("RNC" 01, big-endian unpacked and packed
 * sizes, two CRC-16, leeway, chunk count), packed_size and unpacked_size come
 * from that header. Corrupt or truncated input stops the boot (exit 78). */
void n64c_rnc1_unpack(const void *src, void *dst, s32 packed_size, s32 unpacked_size);
/* RNC method 2 unpacker (public format, no Huffman tables). header is the 18-byte "RNC" 02 header with the
 * packed data after it; packed_size and unpacked_size must equal the header's, and both header CRC-16s must
 * match. Corrupt, truncated or mismatched input stops the boot (exit 78). */
void n64c_rnc2_unpack(const void *header, void *dst, s32 packed_size, s32 unpacked_size);
/* The same with the 18-byte big-endian header apart from the packed data (a header the PI layer put in host order). */
void n64c_rnc2_unpack_parts(const void *header, const void *data, void *dst, s32 packed_size, s32 unpacked_size);
/* CRC-16 of the RNC header (reflected polynomial 0xA001, initial value 0). */
u16 n64c_rnc_crc16(const void *data, u32 size);
#endif
