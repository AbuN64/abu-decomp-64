/* SPDX-License-Identifier: GPL-3.0-or-later
 * Per-command unit tests: hand-built command lists and sample memory, expectations computed
 * here from the public ABI description (integer arithmetic stated per case), not from the core. */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "n64a_abi.h"

enum { RAM = 1 << 20, LIST = 0x1000, SRC = 0x2000, DST = 0x3000, STATE = 0x4000, BASE = 0x5c0 };
enum { OP_CLEAR = 2, OP_ADPCM = 1, OP_ENVMIX = 3, OP_LOAD = 4, OP_RESAMPLE = 5, OP_SAVE = 6, OP_SEGMENT = 7,
       OP_SETBUF = 8, OP_SETVOL = 9, OP_MOVE = 10, OP_LOADADPCM = 11, OP_MIX = 12, OP_INTERLEAVE = 13 };
enum { F_INIT = 1, F_LEFT = 2, F_VOL = 4, F_AUX = 8 };

static int failures;
#define CHECK(c) do { if (!(c)) { fprintf(stderr, "%s:%d: FAIL %s\n", __FILE__, __LINE__, #c); failures++; } } while (0)

static uint8_t *ram;
static uint32_t nwords;
static void put_u8(uint32_t a, uint8_t v) { ram[a ^ 3u] = v; }
static void put_s16(uint32_t a, int16_t v) { memcpy(ram + (a ^ 2u), &v, 2); }
static void put_u32(uint32_t a, uint32_t v) { for (int i = 0; i < 4; i++) put_u8(a + (uint32_t)i, (uint8_t)(v >> (24 - 8 * i))); }
static int16_t get_s16(uint32_t a) { return n64a_ram_s16(ram, a); }
static void cmd(unsigned op, unsigned flags, unsigned w1lo, uint32_t w2)
{
    put_u32(LIST + 8 * nwords, (op << 24) | (flags << 16) | (w1lo & 0xffff));
    put_u32(LIST + 8 * nwords + 4, w2);
    nwords++;
}
static void begin(void) { memset(ram, 0, RAM); nwords = 0; }
static N64aAbi *run(void)
{
    N64aAbi *a = n64a_abi_create(N64A_FLAVOUR_AUDIO);
    CHECK(a && n64a_abi_run(a, ram, LIST, 8 * nwords) == 0);
    return a;
}
static void setbuff(unsigned in, unsigned out, unsigned count) { cmd(OP_SETBUF, 0, in, ((uint32_t)out << 16) | count); }

static void test_copy_clear(void)
{
    begin();
    for (int i = 0; i < 64; i++) put_s16(SRC + 2u * i, (int16_t)(i * 100 - 3000));
    setbuff(0, 0x200, 128);
    cmd(OP_LOAD, 0, 0, SRC);
    cmd(OP_MOVE, 0, 0, (0x200u << 16) | 128);
    cmd(OP_SAVE, 0, 0, DST);
    cmd(OP_CLEAR, 0, 0x200, 128);
    setbuff(0, 0x200, 128);
    cmd(OP_SAVE, 0, 0, DST + 0x100);
    N64aAbi *a = run();
    for (int i = 0; i < 64; i++) CHECK(get_s16(DST + 2u * i) == (int16_t)(i * 100 - 3000));
    for (int i = 0; i < 64; i++) CHECK(get_s16(DST + 0x100 + 2u * i) == 0);
    n64a_abi_destroy(a);
}

static void test_mix_interleave(void)
{
    /* MIXER: out += (in * gain) >> 15, saturating; gain 0x4000 halves, 0x7fff keeps */
    begin();
    for (int i = 0; i < 32; i++) { put_s16(SRC + 2u * i, (int16_t)(i * 1000 - 8000)); put_s16(SRC + 0x100 + 2u * i, 30000); }
    setbuff(0, 0x100, 64);
    cmd(OP_LOAD, 0, 0, SRC);
    cmd(OP_MOVE, 0, 0, (0x100u << 16) | 64);   /* out buffer = ramp */
    cmd(OP_MOVE, 0, 0x180, (0x180u << 16) | 64); /* no-op staging */
    setbuff(0x200, 0x100, 64);
    cmd(OP_LOAD, 0, 0, SRC + 0x100);           /* buffer 0x200 = 30000 */
    cmd(OP_MIX, 0, 0x4000, (0x200u << 16) | 0x100);
    setbuff(0x100, 0x100, 64);
    cmd(OP_SAVE, 0, 0, DST);
    N64aAbi *a = run();
    for (int i = 0; i < 32; i++) {
        int v = (i * 1000 - 8000) + ((30000 * 0x4000) >> 15);
        if (v > 32767) v = 32767;
        CHECK(get_s16(DST + 2u * i) == v);
    }
    n64a_abi_destroy(a);
    /* INTERLEAVE: out[2i] = left[i], out[2i+1] = right[i] */
    begin();
    for (int i = 0; i < 16; i++) { put_s16(SRC + 2u * i, (int16_t)(100 + i)); put_s16(SRC + 0x40 + 2u * i, (int16_t)(-200 - i)); }
    setbuff(0x000, 0x400, 32);
    cmd(OP_LOAD, 0, 0, SRC);
    setbuff(0x100, 0x400, 32);
    cmd(OP_LOAD, 0, 0, SRC + 0x40);
    setbuff(0x000, 0x400, 32);
    cmd(OP_INTERLEAVE, 0, 0, (0x000u << 16) | 0x100);
    cmd(OP_SAVE, 0, 0, DST); /* saves 'out' region, count 32 bytes*/
    a = run();
    /* count is 32 bytes: 16 samples = 8 frames */
    for (int i = 0; i < 8; i++) {
        CHECK(get_s16(DST + 4u * i) == (int16_t)(100 + i));
        CHECK(get_s16(DST + 4u * i + 2) == (int16_t)(-200 - i));
    }
    n64a_abi_destroy(a);
}

