/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "n64a_abi.h"

#include <stdarg.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

#include "alist.h"
#include "hle_external.h"
#include "hle_internal.h"


struct N64aAbi {
    struct hle_t hle;
    unsigned char dmem[0x1000];
    unsigned char imem[0x1000];
    N64aFlavour flavour;
    unsigned warnings;
    /* command-list state (the audio ABI keeps these between commands) */
    uint32_t segments[16];
    uint16_t in, out, count, dry_right, wet_left, wet_right;
    int16_t dry, wet, vol[2], target[2];
    int32_t rate[2];
    uint32_t loop;
    int16_t table[16 * 8];
};

enum { DMEM_BASE = 0x5c0 };
enum { F_INIT = 1, F_LOOP = 2, F_LEFT = 2, F_VOL = 4, F_AUX = 8 };

/* Hooks the vendored core expects its embedder to supply. */
void HleVerboseMessage(void *user, const char *fmt, ...) { (void)user; (void)fmt; }
void HleInfoMessage(void *user, const char *fmt, ...) { (void)user; (void)fmt; }
void HleErrorMessage(void *user, const char *fmt, ...) { ((N64aAbi *)user)->warnings++; (void)fmt; }
void HleWarnMessage(void *user, const char *fmt, ...) { ((N64aAbi *)user)->warnings++; (void)fmt; }
void HleCheckInterrupts(void *user) { (void)user; }
void HleProcessDlistList(void *user) { (void)user; }
void HleProcessAlistList(void *user) { (void)user; }
void HleProcessRdpList(void *user) { (void)user; }
void HleShowCFB(void *user) { (void)user; }
int HleForwardTask(void *user) { (void)user; return 0; }
/* The naudio MP3 command is outside libaudio use here; the shim does not decode it. */
void mp3_task(struct hle_t *hle, unsigned int index, uint32_t address) { (void)hle; (void)index; (void)address; }
void rsp_break(struct hle_t *hle, unsigned int setbits) { (void)hle; (void)setbits; }

N64aAbi *n64a_abi_create(N64aFlavour flavour)
{
    N64aAbi *abi = calloc(1, sizeof *abi);
    if (!abi) return NULL;
    abi->flavour = flavour;
    abi->hle.dmem = abi->dmem;
    abi->hle.imem = abi->imem;
    abi->hle.user_defined = abi;
    return abi;
}

void n64a_abi_destroy(N64aAbi *abi) { free(abi); }
const uint8_t *n64a_abi_scratch(const N64aAbi *abi) { return abi->hle.alist_buffer; }
unsigned n64a_abi_warnings(const N64aAbi *abi) { return abi->warnings; }

static uint32_t seg_addr(N64aAbi *abi, uint32_t so)
{
    return alist_get_address(&abi->hle, so, abi->segments, 16);
}

static void ram_put_s16(uint8_t *ram, uint32_t addr, int16_t v) { memcpy(ram + ((addr & 0xffffffu) ^ 2u), &v, sizeof v); }

static int16_t sat16(int32_t v) { return (int16_t)(v > 32767 ? 32767 : v < -32768 ? -32768 : v); }

/* Signed Q15 multiply with rounding: (a * b * 2 + 0x8000) >> 16, saturating. */
static int16_t mulq15(int16_t a, int16_t b) { return sat16((int32_t)(((int64_t)a * b * 2 + 0x8000) >> 16)); }

static int16_t *dmem_s16(N64aAbi *abi, uint16_t addr) { return (int16_t *)(abi->hle.alist_buffer + ((addr ^ 2u) & 0xfffu)); }

/* Envelope mixer, derived black-box from the oracle (see PROVENANCE.md). The 40-halfword state at
 * `state` holds, per channel, eight 16.16 volume lanes (high words, then low words), a target and a
 * 16.16 rate; then the dry and wet gains. Each 8-sample group first scales every lane by the rate,
 * clamps it to the target (ceiling when rate >= 1.0, floor otherwise), then applies lane k to
 * sample k. With the init flag the lanes start as vol + (k+1)/8 of the way to vol*rate and the
 * first group is not scaled again. */
