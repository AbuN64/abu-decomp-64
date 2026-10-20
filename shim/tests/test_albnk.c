/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Bank reader: a hand-built big-endian control bank (one file, one bank, two instruments sharing a sound,
 * an ADPCM wave with loop and book, a raw wave) read back as native structures, malformed offsets refused, and,
 * when N64C_TEST_ROM names the game ROM, the game's own control bank (ROM 0x28B264, 58028 bytes, two files)
 * walked for the record counts of an independent offset walk. */
#include "../n64c_al.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

static void w32(u8 *b, u32 o, u32 v) { b[o] = (u8)(v >> 24); b[o + 1] = (u8)(v >> 16); b[o + 2] = (u8)(v >> 8); b[o + 3] = (u8)v; }
static void w16(u8 *b, u32 o, u32 v) { b[o] = (u8)(v >> 8); b[o + 1] = (u8)v; }

static _Alignas(16) u8 heapmem[1 << 20];

static void synthetic(void)
{
    enum { FILE_ = 0x14, BANK = 0x40, INST0 = 0x60, INST1 = 0xA0, SND0 = 0xE0, SND1 = 0xF0, ENV = 0x100, KEY = 0x110,
           WAVE0 = 0x120, LOOP = 0x140, BOOK = 0x170, WAVE1 = 0x1A0, RLOOP = 0x1C0, LEN = 0x200 };
    u8 c[LEN] = {0};
    w32(c, 0, 1);
    w32(c, 4, 0x800000 | FILE_);
    w32(c, 12, 0);
    w16(c, FILE_, 0x4231);
    w16(c, FILE_ + 2, 1);
    w32(c, FILE_ + 4, 0x800000 | BANK);
    w16(c, BANK, 2);             /* two instruments */
    c[BANK + 2] = 5;
    w32(c, BANK + 4, 22050);
    w32(c, BANK + 8, 0);         /* no percussion */
    w32(c, BANK + 12, 0x800000 | INST0);
    w32(c, BANK + 16, 0x800000 | INST1);
    c[INST0] = 100; c[INST0 + 1] = 64; c[INST0 + 2] = 9; c[INST0 + 11] = 3;
    w16(c, INST0 + 12, 200); w16(c, INST0 + 14, 2);
    w32(c, INST0 + 16, SND0); w32(c, INST0 + 20, SND1);
    c[INST1 + 1] = 7; w16(c, INST1 + 12, 0xFFFE); w16(c, INST1 + 14, 1);
    w32(c, INST1 + 16, SND0);    /* shared with instrument 0 */
    w32(c, SND0, ENV); w32(c, SND0 + 4, KEY); w32(c, SND0 + 8, WAVE0);
    c[SND0 + 12] = 64; c[SND0 + 13] = 127; c[SND0 + 14] = 2;
    w32(c, SND1, ENV); w32(c, SND1 + 4, 0); w32(c, SND1 + 8, WAVE1);
    w32(c, ENV, 1000); w32(c, ENV + 4, 2000); w32(c, ENV + 8, 0x12345678);
    c[ENV + 12] = 127; c[ENV + 13] = 100;
    c[KEY] = 1; c[KEY + 1] = 127; c[KEY + 2] = 20; c[KEY + 3] = 90; c[KEY + 4] = 60; c[KEY + 5] = 0xF6;
    w32(c, WAVE0, 0x4000); w32(c, WAVE0 + 4, 9000); c[WAVE0 + 8] = AL_ADPCM_WAVE; c[WAVE0 + 9] = 1;
    w32(c, WAVE0 + 12, LOOP); w32(c, WAVE0 + 16, BOOK);
    w32(c, LOOP, 16); w32(c, LOOP + 4, 4000); w32(c, LOOP + 8, 3);
    for (int i = 0; i < 16; i++) w16(c, LOOP + 12 + 2 * i, 0xFF00u + (unsigned)i);
    w32(c, BOOK, 2); w32(c, BOOK + 4, 2);
    for (int i = 0; i < 32; i++) w16(c, BOOK + 8 + 2 * i, (unsigned)(i * 257 - 4000) & 0xFFFFu);
    w32(c, WAVE1, 0x8000); w32(c, WAVE1 + 4, 512); c[WAVE1 + 8] = AL_RAW16_WAVE;
    w32(c, WAVE1 + 12, RLOOP);
    w32(c, RLOOP, 8); w32(c, RLOOP + 4, 200); w32(c, RLOOP + 8, 0xFFFFFFFF);

    ALHeap heap;
    alHeapInit(&heap, heapmem, sizeof heapmem);
    u32 off = 0;
    CHECK(n64c_al_ctl_file(c, LEN, 0, &off) == 1 && off == FILE_);
    CHECK(n64c_al_ctl_file(c, LEN, 1, &off) == 0);
    ALBankFile *f = n64c_al_bnkf_read(&heap, c, LEN, FILE_, 0x100000);
    CHECK(f && f->revision == 0x4231 && f->bankCount == 1);
    if (!f) return;
    ALBank *b = f->bankArray[0];
    CHECK(b && b->instCount == 2 && b->flags == 5 && b->sampleRate == 22050 && !b->percussion);
    ALInstrument *i0 = b->instArray[0], *i1 = b->instArray[1];
    CHECK(i0 && i1 && i0->volume == 100 && i0->pan == 64 && i0->priority == 9 && i0->vibDelay == 3);
    CHECK(i0->bendRange == 200 && i0->soundCount == 2 && i1->bendRange == -2 && i1->pan == 7);
    CHECK(i0->soundArray[0] == i1->soundArray[0]);   /* shared record built once */
    ALSound *s0 = i0->soundArray[0], *s1 = i0->soundArray[1];
    CHECK(s0->samplePan == 64 && s0->sampleVolume == 127 && s0->flags == 2);
    CHECK(s0->envelope == s1->envelope && s0->envelope->attackTime == 1000 && s0->envelope->decayTime == 2000);
    CHECK(s0->envelope->releaseTime == 0x12345678 && s0->envelope->attackVolume == 127 && s0->envelope->decayVolume == 100);
    CHECK(s0->keyMap && s0->keyMap->keyMin == 20 && s0->keyMap->keyMax == 90 && s0->keyMap->keyBase == 60 && s0->keyMap->detune == -10);
    CHECK(!s1->keyMap);
    ALWaveTable *w0 = s0->wavetable, *w1 = s1->wavetable;
    CHECK(w0->type == AL_ADPCM_WAVE && w0->len == 9000 && w0->flags == 1 && (uintptr_t)w0->base == 0x104000);
    CHECK(w0->waveInfo.adpcm.loop && w0->waveInfo.adpcm.loop->start == 16 && w0->waveInfo.adpcm.loop->end == 4000);
    CHECK(w0->waveInfo.adpcm.loop->count == 3 && w0->waveInfo.adpcm.loop->state[0] == -256 && w0->waveInfo.adpcm.loop->state[15] == -241);
    ALADPCMBook *bk = w0->waveInfo.adpcm.book;
    CHECK(bk && bk->order == 2 && bk->npredictors == 2 && bk->book[0] == -4000 && bk->book[31] == 31 * 257 - 4000);
    CHECK(w1->type == AL_RAW16_WAVE && w1->len == 512 && (uintptr_t)w1->base == 0x108000);
    CHECK(w1->waveInfo.raw.loop && w1->waveInfo.raw.loop->end == 200 && w1->waveInfo.raw.loop->count == 0xFFFFFFFFu);

    /* malformed: an instrument offset past the end, an unaligned record, a file offset past the end, a small heap */
    u8 bad[LEN];
    memcpy(bad, c, LEN);
    w32(bad, BANK + 16, 0x800000 | (LEN + 8));
    alHeapInit(&heap, heapmem, sizeof heapmem);
    CHECK(!n64c_al_bnkf_read(&heap, bad, LEN, FILE_, 0));
    memcpy(bad, c, LEN);
    w32(bad, INST1 + 16, SND0 + 2);
    alHeapInit(&heap, heapmem, sizeof heapmem);
    CHECK(!n64c_al_bnkf_read(&heap, bad, LEN, FILE_, 0));
    alHeapInit(&heap, heapmem, sizeof heapmem);
    CHECK(!n64c_al_bnkf_read(&heap, c, LEN, LEN - 2, 0));
    alHeapInit(&heap, heapmem, 64);
    CHECK(!n64c_al_bnkf_read(&heap, c, LEN, FILE_, 0));
}

