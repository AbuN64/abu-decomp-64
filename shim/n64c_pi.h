/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_PI_H
#define N64C_PI_H
#include "n64c_os.h"
#ifdef __cplusplus
extern "C" {
#endif
typedef enum { N64C_RES_RAW, N64C_RES_SWAP16, N64C_RES_SWAP32, N64C_RES_STRUCT, N64C_RES_TAGGED, N64C_RES_ALCTL,
               N64C_RES_IMAGE, N64C_RES_GRAPH, N64C_RES_PACKED, N64C_RES_UNTYPED } N64cResKind;
/* Resolve an original pointer to its native32-bit value. The binding owner
 * supplies function/data identities; PI never guesses from address bit patterns.
 * A zero pointer stays zero without calling the resolver. */
typedef int (*N64cRelocateFn)(u32 original,u32 *native,void *user);
void n64c_pi_set_relocator(N64cRelocateFn resolve,void *user);
/* Reserve space for count ROM descriptors (0 success, -1 allocation/size failure), preserving registrations.
 * The boot passes the generated load + resource + packed + overlay-image count; close releases the allocation.
 * Individual registrations reserve their next slot when no batch count was supplied. No fixed descriptor cap. */
int n64c_pi_reserve(size_t count);
/* TAGGED fields spec: "offset/width;tag=fields;tag=fields" in decimal (e.g. "14/2;1=4 4 4 2 2;2=..."): back-to-back
 * records of different sizes; the big-endian tag of width 1, 2 or 4 bytes at offset selects the record's field string
 * (its widths sum to the record size). A DMA subrange must start and end inside whole fields of whole records.
 * Register the ROM range [lo, hi) as one resource type. fields is used by STRUCT: widths in bytes, e.g. "222 2 22 1111"; p is a relocated32-bit pointer.
 * ALCTL (fields NULL): an audio control bank container the game maps and reads in place; at registration the range is
 * read from the ROM and its record graph walked (n64c_al_ctl_widths) into a per-byte width map that every DMA of a
 * subrange applies. Registration needs the ROM open and fails on a malformed container.
 * Resources must be disjoint whole records. DMA subranges must contain whole
 * multibyte fields. Failed reads have no completion event; discard their buffer. */
int n64c_pi_register(u32 lo, u32 hi, N64cResKind kind, const char *fields);
/* UNTYPED (n64c_pi_register, or n64c_pi_register_packed for an RNC stream): fields is the name of a decomp blob fact
 * that no layout types (the manifest byte-order gate, link.py): the first DMA that starts in the range stops the boot
 * with exit 78 and names the blob; with N64C_LAYOUT_GATE=log it is logged once (boot-diag: untyped blob NAME ROM
 * 0x...) and served raw, so one diagnostic boot lists every untyped blob it reads. */
/* IMAGE: the ROM range [lo, hi) holds data the source types (overlay data units compiled natively, image.py); every
 * DMA of a subrange receives the matching bytes of image (hi-lo bytes, kept by the caller) in place of the ROM's,
 * so a field cut by the DMA ends is still whole once both parts arrive. Same disjointness rule as n64c_pi_register. */
int n64c_pi_register_image(u32 lo, u32 hi, const u8 *image);
/* PACKED: the ROM range [lo, hi) is one RNC stream (method 1 or 2: the 18-byte header, then the packed bytes). Every
 * DMA of it receives the header in host order (u32 magic, unpacked and packed sizes, u16 CRCs, leeway and chunk
 * bytes; a field cut by the DMA ends is whole once both parts arrive) and the packed bytes raw. kind and fields type
 * the unpacked bytes, which n64c_pi_rnc1_unpack/n64c_pi_rnc2_unpack convert after unpacking a stream they trace back
 * to this range: RAW (fields NULL) leaves them, SWAP16/SWAP32/STRUCT/TAGGED as above, GRAPH is a typed record graph
 * (fields is its program, below). Registration reads the header from the ROM and fails on a range that is not one
 * whole RNC stream or a malformed program.
 * GRAPH program: types separated by ';', type 0 the root, each one of
 *   F:<items>               a record: items separated by spaces, a width 1-8 or a reference @<s>:<t> (a u32 offset
 *                           from the start of section s of the enclosing directory to a record of type t; 0 and
 *                           0xFFFFFFFF are null)
 *   T:<offset>/<width>:<tag>=<t>,...  a record whose big-endian tag picks its F variant (its size: the largest)
 *   R:<head>,<element>      F record head, then F element records up to the end of the enclosing extent
 *   A:<head>,<element>:<count>  a counted array: F head followed by count F/T elements; reference section 0 is
 *                           the entire array, starting at its head (other enclosing bases are not visible)
 *   C:<head>,<block>:<count>:<payload>  F head then count sized blocks, each F block header followed by payload
 *                           raw bytes. Count reads the head; payload reads each block header.
 *                           Both expressions are scale/divisor:offset/width,... (1-8 unsigned scalar fields,
 *                           widths 1,2,4): floor(scale * product(fields) / divisor). Checked arithmetic and bounds;
 *                           expressions must name whole scalar fields of their header. Trailing bytes stay raw.
 *   P:<t>                   an RNC stream (method 1 or 2) nested in the bytes: header fields typed, packed bytes raw;
 *                           in a ROM range (n64c_pi_register) its unpacked bytes get type t (- for raw) when the game
 *                           unpacks it, like a PACKED range's
 *   L:<t>                   a leaf: u32 stride (the size of F or T type t; any when count is 0), u32 count, count
 *                           records of type t
 *   X:<t>                   a leaf stored as byte planes (the game transposes it into records, plane k = byte k of
 *                           every record): u32 stride (the size of F type t, no references), u32 count, then stride
 *                           planes of count bytes; each field's planes are reversed so the transpose builds host-order
 *                           records. Only in unpacked bytes (a ROM range is converted per DMA piece)
 *   D:<t>,<t>,...           a directory: u32 count, u32 section offsets from its start (the table runs to the first
 *                           section, an end offset may follow); section i has type i of the list (- for none: its
 *                           bytes are typed by references, or stay raw)
 *   E:<t>                   a directory whose sections all have type t
 *   G:<offset>/<width>:<tag>=<t>,...  a directory whose sections are records typed by their big-endian tag
 * Bytes no type reaches stay raw (padding); records may share equal byte runs, but two types giving one byte different
 * widths where the order matters fail the layout. n64c_pi_register takes GRAPH for a ROM range too (an uncompressed
 * container): the graph is walked over the ROM bytes at registration into a per-byte width map (like ALCTL). */
int n64c_pi_register_packed(u32 lo, u32 hi, N64cResKind kind, const char *fields);
/* The cartridge offset the byte at ram was last DMA'd from (1), or 0 when no DMA since wrote it. */
int n64c_pi_origin(const void *ram, u32 *dev);
/* ram now holds data no DMA wrote (unpacked output): its origin is forgotten. */
void n64c_pi_forget(const void *ram, u32 bytes);
/* The game's RNC unpack calls: unpack (n64c_rnc.h), then, when the stream traces back to a PACKED range (the origin
 * of src, which is header + 18, or of header), convert the unpacked bytes by that range's layout. A layout that does
 * not fit the unpacked bytes stops the boot (exit 78). */
void n64c_pi_rnc1_unpack(const void *src, void *dst, s32 packed_size, s32 unpacked_size);
void n64c_pi_rnc2_unpack(const void *header, void *dst, s32 packed_size, s32 unpacked_size);
#ifdef __cplusplus
}
#endif
#endif