static void envmix(N64aAbi *abi, uint8_t *ram, unsigned flags, uint32_t state)
{
    const bool init = flags & F_INIT, aux = flags & F_AUX;
    int32_t lane[2][8];
    int16_t tgt[2], dry, wet;
    int32_t rate[2];
    if (init) {
        for (int c = 0; c < 2; c++) {
            tgt[c] = abi->target[c];
            rate[c] = abi->rate[c];
            const int64_t base = (int64_t)abi->vol[c] << 16;
            const int64_t step = (int64_t)abi->vol[c] * ((int64_t)rate[c] - 0x10000) / 8;
            for (int k = 0; k < 8; k++) lane[c][k] = (int32_t)(base + step * (k + 1));
        }
        dry = abi->dry;
        wet = abi->wet;
    } else {
        for (int c = 0; c < 2; c++) {
            for (int k = 0; k < 8; k++)
                lane[c][k] = (int32_t)(((uint32_t)(uint16_t)n64a_ram_s16(ram, state + 2u * (uint32_t)(c * 16 + k)) << 16) |
                                       (uint16_t)n64a_ram_s16(ram, state + 2u * (uint32_t)(c * 16 + 8 + k)));
            tgt[c] = n64a_ram_s16(ram, state + 64u + 6u * (uint32_t)c);
            rate[c] = (int32_t)(((uint32_t)(uint16_t)n64a_ram_s16(ram, state + 66u + 6u * (uint32_t)c) << 16) |
                                (uint16_t)n64a_ram_s16(ram, state + 68u + 6u * (uint32_t)c));
        }
        dry = n64a_ram_s16(ram, state + 76u);
        wet = n64a_ram_s16(ram, state + 78u);
    }
    const uint16_t bufs[4] = {abi->out, abi->dry_right, abi->wet_left, abi->wet_right};
    const unsigned groups = (abi->count + 15u) / 16u;
    for (unsigned g = 0; g < groups; g++) {
        for (int c = 0; c < 2; c++) {
            if (init && g == 0) continue;
            for (int k = 0; k < 8; k++) {
                int32_t v = (int32_t)(((int64_t)lane[c][k] * rate[c]) >> 16);
                int16_t hi = (int16_t)(v >> 16);
                hi = rate[c] >= 0x10000 ? (hi > tgt[c] ? tgt[c] : hi) : (hi < tgt[c] ? tgt[c] : hi);
                lane[c][k] = (int32_t)(((uint32_t)(uint16_t)hi << 16) | ((uint32_t)v & 0xffffu));
            }
        }
        for (int k = 0; k < 8; k++) {
            const uint16_t off = (uint16_t)(2u * (g * 8u + (unsigned)k));
            const int16_t x = *dmem_s16(abi, (uint16_t)(abi->in + off));
            const int16_t l = mulq15(x, (int16_t)(lane[0][k] >> 16)), r = mulq15(x, (int16_t)(lane[1][k] >> 16));
            const int16_t outs[4] = {mulq15(l, dry), mulq15(r, dry), mulq15(l, wet), mulq15(r, wet)};
            for (int b = 0; b < (aux ? 4 : 2); b++) {
                int16_t *d = dmem_s16(abi, (uint16_t)(bufs[b] + off));
                *d = sat16(*d + outs[b]);
            }
        }
    }
    for (int c = 0; c < 2; c++) {
        for (int k = 0; k < 8; k++) {
            ram_put_s16(ram, state + 2u * (uint32_t)(c * 16 + k), (int16_t)(lane[c][k] >> 16));
            ram_put_s16(ram, state + 2u * (uint32_t)(c * 16 + 8 + k), (int16_t)(lane[c][k] & 0xffff));
        }
        ram_put_s16(ram, state + 64u + 6u * (uint32_t)c, tgt[c]);
        ram_put_s16(ram, state + 66u + 6u * (uint32_t)c, (int16_t)(rate[c] >> 16));
        ram_put_s16(ram, state + 68u + 6u * (uint32_t)c, (int16_t)(rate[c] & 0xffff));
    }
    ram_put_s16(ram, state + 76u, dry);
    ram_put_s16(ram, state + 78u, wet);
}