static void test_segment(void)
{
    begin();
    for (int i = 0; i < 16; i++) put_s16(SRC + 0x40 + 2u * i, (int16_t)(7 * i + 1));
    cmd(OP_SEGMENT, 0, 0, (3u << 24) | SRC);   /* segment 3 -> SRC */
    setbuff(0, 0x200, 32);
    cmd(OP_LOAD, 0, 0, (3u << 24) | 0x40);
    cmd(OP_MOVE, 0, 0, (0x200u << 16) | 32);
    cmd(OP_SAVE, 0, 0, DST);
    N64aAbi *a = run();
    CHECK(n64a_abi_warnings(a) == 0);
    for (int i = 0; i < 16; i++) CHECK(get_s16(DST + 2u * i) == (int16_t)(7 * i + 1));
    n64a_abi_destroy(a);
}

static void test_adpcm(void)
{
    /* zero codebook: sample = signed nibble << scale. Output buffer starts with the 16 state samples. */
    begin();
    put_u8(SRC, 0x20); /* scale 2, predictor 0 */
    for (int i = 0; i < 8; i++) put_u8(SRC + 1 + (uint32_t)i, (uint8_t)(((i & 7) << 4) | ((15 - i) & 15)));
    for (int i = 0; i < 16; i++) put_s16(STATE + 2u * i, (int16_t)(500 + i));
    cmd(OP_LOADADPCM, 0, 32, 0x7000); /* 32 bytes of zero codebook (2 entries x 8 x s16 of zero is enough for pred 0) */
    setbuff(0x000, 0x200, 32);
    cmd(OP_LOAD, 0, 0, SRC);
    setbuff(0x000, 0x200, 32);
    cmd(OP_ADPCM, 0, 0, STATE);                /* not init: state from STATE */
    setbuff(0x200, 0x200, 64);
    cmd(OP_SAVE, 0, 0, DST);
    N64aAbi *a = run();
    for (int i = 0; i < 16; i++) CHECK(get_s16(DST + 2u * i) == (int16_t)(500 + i));
    for (int i = 0; i < 8; i++) {
        int hi = i & 7, lo = (15 - i) & 15;
        int hs = (hi & 8 ? hi - 16 : hi) * 4, ls = (lo & 8 ? lo - 16 : lo) * 4;
        CHECK(get_s16(DST + 32 + 4u * i) == hs);
        CHECK(get_s16(DST + 32 + 4u * i + 2) == ls);
    }
    n64a_abi_destroy(a);
}

static void test_resample(void)
{
    begin();
    for (int i = 0; i < 128; i++) put_s16(SRC + 2u * i, 1000);
    setbuff(0x000, 0x400, 128);
    cmd(OP_LOAD, 0, 0, SRC);
    setbuff(0x000, 0x400, 128);
    cmd(OP_RESAMPLE, F_INIT, 0x8000, STATE);   /* ratio 1.0 */
    setbuff(0x400, 0x400, 128);
    cmd(OP_SAVE, 0, 0, DST);
    N64aAbi *a = run();
    int steady = 0;
    for (int i = 16; i < 64; i++) if (get_s16(DST + 2u * i) > 900 && get_s16(DST + 2u * i) < 1100) steady++;
    CHECK(steady >= 44);
    n64a_abi_destroy(a);

}