static int cmp(const void *a, const void *b)
{
    uintptr_t x = (uintptr_t)*(void *const *)a, y = (uintptr_t)*(void *const *)b;
    return x < y ? -1 : x > y;
}
/* Distinct non-NULL pointers in v[0..n). */
static size_t uniq(void **v, size_t n)
{
    qsort(v, n, sizeof *v, cmp);
    size_t k = 0;
    for (size_t i = 0; i < n; i++)
        if (v[i] && (k == 0 || v[k - 1] != v[i])) v[k++] = v[i];
    return k;
}

static void game_bank(const char *romPath)
{
    FILE *fp = fopen(romPath, "rb");
    if (!fp) { printf("skip: cannot open %s\n", romPath); return; }
    enum { BASE = 0x28B264, LEN = 0x299510 - 0x28B264 };
    u8 *c = malloc(LEN);
    if (!c || fseek(fp, BASE, SEEK_SET) || fread(c, 1, LEN, fp) != LEN) { printf("skip: short ROM\n"); fclose(fp); free(c); return; }
    fclose(fp);
    static _Alignas(16) u8 big[4 << 20];
    ALHeap heap;
    alHeapInit(&heap, big, sizeof big);
    static void *insts[2048], *snds[2048], *envs[2048], *keys[2048], *waves[2048], *loops[2048], *books[2048];
    size_t ni = 0, ns = 0, ne = 0, nk = 0, nw = 0, nl = 0, nb = 0;
    CHECK(n64c_al_ctl_file(c, LEN, 0, NULL) == 2);
    for (s32 n = 0; n < 2; n++) {
        u32 off = 0;
        CHECK(n64c_al_ctl_file(c, LEN, n, &off) == 2);
        ALBankFile *f = n64c_al_bnkf_read(&heap, c, LEN, off, 0);
        CHECK(f && f->revision == 0x4231);
        if (!f) continue;
        for (s32 i = 0; i < f->bankCount; i++) {
            ALBank *b = f->bankArray[i];
            CHECK(b != NULL);
            if (!b) continue;
            ALInstrument *list[512];
            s32 cnt = 0;
            for (s32 k = 0; k < b->instCount && cnt < 511; k++) list[cnt++] = b->instArray[k];
            if (b->percussion) list[cnt++] = b->percussion;
            for (s32 k = 0; k < cnt; k++) {
                if (!list[k]) continue;
                insts[ni++] = list[k];
                for (s32 s = 0; s < list[k]->soundCount; s++) {
                    ALSound *sd = list[k]->soundArray[s];
                    if (!sd) continue;
                    snds[ns++] = sd; envs[ne++] = sd->envelope; keys[nk++] = sd->keyMap;
                    if (sd->wavetable) {
                        waves[nw++] = sd->wavetable;
                        if (sd->wavetable->type == AL_ADPCM_WAVE) {
                            loops[nl++] = sd->wavetable->waveInfo.adpcm.loop;
                            books[nb++] = sd->wavetable->waveInfo.adpcm.book;
                        }
                    }
                }
            }
        }
    }
    size_t cI = uniq(insts, ni), cS = uniq(snds, ns), cE = uniq(envs, ne), cK = uniq(keys, nk);
    size_t cW = uniq(waves, nw), cL = uniq(loops, nl), cB = uniq(books, nb);
    printf("game bank: inst %zu sound %zu env %zu key %zu wave %zu loop %zu book %zu\n", cI, cS, cE, cK, cW, cL, cB);
    /* counts from lanes/DATA-4/ctlmap.py, an independent walk of the same offset graph */
    CHECK(cI == 22 && cS == 356 && cE == 356 && cK == 79);
    CHECK(cW == 251 && cL == 38 && cB == 251);
    free(c);
}

int main(void)
{
    synthetic();
    const char *rom = getenv("N64C_TEST_ROM");
    if (rom) game_bank(rom);
    else printf("N64C_TEST_ROM not set: game bank walk skipped\n");
    if (fails) { printf("%d failures\n", fails); return 1; }
    printf("albnk ok\n");
    return 0;
}