static void command(N64aAbi *abi, uint8_t *ram, uint32_t w1, uint32_t w2)
{
    struct hle_t *hle = &abi->hle;
    const unsigned flags = (w1 >> 16) & 0xff;
    switch ((w1 >> 24) & 0x7f) {
    case 0: break; /* no-op */
    case 1: /* ADPCM */
        if (abi->flavour == N64A_FLAVOUR_NAUDIO) { abi->warnings++; break; }
        alist_adpcm(hle, flags & F_INIT, flags & F_LOOP, false, abi->out, abi->in, (uint16_t)((abi->count + 31u) & ~31u),
                    abi->table, abi->loop, seg_addr(abi, w2));
        break;
    case 2: { /* clear buffer */
        const uint16_t n = w2 & 0xfff;
        if (n) alist_clear(hle, (uint16_t)(w1 + DMEM_BASE), (uint16_t)((n + 15u) & ~15u));
        break;
    }
    case 3: envmix(abi, ram, flags, seg_addr(abi, w2)); break;
    case 4: if (abi->count) alist_load(hle, abi->in, seg_addr(abi, w2), abi->count); break;
    case 5: /* resample */
        alist_resample(hle, flags & F_INIT, flags & 2, abi->out, abi->in, (uint16_t)((abi->count + 15u) & ~15u),
                       (w1 & 0xffffu) << 1, seg_addr(abi, w2));
        break;
    case 6: if (abi->count) alist_save(hle, abi->out, seg_addr(abi, w2), abi->count); break;
    case 7: alist_set_address(hle, w2, abi->segments, 16); break;
    case 8:
        if (flags & F_AUX) {
            abi->dry_right = (uint16_t)(w1 + DMEM_BASE);
            abi->wet_left = (uint16_t)((w2 >> 16) + DMEM_BASE);
            abi->wet_right = (uint16_t)(w2 + DMEM_BASE);
        } else {
            abi->in = (uint16_t)(w1 + DMEM_BASE);
            abi->out = (uint16_t)((w2 >> 16) + DMEM_BASE);
            abi->count = (uint16_t)w2;
        }
        break;
    case 9: /* set volume / envelope parameters */
        if (flags & F_AUX) {
            abi->dry = (int16_t)w1;
            abi->wet = (int16_t)w2;
        } else {
            const unsigned lr = (flags & F_LEFT) ? 0 : 1;
            if (flags & F_VOL) abi->vol[lr] = (int16_t)w1;
            else { abi->target[lr] = (int16_t)w1; abi->rate[lr] = (int32_t)w2; }
        }
        break;
    case 10: { /* move */
        const uint16_t n = (uint16_t)w2;
        if (n) alist_move(hle, (uint16_t)((w2 >> 16) + DMEM_BASE), (uint16_t)(w1 + DMEM_BASE), (uint16_t)((n + 15u) & ~15u));
        break;
    }
    case 11: { /* load ADPCM codebook / filter coefficients */
        const uint16_t n = (uint16_t)w1;
        if (n <= sizeof abi->table) { /* 16 predictors x 8 halfwords, 8-byte aligned count */
            const uint32_t a = seg_addr(abi, w2);
            for (unsigned i = 0; i < (((unsigned)n + 7u) & ~7u) / 2; i++) abi->table[i] = n64a_ram_s16(ram, a + 2u * i);
        } else abi->warnings++;
        break;
    }
    case 12: if (abi->count) alist_mix(hle, (uint16_t)(w2 + DMEM_BASE), (uint16_t)((w2 >> 16) + DMEM_BASE), (uint16_t)((abi->count + 31u) & ~31u), (int16_t)w1); break;
    case 13: if (abi->count) alist_interleave(hle, abi->out, (uint16_t)((w2 >> 16) + DMEM_BASE), (uint16_t)(w2 + DMEM_BASE), (uint16_t)((abi->count + 15u) & ~15u)); break;
    case 14: if (abi->count) alist_polef(hle, flags & F_INIT, abi->out, abi->in, (uint16_t)((abi->count + 15u) & ~15u), (uint16_t)w1, abi->table, seg_addr(abi, w2)); break;
    case 15: abi->loop = seg_addr(abi, w2); break;
    case 16: { /* load raw bytes: RAM bytes in transport order (cartridge sample data) into the working memory */
        const uint32_t a = seg_addr(abi, w2) & 0xffffffu;
        for (unsigned i = 0; i < abi->count; i++) hle->alist_buffer[((abi->in + i) ^ 3u) & 0xfffu] = ram[a + i];
        break;
    }
    default: abi->warnings++; break;
    }
}

int n64a_abi_run(N64aAbi *abi, uint8_t *guest_ram, uint32_t list_addr, uint32_t list_bytes)
{
    if (!abi || !guest_ram || (list_addr & 7u) || (list_bytes & 7u)) return -1;
    abi->hle.dram = guest_ram;
    memset(abi->segments, 0, sizeof abi->segments);
    for (uint32_t off = 0; off < list_bytes; off += 8) {
        const uint32_t a = (list_addr + off) & 0xffffffu;
        uint32_t w[2];
        for (int i = 0; i < 2; i++) {
            w[i] = 0;
            for (uint32_t b = 0; b < 4; b++) w[i] = (w[i] << 8) | guest_ram[(a + 4u * (uint32_t)i + b) ^ 3u];
        }
        command(abi, guest_ram, w[0], w[1]);
    }
    return 0;
}

int16_t n64a_ram_s16(const uint8_t *guest_ram, uint32_t addr)
{
    int16_t v;
    memcpy(&v, guest_ram + ((addr & 0xffffffu) ^ 2u), sizeof v);
    return v;
}

void n64a_ai_submit(const uint8_t *guest_ram, uint32_t addr, uint32_t bytes, uint32_t rate, N64aPcmSink sink, void *user)
{
    enum { CHUNK = 256 };
    int16_t buf[CHUNK * 2];
    size_t frames = bytes / 4;
    for (size_t done = 0; done < frames;) {
        size_t n = frames - done < CHUNK ? frames - done : CHUNK;
        for (size_t i = 0; i < n * 2; i++) buf[i] = n64a_ram_s16(guest_ram, addr + (uint32_t)((done * 2 + i) * 2));
        sink(user, buf, n, rate);
        done += n;
    }
}