/* Envelope mixer expectations were recorded from the oracle's own output for these exact inputs. */
static void envmix_run(const int16_t *state, int n, int16_t in, bool init, const int16_t *setvol /* vol l,r, tgt l, tgt r, dry, wet */, uint32_t rate_l, uint32_t rate_r)
{
    enum { OUTS = 0x8000 };
    begin();
    for (int i = 0; i < 40; i++) put_s16(STATE + 2u * (uint32_t)i, state ? state[i] : 0);
    for (int i = 0; i < n; i++) put_s16(SRC + 2u * (uint32_t)i, in);
    cmd(OP_SEGMENT, 0, 0, 0);
    const unsigned bufs[4] = {0x440, 0x580, 0x6c0, 0x800};
    for (int b = 0; b < 4; b++) cmd(OP_CLEAR, 0, bufs[b], (uint32_t)n * 2);
    setbuff(0, 0, (unsigned)n * 2);
    cmd(OP_LOAD, 0, 0, SRC);
    setbuff(0, 0x440, (unsigned)n * 2);
    cmd(OP_SETBUF, F_AUX, 0x580, (0x6c0u << 16) | 0x800);
    if (init) {
        cmd(OP_SETVOL, F_LEFT | F_VOL, (unsigned)setvol[0], 0);
        cmd(OP_SETVOL, F_VOL, (unsigned)setvol[1], 0);
        cmd(OP_SETVOL, F_LEFT, (unsigned)setvol[2], rate_l);
        cmd(OP_SETVOL, 0, (unsigned)setvol[3], rate_r);
        cmd(OP_SETVOL, F_AUX, (unsigned)setvol[4], (uint32_t)(uint16_t)setvol[5]);
    }
    cmd(OP_ENVMIX, F_AUX | (init ? F_INIT : 0), 0, STATE);
    for (int b = 0; b < 4; b++) {
        setbuff(0, bufs[b], (unsigned)n * 2);
        cmd(OP_SAVE, 0, 0, OUTS + 0x1000u * (uint32_t)b);
    }
    N64aAbi *a = run();
    CHECK(n64a_abi_warnings(a) == 0);
    n64a_abi_destroy(a);
}
static int16_t outv(int buf, int i) { return get_s16(0x8000u + 0x1000u * (uint32_t)buf + 2u * (uint32_t)i); }

static void test_envmix(void)
{
    int16_t st[40] = {0};
    for (int k = 0; k < 8; k++) { st[k] = 0x0306; st[16 + k] = 0x0323; }
    st[32] = 0x0306; st[33] = 1; st[35] = 0x0323; st[36] = 1; st[38] = 0x70a6; st[39] = 0x3cc6;
    envmix_run(st, 16, 16000, false, NULL, 0, 0); /* steady gains: vol 774/803, dry 28838, wet 15558 */
    CHECK(outv(0, 0) == 333 && outv(1, 15) == 345 && outv(2, 3) == 179 && outv(3, 9) == 186);
    int16_t s2[40];
    memcpy(s2, st, sizeof s2);
    s2[32] = 0x0400; s2[34] = 0x1000; s2[38] = s2[39] = 0x7fff; /* target 1024, rate 1.0625 */
    envmix_run(s2, 64, 32767, false, NULL, 0, 0);
    const int expect[8] = {822, 873, 928, 986, 1024, 1024, 1024, 1024};
    for (int g = 0; g < 8; g++) for (int k = 0; k < 8; k++) CHECK(outv(0, g * 8 + k) == expect[g]);
    CHECK(get_s16(STATE) == 1024 && get_s16(STATE + 16) == 4620); /* lane 0 high/low words after 8 groups */
    /* rate below 1.0 ramps down; a target above the volume is reached at once */
    s2[32] = 0x0100; s2[33] = 0; s2[34] = (int16_t)0xE666;
    for (int k = 0; k < 8; k++) s2[k] = 0x0600;
    envmix_run(s2, 16, 32767, false, NULL, 0, 0);
    CHECK(outv(0, 0) == 1382 && outv(0, 8) == 1244);
    /* init: lanes ramp linearly toward vol*rate across the first group */
    const int16_t sv[6] = {1000, 1000, 4000, 100, 0x7fff, 0x7fff};
    envmix_run(NULL, 16, 32767, true, sv, 0x20000, 0x8000);
    for (int k = 0; k < 8; k++) {
        CHECK(abs(outv(0, k) - (1000 + 125 * (k + 1))) <= 1);
        CHECK(abs(outv(1, k) - (1000 - (1000 * (k + 1)) / 16)) <= 1);
    }
    CHECK(abs(outv(0, 8) - 2250) <= 1 && abs(outv(1, 8) - 468) <= 1);
}

static void test_args(void)
{
    N64aAbi *a = n64a_abi_create(N64A_FLAVOUR_AUDIO);
    CHECK(n64a_abi_run(a, ram, 4, 8) == -1);
    CHECK(n64a_abi_run(a, ram, 8, 4) == -1);
    CHECK(n64a_abi_run(a, NULL, 8, 8) == -1);
    n64a_abi_destroy(a);
}

int main(void)
{
    ram = calloc(1, RAM);
    if (!ram) return 1;
    test_copy_clear();
    test_mix_interleave();
    test_segment();
    test_adpcm();
    test_resample();
    test_envmix();
    test_args();
    free(ram);
    printf(failures ? "n64a_abi: %d failure(s)\n" : "n64a_abi: ok\n", failures);
    return failures != 0;
}
