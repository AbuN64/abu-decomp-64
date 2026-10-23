/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Controller Pak image: the 32 KiB memory card that plugs into a controller. Layout written from the public
 * description of the card (128 pages of 256 bytes):
 *   page 0      id area: four 32-byte id blocks at 0x20, 0x60, 0x80, 0xC0 and the 32-byte label at 0xE0
 *   page 1, 2   index table (128 big-endian u16, one per page; page 2 is a second copy)
 *   page 3, 4   note table (16 notes of 32 bytes)
 *   page 5..127 data pages
 * Standard .mpk files are this image, so saves from other emulators and tools load as they are. */
#ifndef N64C_CPAK_H
#define N64C_CPAK_H
#include "n64c_os.h"
#ifdef __cplusplus
extern "C" {
#endif
#define N64C_CPAK_BYTES 0x8000u
#define N64C_CPAK_PAGE 256u
#define N64C_CPAK_PAGES 128u
#define N64C_CPAK_FIRST_DATA_PAGE 5u
#define N64C_CPAK_NOTES 16u
#define N64C_CPAK_NOTE_BYTES 32u
#define N64C_CPAK_INDEX_END 0x0001u  /* last page of a file */
#define N64C_CPAK_INDEX_FREE 0x0003u /* unused page */
/* Fill a blank card: fixed serial (replays must start from identical bytes), valid id blocks, all data pages free. */
void n64c_cpak_format(u8 *image);
/* Index table: sum of the low bytes of entries 5..127 into entry 0. Call after any change, then copy to page 2. */
void n64c_cpak_index_seal(u8 *image);
/* Id block at offset (0x20 etc.): checksums and header fields hold. */
int n64c_cpak_id_ok(const u8 *id_block);
/* The two checksums of the first 28 bytes of an id block. */
void n64c_cpak_id_sums(const u8 *id_block, u16 *sum, u16 *inverted);
/* CRC-8 (polynomial 0x85) the card sends after 32 data bytes and after a write. */
u8 n64c_cpak_data_crc(const u8 *bytes32);
/* The 5-bit CRC in the low bits of the two address bytes of a read or write packet. */
u8 n64c_cpak_address_crc(u16 block_address);
#ifdef __cplusplus
}
#endif
#endif
