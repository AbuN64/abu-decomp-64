/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Bank reader of the audio library subset. Written from the public bank file layout (ALBankFile, ALBank,
 * ALInstrument, ALSound, ALEnvelope, ALKeyMap, ALWaveTable, ADPCM loop and book field roles) and from the bytes of the
 * game's own control bank; no vendor source. Every read goes through byte accessors, so the bank may sit in the
 * ROM's big-endian order on a host of either order, and every offset is range checked. Shared records (a sound
 * used by several instruments, a wave used by several sounds) are built once. */
#include "n64c_al.h"
#include <stdlib.h>
#include <string.h>

typedef struct {
    const u8 *c;
    u32 len;
    u32 sampleBase;
    ALHeap *heap;
    void **seen;   /* built record per 4-byte slot of the bank */
    int bad;
} Rd;

enum { FLAG_MASK = 0x7FFFFFu };

static u32 u32at(Rd *r, u32 o)
{
    if (o > r->len || r->len - o < 4) { r->bad = 1; return 0; }
    return ((u32)r->c[o] << 24) | ((u32)r->c[o + 1] << 16) | ((u32)r->c[o + 2] << 8) | r->c[o + 3];
}
static u32 u16at(Rd *r, u32 o)
{
    if (o > r->len || r->len - o < 2) { r->bad = 1; return 0; }
    return ((u32)r->c[o] << 8) | r->c[o + 1];
}
static u32 u8at(Rd *r, u32 o)
{
    if (o >= r->len) { r->bad = 1; return 0; }
    return r->c[o];
}
static s32 s32at(Rd *r, u32 o) { return (s32)u32at(r, o); }
static s16 s16at(Rd *r, u32 o) { return (s16)u16at(r, o); }
static u32 offat(Rd *r, u32 o) { return u32at(r, o) & FLAG_MASK; }

static void *alloc(Rd *r, s32 num, s32 size)
{
    void *p = alHeapDBAlloc((u8 *)"albnk", 0, r->heap, num, size);
    if (!p) r->bad = 1;
    return p;
}
/* The memo cell of the record at o (records sit on 4-byte boundaries). */
static void **slot(Rd *r, u32 o)
{
    if (o >= r->len || (o & 3u)) { r->bad = 1; return NULL; }
    return &r->seen[o >> 2];
}

