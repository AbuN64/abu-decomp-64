/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_DECOMP_BOOT_H
#define N64C_DECOMP_BOOT_H
#include "n64c_os.h"
#include "n64c_pi.h"
#include "n64c_system.h"
typedef struct {
    const char *symbol;
    u32 rom, ram, bytes;
    N64cResKind kind;
    const char *fields;
} N64cBootLoad;
extern const N64cBootLoad n64c_boot_loads[];
/* Original resident data built from the source's own data units (image.py):
 * bytes at an image offset are copied to physical RDRAM before typed loads. */
typedef struct {
    u32 ram, offset, bytes;
} N64cBootImage;
extern const N64cBootImage n64c_boot_image[];
extern const size_t n64c_boot_image_count;
extern const unsigned char n64c_boot_image_bytes[];
/* Overlay data from the same units, keyed by ROM offset: the PI layer writes these image bytes over every DMA of the
 * range (n64c_pi_register_image), wherever the game puts the overlay. */
typedef struct {
    u32 rom, offset, bytes;
} N64cBootRomImage;
extern const N64cBootRomImage n64c_boot_rom_image[];
extern const size_t n64c_boot_rom_image_count;
extern const size_t n64c_boot_load_count;
/* ROM ranges the game DMAs later: PI byte-order descriptors only (ram is 0, nothing is copied). */
extern const N64cBootLoad n64c_boot_resources[];
extern const size_t n64c_boot_resource_count;
/* RNC streams the decomp's blob facts name (blobs.toml codec rnc1/rnc2): the PI layer serves their headers in host
 * order and types their unpacked bytes by kind/fields (n64c_pi_register_packed; RAW when the manifest gives none). */
extern const N64cBootLoad n64c_boot_packed[];
extern const size_t n64c_boot_packed_count;
extern const u32 n64c_boot_ram_bytes, n64c_boot_video_clock;
extern const u32 n64c_boot_rom_bytes;
extern const char n64c_boot_rom_sha256[];
extern const N64cSystemGlobals n64c_boot_system;
extern const u32 n64c_boot_tv_type, n64c_boot_reset_type;
extern const int n64c_boot_audio_abi; /* N64aFlavour, or -1: audio tasks are refused */
int n64c_boot_entry(void);
#endif
