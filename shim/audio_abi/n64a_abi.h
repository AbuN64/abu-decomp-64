/* SPDX-License-Identifier: GPL-3.0-or-later
 * Thin adapter over the vendored mupen64plus-rsp-hle alist interpreter (see vendor/ and
 * SOURCES.md). It supplies the envelope mixer and builds the task frame the vendored core reads,
 * runs a command list, and forwards PCM to a sink. Guest RAM uses the 32-bit word-swapped
 * byte layout (byte i at host offset i ^ 3), the layout recompiled N64 hosts use. */
#ifndef N64A_ABI_H
#define N64A_ABI_H
#include <stddef.h>
#include <stdint.h>

typedef enum {
    N64A_FLAVOUR_AUDIO,    /* standard libaudio ABI */
    N64A_FLAVOUR_AUDIO_GE, /* standard ABI, GoldenEye envmixer variant */
    N64A_FLAVOUR_AUDIO_BC, /* standard ABI, Banjo-Kazooie envmixer variant */
    N64A_FLAVOUR_NAUDIO,   /* n_audio ABI */
} N64aFlavour;

typedef struct N64aAbi N64aAbi;

/* PCM sink: interleaved native-endian 16-bit stereo frames. */
typedef void (*N64aPcmSink)(void *user, const int16_t *frames, size_t frame_count, uint32_t rate);

N64aAbi *n64a_abi_create(N64aFlavour flavour);
void n64a_abi_destroy(N64aAbi *abi);
/* Runs list_bytes of 8-byte commands at guest address list_addr. Returns 0, or -1 on bad arguments.
 * Guest RAM size must be a power of two no larger than 16 MiB; addresses wrap at 24 bits. */
int n64a_abi_run(N64aAbi *abi, uint8_t *guest_ram, uint32_t list_addr, uint32_t list_bytes);
/* Pending-warning count since creation (invalid opcode/segment reports from the core). */
unsigned n64a_abi_warnings(const N64aAbi *abi);

/* Diagnostics only: the 4 KiB working memory the core mixes in (same addressing as the RSP DMEM image). */
const uint8_t *n64a_abi_scratch(const N64aAbi *abi);

/* AI side: converts a guest stereo S16 big-endian buffer into the sink. */
void n64a_ai_submit(const uint8_t *guest_ram, uint32_t addr, uint32_t bytes, uint32_t rate, N64aPcmSink sink, void *user);
int16_t n64a_ram_s16(const uint8_t *guest_ram, uint32_t addr);
#endif