static ALEnvelope *read_env(Rd *r, u32 o)
{
    if (!o) return NULL;
    void **s = slot(r, o);
    if (!s) return NULL;
    if (*s) return *s;
    ALEnvelope *e = alloc(r, 1, sizeof *e);
    if (!e) return NULL;
    *s = e;
    e->attackTime = s32at(r, o);
    e->decayTime = s32at(r, o + 4);
    e->releaseTime = s32at(r, o + 8);
    e->attackVolume = (u8)u8at(r, o + 12);
    e->decayVolume = (u8)u8at(r, o + 13);
    return e;
}
static ALKeyMap *read_key(Rd *r, u32 o)
{
    if (!o) return NULL;
    void **s = slot(r, o);
    if (!s) return NULL;
    if (*s) return *s;
    ALKeyMap *k = alloc(r, 1, sizeof *k);
    if (!k) return NULL;
    *s = k;
    k->velocityMin = (u8)u8at(r, o);
    k->velocityMax = (u8)u8at(r, o + 1);
    k->keyMin = (u8)u8at(r, o + 2);
    k->keyMax = (u8)u8at(r, o + 3);
    k->keyBase = (u8)u8at(r, o + 4);
    k->detune = (s8)u8at(r, o + 5);
    return k;
}
static ALWaveTable *read_wave(Rd *r, u32 o)
{
    if (!o) return NULL;
    void **s = slot(r, o);
    if (!s) return NULL;
    if (*s) return *s;
    ALWaveTable *w = alloc(r, 1, sizeof *w);
    if (!w) return NULL;
    *s = w;
    w->base = (u8 *)(uintptr_t)(r->sampleBase + u32at(r, o));
    w->len = s32at(r, o + 4);
    w->type = (u8)u8at(r, o + 8);
    w->flags = (u8)u8at(r, o + 9);
    u32 lo = offat(r, o + 12);
    if (w->type == AL_ADPCM_WAVE) {
        u32 bk = offat(r, o + 16);
        if (lo) {
            ALADPCMloop *l = alloc(r, 1, sizeof *l);
            if (!l) return NULL;
            l->start = u32at(r, lo);
            l->end = u32at(r, lo + 4);
            l->count = u32at(r, lo + 8);
            for (u32 i = 0; i < 16; i++) l->state[i] = s16at(r, lo + 12 + 2 * i);
            w->waveInfo.adpcm.loop = l;
        }
        if (bk) {
            s32 order = s32at(r, bk), np = s32at(r, bk + 4);
            if (order < 0 || np < 0 || order > 16 || np > 64) { r->bad = 1; return NULL; }
            ALADPCMBook *b = alloc(r, 1, (s32)(sizeof *b + 2u * (u32)order * (u32)np * 8u));
            if (!b) return NULL;
            b->order = order;
            b->npredictors = np;
            for (s32 i = 0; i < order * np * 8; i++) b->book[i] = s16at(r, bk + 8 + 2 * (u32)i);
            w->waveInfo.adpcm.book = b;
        }
    } else if (lo) {
        ALRawLoop *l = alloc(r, 1, sizeof *l);
        if (!l) return NULL;
        l->start = u32at(r, lo);
        l->end = u32at(r, lo + 4);
        l->count = u32at(r, lo + 8);
        w->waveInfo.raw.loop = l;
    }
    return r->bad ? NULL : w;
}
static ALSound *read_sound(Rd *r, u32 o)
{
    if (!o) return NULL;
    void **s = slot(r, o);
    if (!s) return NULL;
    if (*s) return *s;
    ALSound *snd = alloc(r, 1, sizeof *snd);
    if (!snd) return NULL;
    *s = snd;
    snd->envelope = read_env(r, offat(r, o));
    snd->keyMap = read_key(r, offat(r, o + 4));
    snd->wavetable = read_wave(r, offat(r, o + 8));
    snd->samplePan = (u8)u8at(r, o + 12);
    snd->sampleVolume = (u8)u8at(r, o + 13);
    snd->flags = (u8)u8at(r, o + 14);
    return snd;
}
static ALInstrument *read_inst(Rd *r, u32 o)
{
    if (!o) return NULL;
    void **s = slot(r, o);
    if (!s) return NULL;
    if (*s) return *s;
    s32 n = s16at(r, o + 14);
    if (n < 0 || n > 4096) { r->bad = 1; return NULL; }
    ALInstrument *in = alloc(r, 1, (s32)(sizeof *in + sizeof(ALSound *) * (size_t)(n > 1 ? n - 1 : 0)));
    if (!in) return NULL;
    *s = in;
    in->volume = (u8)u8at(r, o);
    in->pan = (u8)u8at(r, o + 1);
    in->priority = (u8)u8at(r, o + 2);
    in->flags = (u8)u8at(r, o + 3);
    in->tremType = (u8)u8at(r, o + 4);
    in->tremRate = (u8)u8at(r, o + 5);
    in->tremDepth = (u8)u8at(r, o + 6);
    in->tremDelay = (u8)u8at(r, o + 7);
    in->vibType = (u8)u8at(r, o + 8);
    in->vibRate = (u8)u8at(r, o + 9);
    in->vibDepth = (u8)u8at(r, o + 10);
    in->vibDelay = (u8)u8at(r, o + 11);
    in->bendRange = s16at(r, o + 12);
    in->soundCount = (s16)n;
    for (s32 i = 0; i < n && !r->bad; i++) in->soundArray[i] = read_sound(r, offat(r, o + 16 + 4 * (u32)i));
    return in;
}
static ALBank *read_bank(Rd *r, u32 o)
{
    if (!o) return NULL;
    void **s = slot(r, o);
    if (!s) return NULL;
    if (*s) return *s;
    s32 n = s16at(r, o);
    if (n < 0 || n > 4096) { r->bad = 1; return NULL; }
    ALBank *b = alloc(r, 1, (s32)(sizeof *b + sizeof(ALInstrument *) * (size_t)(n > 1 ? n - 1 : 0)));
    if (!b) return NULL;
    *s = b;
    b->instCount = (s16)n;
    b->flags = (u8)u8at(r, o + 2);
    b->sampleRate = s32at(r, o + 4);
    b->percussion = read_inst(r, offat(r, o + 8));
    for (s32 i = 0; i < n && !r->bad; i++) b->instArray[i] = read_inst(r, offat(r, o + 12 + 4 * (u32)i));
    return b;
}

ALBankFile *n64c_al_bnkf_read(ALHeap *heap, const u8 *ctl, s32 len, u32 fileOffset, u32 sampleBase)
{
    if (!heap || !ctl || len <= 0) return NULL;
    Rd r = {.c = ctl, .len = (u32)len, .sampleBase = sampleBase, .heap = heap};
    r.seen = calloc((size_t)len / 4 + 1, sizeof(void *));
    if (!r.seen) return NULL;
    ALBankFile *f = NULL;
    s32 n = s16at(&r, fileOffset + 2);
    if (!r.bad && n >= 0 && n <= 256) {
        f = alloc(&r, 1, (s32)(sizeof *f + sizeof(ALBank *) * (size_t)(n > 1 ? n - 1 : 0)));
        if (f) {
            f->revision = s16at(&r, fileOffset);
            f->bankCount = (s16)n;
            for (s32 i = 0; i < n && !r.bad; i++) f->bankArray[i] = read_bank(&r, offat(&r, fileOffset + 4 + 4 * (u32)i));
        }
    }
    free(r.seen);
    return r.bad ? NULL : f;
}

s32 n64c_al_ctl_file(const u8 *ctl, s32 len, s32 n, u32 *fileOffset)
{
    if (!ctl || len < 4) return 0;
    Rd r = {.c = ctl, .len = (u32)len};
    s32 count = s32at(&r, 0);
    if (count <= 0 || count > 256 || n < 0 || n >= count) return 0;
    u32 o = offat(&r, 4 + 8 * (u32)n);
    if (r.bad || o >= (u32)len) return 0;
    if (fileOffset) *fileOffset = o;
    return count;
}
