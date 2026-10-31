/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Synthesizer driver of the audio library subset: voices, players and the audio frame that turns them
 * into an audio command list for the RSP audio ABI (audio_abi/n64a_abi.c is the consumer).
 *
 * Design (ours, from the documented behaviour): output is produced in chunks of at most CHUNK samples.
 * Each chunk first lets the registered players run (they start, stop and re-parameterise voices), then
 * renders every playing voice: DMA the sample data through the application's DMA callback, decode
 * (VADPCM) or copy (16-bit PCM), resample, and mix with a volume ramp into the main and effects buses.
 * The effects bus then goes through the custom reverb network (fxType 6: a ring of delay lines, each
 * an all-pass section with an optional one-pole low-pass and an output tap, parameters from the table
 * the game hands in) and is added to both main buses. The main buses are interleaved and saved. */
#include "n64c_al.h"
#include <math.h>
#include <stdio.h>
#include <string.h>

#define CHUNK 128
#define CMD_LIMIT 2000   /* the game reserves 0x800 commands per frame; keep headroom for the tail */
#define CMD_RESERVE 48

/* RSP audio ABI command numbers and flags (see audio_abi/n64a_abi.c). */
enum { C_ADPCM = 1, C_CLEAR = 2, C_ENVMIX = 3, C_LOAD = 4, C_RESAMPLE = 5, C_SAVE = 6, C_SETBUF = 8,
       C_SETVOL = 9, C_MOVE = 10, C_LOADADPCM = 11, C_MIX = 12, C_INTERLEAVE = 13, C_POLEF = 14, C_SETLOOP = 15,
       C_LOADRAW = 16 /* the shim's own: bytes as they sit in RAM from a cartridge DMA */ };
enum { F_INIT = 1, F_LOOP = 2, F_LEFT = 2, F_VOL = 4, F_AUX = 8 };

/* Working memory layout, offsets relative to the ABI's mixing area. */
enum {
    B_MAIN_L = 0x000, B_MAIN_R = 0x100, B_AUX_L = 0x200, B_AUX_R = 0x300,
    B_STREAM = 0x410,   /* 16 bytes of resampler history sit below */
    B_DECIN = 0x620,    /* compressed frames */
    B_DEC = 0x6E0,      /* decoder output (16 history samples first); also raw PCM, resampler output, interleave */
    B_RES = 0x6E0,
    B_INTER = 0x6E0,
    /* effects network scratch (the voice scratch above is free once every voice has been rendered) */
    B_FX_X = 0x6E0, B_FX_Y = 0x7E0, B_FX_ACC = 0x8E0
};

#define FX_MAX_DELAYS 16
typedef struct N64cFxDelay {
    s32 in, out;          /* samples behind the head: where the input tap reads and the output slot sits */
    s32 fb, ff, gain;     /* feedback and feed-forward coefficients (Q15), output level (Q15) */
    s32 filter;           /* one-pole low-pass coefficient (Q15); 0: none */
    s16 *table;           /* 16 halfwords for the polef command (8 + 8) */
    u8 *state;            /* 16 bytes of filter memory */
} N64cFxDelay;
typedef struct N64cFx {
    s32 count;
    s32 ringLen;          /* samples */
    s32 head;
    u8 *ring;
    N64cFxDelay d[FX_MAX_DELAYS];
} N64cFx;

enum { V_FREE = 0, V_ALLOC, V_PLAY, V_ENDED };

struct N64cVoice {
    u8 state;
    u8 pan, fxmix;
    u8 first;        /* nothing rendered yet: resampler starts from silence */
    u8 adpcmFresh;   /* decoder history starts at zero */
    u8 loopJump;     /* the next decode starts at the loop point with the loop's history */
    void *tag;
    s32 priority;
    u32 age;
    N64cSynth *syn;
    ALWaveTable *wave;
    u32 total;       /* samples in the wave */
    u32 pos;         /* next sample of the wave to deliver to the resampler */
    u32 nextFrame;   /* VADPCM: next frame to decode */
    s32 lead;        /* decoded samples not yet delivered (>= 0), or samples to discard (< 0) */
    u32 accu;        /* resampler phase, 16 bits */
    u32 loopsLeft;   /* 0: not looping; 0xFFFFFFFF: forever */
    u32 pitchField;  /* resampler ratio, 1.15 fixed point */
    s16 vol;         /* envelope scalar, 0..32767 */
    s16 volTgt;
    s32 rampLeft;    /* samples left on the ramp to volTgt */
    f32 curL, curR;  /* mixing gain at the end of the previous chunk */
    u8 *adpcmState;  /* 32 bytes: last decoded frame */
    u8 *resState;    /* 16 bytes: resampler history and phase */
    u8 *mixState;    /* 80 bytes: envelope mixer state */
    u8 *book;        /* BOOK_MAX bytes: the wave's codebook, copied where the RSP can read it */
    u8 *loopState;   /* 32 bytes: the wave's loop history, copied likewise */
    ALDMAproc dma;   /* this voice's sample DMA, from the game's dmaproc */
    void *dmaState;
};

struct N64cSynth {
    ALHeap *heap;
    s32 outputRate;
    s32 numVoices;
    N64cVoice *voices;
    ALLink players;      /* head sentinel */
    u32 clock;           /* voice age */
    s32 fxType;
    N64cFx *fx;
};

static N64cSynth *g_synth;

/* Per-voice RSP state: decoder 32, resampler 16, envelope mixer 80, codebook (order 2 x 8 predictors) 256, loop 32.
 * The RSP reads physical RAM without a TLB, so a codebook or loop record inside a TLB-mapped bank (the game builds
 * its banks there) is copied into this block when the voice starts, as libultra copies the loop history. */
enum { BOOK_MAX = 256, VOICE_STATE = 128 + BOOK_MAX + 32 };

static u32 phys(const void *p) { return osVirtualToPhysical((void *)(uintptr_t)p); }

/* ---- command emitters ---- */
static Acmd *emit(Acmd *p, u32 w0, u32 w1)
{
    p->w0 = w0;
    p->w1 = w1;
    return p + 1;
}
static Acmd *buffers(Acmd *p, u32 in, u32 out, u32 count)
{
    return emit(p, (C_SETBUF << 24) | (in & 0xffffu), (out << 16) | (count & 0xffffu));
}
static Acmd *buffers_aux(Acmd *p, u32 dryR, u32 wetL, u32 wetR)
{
    return emit(p, (C_SETBUF << 24) | (F_AUX << 16) | (dryR & 0xffffu), (wetL << 16) | (wetR & 0xffffu));
}
static Acmd *clearbuf(Acmd *p, u32 dmem, u32 bytes)
{
    return emit(p, (C_CLEAR << 24) | (dmem & 0xffffu), bytes & 0xfffu);
}
static Acmd *loadraw(Acmd *p, u32 addr) { return emit(p, C_LOADRAW << 24, addr); }
static Acmd *savebuf(Acmd *p, u32 addr) { return emit(p, C_SAVE << 24, addr); }
static Acmd *movebuf(Acmd *p, u32 from, u32 to, u32 bytes)
{
    return emit(p, (C_MOVE << 24) | (from & 0xffffu), (to << 16) | (bytes & 0xffffu));
}

/* ---- effects network ---- */
static void put_h(s16 *base, u32 i, s32 v) { base[i ^ 1u] = (s16)v; }  /* guest halfword order inside a word */

/* params: { count, ring length in samples, then per delay: input, output, feedback, feed-forward, gain,
 * chorus rate, chorus depth, low-pass coefficient }. The chorus fields are not used by the game's table. */
static N64cFx *fx_new(ALHeap *heap, const s32 *p)
{
    s32 n = p[0], len = p[1];
    if (n <= 0 || n > FX_MAX_DELAYS || len < 64 || len > (1 << 20)) return NULL;
    N64cFx *fx = alHeapDBAlloc(NULL, 0, heap, 1, (s32)sizeof *fx);
    if (!fx) return NULL;
    fx->ring = alHeapDBAlloc(NULL, 0, heap, len, 2);
    if (!fx->ring) return NULL;
    fx->count = n;
    fx->ringLen = len;
    for (s32 i = 0; i < n; i++) {
        const s32 *e = p + 2 + 8 * i;
        N64cFxDelay *d = &fx->d[i];
        d->in = e[0] % len;
        d->out = e[1] % len;
        d->fb = e[2];
        d->ff = e[3];
        d->gain = e[4];
        d->filter = e[7];
        d->table = alHeapDBAlloc(NULL, 0, heap, 1, 32);
        d->state = alHeapDBAlloc(NULL, 0, heap, 1, 16);
        if (!d->table || !d->state) return NULL;
        if (d->filter) {
            /* first half zero; second half is the pole's response, c/2 * (c/32768)^k */
            const f32 r = ((f32)d->filter - 1.5f) / 32768.0f;
            f32 a = (f32)d->filter * 0.5f;
            for (s32 k = 0; k < 8; k++) { put_h(d->table, (u32)(8 + k), (s32)a); a *= r; }
        }
    }
    return fx;
}

static Acmd *emit_mix(Acmd *c, u32 out, u32 in, s32 gain)
{
    return emit(c, (C_MIX << 24) | ((u32)gain & 0xffffu), (in << 16) | out);
}

/* Moves `n` samples between the ring at sample index idx (wrapping) and dmem. */
static Acmd *fx_ring(Acmd *c, const N64cFx *fx, u32 idx, u32 n, u32 dmem, int save)
{
    const u32 first = (u32)fx->ringLen - idx < n ? (u32)fx->ringLen - idx : n;
    for (u32 part = 0, at = idx, done = 0; part < 2 && done < n; part++) {
        const u32 cnt = part == 0 ? first : n - first;
        c = save ? buffers(c, 0, dmem + done * 2u, cnt * 2u) : buffers(c, dmem + done * 2u, 0, cnt * 2u);
        c = emit(c, (save ? C_SAVE : C_LOAD) << 24, phys(fx->ring) + at * 2u);
        done += cnt;
        at = 0;
    }
    return c;
}

static Acmd *fx_apply(N64cFx *fx, Acmd *c, u32 n)
{
    const u32 nb = n * 2u, R = (u32)fx->ringLen, h = (u32)fx->head;
    /* wet send as mono into the ring head */
    c = buffers(c, 0, B_AUX_L, nb);
    c = emit_mix(c, B_AUX_L, B_AUX_L, (s16)0xda83);
    c = emit_mix(c, B_AUX_L, B_AUX_R, 0x5a82);
    c = fx_ring(c, fx, h, n, B_AUX_L, 1);
    c = clearbuf(c, B_FX_ACC, nb);
    for (s32 i = 0; i < fx->count; i++) {
        const N64cFxDelay *d = &fx->d[i];
        const u32 inIdx = (h + R - (u32)d->in) % R, outIdx = (h + R - (u32)d->out) % R;
        c = fx_ring(c, fx, inIdx, n, B_FX_X, 0);
        c = fx_ring(c, fx, outIdx, n, B_FX_Y, 0);
        c = buffers(c, 0, 0, nb);
        if (d->ff) c = emit_mix(c, B_FX_Y, B_FX_X, d->ff);
        if (d->fb) c = emit_mix(c, B_FX_X, B_FX_Y, d->fb);
        c = fx_ring(c, fx, inIdx, n, B_FX_X, 1);
        if (d->filter) {
            c = buffers(c, B_FX_Y, B_FX_Y, nb);
            c = emit(c, (C_LOADADPCM << 24) | 0x20u, phys(d->table));
            c = emit(c, (C_POLEF << 24) | (u32)((0x8000 - d->filter + 1) / 2), phys(d->state));
        }
        c = fx_ring(c, fx, outIdx, n, B_FX_Y, 1);
        if (d->gain) {
            c = buffers(c, 0, 0, nb);
            c = emit_mix(c, B_FX_ACC, B_FX_Y, d->gain);
        }
    }
    c = buffers(c, 0, 0, nb);
    c = emit_mix(c, B_MAIN_L, B_FX_ACC, 0x7fff);
    c = emit_mix(c, B_MAIN_R, B_FX_ACC, 0x7fff);
    fx->head = (s32)((h + n) % R);
    return c;
}

/* ---- synthesizer ---- */
N64cSynth *n64c_syn_global(void) { return g_synth; }

s32 n64c_syn_chunk_micros(const N64cSynth *syn)
{
    return syn && syn->outputRate > 0 ? (s32)((int64_t)CHUNK * 1000000 / syn->outputRate) : 5000;
}

static s32 micros_to_samples(const N64cSynth *syn, s32 micros)
{
    if (micros <= 0) return 0;
    return (s32)((int64_t)micros * syn->outputRate / 1000000);
}

static s32 samples_to_micros(const N64cSynth *syn, s32 samples)
{
    return syn->outputRate > 0 ? (s32)((int64_t)samples * 1000000 / syn->outputRate) : 0;
}

void alInit(ALGlobals *g, ALSynConfig *c)
{
    g->drvr = NULL;
    g_synth = NULL;
    if (!c || !c->heap) return;
    N64cSynth *syn = alHeapDBAlloc(NULL, 0, c->heap, 1, (s32)sizeof *syn);
    s32 n = c->maxPVoices > 0 ? c->maxPVoices : c->maxVVoices;
    if (n <= 0) n = 1;
    if (!syn) return;
    syn->voices = alHeapDBAlloc(NULL, 0, c->heap, n, (s32)sizeof(N64cVoice));
    u8 *states = alHeapDBAlloc(NULL, 0, c->heap, n, VOICE_STATE);
    if (!syn->voices || !states) return;
    syn->heap = c->heap;
    syn->numVoices = n;
    syn->outputRate = c->outputRate > 0 ? c->outputRate : 32000;
    syn->fxType = c->fxType;
    for (s32 i = 0; i < n; i++) {
        syn->voices[i].syn = syn;
        u8 *st = states + (size_t)i * VOICE_STATE;
        syn->voices[i].adpcmState = st;
        syn->voices[i].resState = st + 32;
        syn->voices[i].mixState = st + 48;
        syn->voices[i].book = st + 128;
        syn->voices[i].loopState = st + 128 + BOOK_MAX;
        /* As alSynNew: each physical voice asks the game's dmaproc for its own DMA routine and state. */
        if (c->dmaproc) syn->voices[i].dma = c->dmaproc(&syn->voices[i].dmaState);
    }
    if (c->fxType == 6 && c->params) syn->fx = fx_new(c->heap, (const s32 *)c->params);
    g->drvr = syn;
    g_synth = syn;
}

void alClose(ALGlobals *g)
{
    if (g_synth == g->drvr) g_synth = NULL;
    g->drvr = NULL;
}

void n64c_syn_add_player(N64cSynth *syn, N64cPlayer *p)
{
    p->synth = syn;
    p->samplesLeft = 0;
    p->samplesSince = 0;
    alLink(&p->node, &syn->players);
}

void n64c_syn_remove_player(N64cSynth *syn, N64cPlayer *p)
{
    (void)syn;
    alUnlink(&p->node);
    p->node.next = p->node.prev = NULL;
}

/* ---- voices ---- */
N64cVoice *n64c_voice_alloc(N64cSynth *syn, void *tag, s32 priority)
{
    if (!syn) return NULL;
    N64cVoice *pick = NULL;
    for (s32 i = 0; i < syn->numVoices; i++) {
        N64cVoice *v = &syn->voices[i];
        if (v->state == V_FREE) { pick = v; break; }
    }
    if (!pick) {
        /* A finished voice is free for the taking; otherwise the lowest priority, oldest first, if it ranks no higher. */
        for (s32 i = 0; i < syn->numVoices; i++) {
            N64cVoice *v = &syn->voices[i];
            if (v->state == V_ENDED) { pick = v; break; }
            if (!pick || v->priority < pick->priority || (v->priority == pick->priority && v->age < pick->age)) pick = v;
        }
        if (pick->state != V_ENDED && pick->priority > priority) return NULL;
    }
    N64cVoice keep = *pick;
    memset(pick, 0, sizeof *pick);
    pick->syn = keep.syn;
    pick->adpcmState = keep.adpcmState;
    pick->resState = keep.resState;
    pick->mixState = keep.mixState;
    pick->book = keep.book;
    pick->loopState = keep.loopState;
    pick->dma = keep.dma;
    pick->dmaState = keep.dmaState;
    pick->state = V_ALLOC;
    pick->tag = tag;
    pick->priority = priority;
    pick->age = ++syn->clock;
    pick->pan = 64;
    pick->pitchField = 0x8000;
    pick->first = 1;
    pick->adpcmFresh = 1;
    return pick;
}

static u32 book_bytes(const ALADPCMBook *book) { return (u32)(book->order * book->npredictors * 16); }

int n64c_voice_valid(const N64cVoice *v, const void *tag)
{
    return v && v->state != V_FREE && v->tag == tag;
}

void n64c_voice_start(N64cVoice *v, ALWaveTable *wave)
{
    if (!v || !wave || v->state == V_FREE) return;
    v->wave = wave;
    if (wave->type == AL_RAW16_WAVE) {
        v->total = (u32)wave->len / 2;
        const ALRawLoop *lp = wave->waveInfo.raw.loop;
        v->loopsLeft = lp && lp->count ? lp->count : 0;
    } else {
        v->total = ((u32)wave->len / 9) * 16;
        const ALADPCMloop *lp = wave->waveInfo.adpcm.loop;
        const ALADPCMBook *book = wave->waveInfo.adpcm.book;
        v->loopsLeft = lp && lp->count ? lp->count : 0;
        if (book) {
            u32 bytes = book_bytes(book);
            if (bytes > BOOK_MAX) {
                fprintf(stderr, "n64c-alsyn: codebook of %u bytes exceeds %u: voice not started\n", (unsigned)bytes,
                        (unsigned)BOOK_MAX);
                v->state = V_ENDED;
                return;
            }
            memcpy(v->book, book->book, bytes);
        }
        if (lp) memcpy(v->loopState, lp->state, sizeof lp->state);
    }
    v->pos = 0;
    v->nextFrame = 0;
    v->lead = 0;
    v->accu = 0;
    v->state = V_PLAY;
}

void n64c_voice_free(N64cVoice *v)
{
    if (v) v->state = V_FREE;
}

void n64c_voice_set_pitch(N64cVoice *v, f32 ratio)
{
    if (!v) return;
    f32 f = ratio * 32768.0f + 0.5f;
    v->pitchField = f < 1.0f ? 1u : (f > 65535.0f ? 65535u : (u32)f);
}

void n64c_voice_set_vol(N64cVoice *v, s16 vol, s32 rampMicros)
{
    if (!v) return;
    if (vol < 0) vol = 0;
    /* libaudio's envelope mixer maps the volume to loudness non-linearly: (vol * vol) >> 15. */
    vol = (s16)(((s32)vol * (s32)vol) >> 15);
    v->volTgt = vol;
    v->rampLeft = micros_to_samples(v->syn, rampMicros);
    if (v->rampLeft <= 0) { v->vol = vol; v->rampLeft = 0; }
}

void n64c_voice_set_pan(N64cVoice *v, u8 pan) { if (v) v->pan = pan > 127 ? 127 : pan; }
void n64c_voice_set_fxmix(N64cVoice *v, u8 mix) { if (v) v->fxmix = mix > 127 ? 127 : mix; }
void n64c_voice_set_priority(N64cVoice *v, s32 priority) { if (v) v->priority = priority; }
int n64c_voice_playing(const N64cVoice *v) { return v && v->state == V_PLAY; }

/* ---- rendering ---- */
static u32 pitch_q16(const N64cVoice *v) { return v->pitchField << 1; }

static Acmd *dma_load(N64cVoice *v, Acmd *c, u32 romAddr, u32 bytes, u32 dmem, u32 *off)
{
    u32 pa = v->dma ? (u32)v->dma((s32)romAddr, (s32)bytes, v->dmaState) : 0;
    *off = 0;
    c = buffers(c, dmem, 0, bytes);
    return loadraw(c, pa);
}

/* Appends `take` samples of the wave, starting at the voice position, to the resampler input at sample w. */
static Acmd *piece(N64cVoice *v, Acmd *c, u32 take, u32 w)
{
    const ALWaveTable *wt = v->wave;
    u32 off;
    if (wt->type == AL_RAW16_WAVE) {
        c = dma_load(v, c, (u32)(uintptr_t)wt->base + v->pos * 2u, take * 2u, B_DEC, &off);
        c = movebuf(c, B_DEC + off, B_STREAM + 2u * w, take * 2u);
        v->pos += take;
        return c;
    }
    const ALADPCMBook *book = wt->waveInfo.adpcm.book;
    s32 need = (s32)take - v->lead;
    u32 frames = need > 0 ? (u32)(need + 15) / 16u : 0u;
    off = 0;
    if (frames) c = dma_load(v, c, (u32)(uintptr_t)wt->base + v->nextFrame * 9u, frames * 9u, B_DECIN, &off);
    if (book) {
        c = emit(c, (C_LOADADPCM << 24) | book_bytes(book), phys(v->book));
    }
    u32 flags = 0;
    if (v->adpcmFresh) flags |= F_INIT;
    if (v->loopJump) {
        flags |= F_LOOP;
        c = emit(c, C_SETLOOP << 24, phys(v->loopState));
    }
    c = buffers(c, B_DECIN + off, B_DEC, frames * 32u);
    c = emit(c, (C_ADPCM << 24) | (flags << 16), phys(v->adpcmState));
    v->adpcmFresh = 0;
    v->loopJump = 0;
    v->nextFrame += frames;
    c = movebuf(c, (u32)((s32)B_DEC + 2 * (16 - v->lead)), B_STREAM + 2u * w, take * 2u);
    v->lead = v->lead + (s32)(16u * frames) - (s32)take;
    v->pos += take;
    return c;
}

static void loop_jump(N64cVoice *v)
{
    if (v->wave->type == AL_RAW16_WAVE) {
        v->pos = v->wave->waveInfo.raw.loop->start;
    } else {
        const ALADPCMloop *lp = v->wave->waveInfo.adpcm.loop;
        v->pos = lp->start;
        v->nextFrame = lp->start >> 4;
        v->lead = -(s32)(lp->start & 15u);
        v->loopJump = 1;
    }
    if (v->loopsLeft != 0xFFFFFFFFu) v->loopsLeft--;
}

static u32 loop_end(const N64cVoice *v)
{
    if (!v->loopsLeft) return v->total;
    u32 e = v->wave->type == AL_RAW16_WAVE ? v->wave->waveInfo.raw.loop->end : v->wave->waveInfo.adpcm.loop->end;
    return e < v->total ? e : v->total;
}

static s16 sat_q15(f32 x) { return x <= 0.0f ? 0 : (x >= 32767.0f ? 32767 : (s16)(x + 0.5f)); }

/* Rate per 8-sample group (16.16) that carries a gain from `from` to `to` over `groups` groups; gains below 1
 * count as 1 (a multiplicative ramp cannot leave or reach zero). */
static s32 ramp_rate(f32 from, f32 to, u32 groups)
{
    if (from < 1.0f) from = 1.0f;
    if (to < 1.0f) to = 1.0f;
    f32 r = powf(to / from, 1.0f / (f32)(groups ? groups : 1u));
    if (r < 0.01f) r = 0.01f;
    if (r > 64.0f) r = 64.0f;
    return (s32)(r * 65536.0f);
}

static Acmd *render_voice(N64cVoice *v, Acmd *c, u32 n)
{
    const u32 p = pitch_q16(v);
    const u32 total = (v->accu + n * p) >> 16;
    u32 w = 0;
    /* 1. assemble the resampler input: `total` samples, following loops */
    while (w < total) {
        u32 limit = loop_end(v);
        if (v->pos >= limit) {
            if (v->loopsLeft) { loop_jump(v); continue; }
            v->state = V_ENDED;
            break;
        }
        u32 take = total - w;
        if (take > limit - v->pos) take = limit - v->pos;
        c = piece(v, c, take, w);
        w += take;
    }
    if (w < total) c = clearbuf(c, B_STREAM + 2u * w, ((total - w) * 2u + 15u) & ~15u);
    /* 2. resample */
    c = buffers(c, B_STREAM, B_RES, n * 2u);
    c = emit(c, (C_RESAMPLE << 24) | ((v->first ? (u32)F_INIT : 0u) << 16) | v->pitchField, phys(v->resState));
    v->first = 0;
    v->accu = (v->accu + n * p) & 0xffffu;
    /* 3. envelope scalar, pan and effects send for this chunk */
    f32 vol = (f32)v->vol;
    if (v->rampLeft > 0) {
        if ((s32)n >= v->rampLeft) { vol = (f32)v->volTgt; v->vol = v->volTgt; v->rampLeft = 0; }
        else {
            vol += ((f32)v->volTgt - vol) * (f32)n / (f32)v->rampLeft;
            v->vol = sat_q15(vol);
            v->rampLeft -= (s32)n;
        }
    }
    const f32 th = (f32)v->pan * (1.5707963f / 127.0f);
    const f32 eL = vol * cosf(th), eR = vol * sinf(th);
    const f32 fx = (f32)v->fxmix * (1.5707963f / 127.0f);
    const u32 groups = n / 8u;
    c = buffers(c, B_RES, B_MAIN_L, n * 2u);
    c = buffers_aux(c, B_MAIN_R, B_AUX_L, B_AUX_R);
    c = emit(c, (C_SETVOL << 24) | ((u32)(F_LEFT | F_VOL) << 16) | (u16)sat_q15(v->curL), 0);
    c = emit(c, (C_SETVOL << 24) | ((u32)F_LEFT << 16) | (u16)sat_q15(eL), (u32)ramp_rate(v->curL, eL, groups));
    c = emit(c, (C_SETVOL << 24) | ((u32)F_VOL << 16) | (u16)sat_q15(v->curR), 0);
    c = emit(c, (C_SETVOL << 24) | (u32)0 | (u16)sat_q15(eR), (u32)ramp_rate(v->curR, eR, groups));
    c = emit(c, (C_SETVOL << 24) | ((u32)F_AUX << 16) | (u16)sat_q15(32767.0f * cosf(fx)), (u32)(u16)sat_q15(32767.0f * sinf(fx)));
    c = emit(c, (C_ENVMIX << 24) | ((u32)(F_INIT | F_AUX) << 16), phys(v->mixState));
    v->curL = eL;
    v->curR = eR;
    return c;
}

static void run_players(N64cSynth *syn, u32 n)
{
    for (ALLink *l = syn->players.next; l; l = l->next) {
        N64cPlayer *p = (N64cPlayer *)l;
        p->samplesSince += (s32)n;
        p->samplesLeft -= (s32)n;
        while (p->samplesLeft <= 0) {
            s32 delta = p->handler(p, samples_to_micros(syn, p->samplesSince));
            p->samplesSince = 0;
            s32 s = micros_to_samples(syn, delta);
            if (s < 1) s = 1;   /* at most once per chunk */
            p->samplesLeft += s;
            if (p->samplesLeft <= 0) p->samplesLeft = 1;
        }
    }
}

Acmd *alAudioFrame(Acmd *cmdList, s32 *cmdLen, s16 *outBuf, s32 outLen)
{
    N64cSynth *syn = g_synth;
    Acmd *c = cmdList;
    if (!syn || outLen <= 0) {
        *cmdLen = 0;
        return cmdList;
    }
    s32 done = 0;
    while (outLen - done >= 8) {
        u32 n = (u32)(outLen - done);
        if (n > CHUNK) n = CHUNK;
        n &= ~7u;
        run_players(syn, n);
        c = clearbuf(c, B_MAIN_L, 0x400);
        for (s32 i = 0; i < syn->numVoices; i++) {
            N64cVoice *v = &syn->voices[i];
            if (v->state != V_PLAY) continue;
            if ((c - cmdList) > CMD_LIMIT - CMD_RESERVE) break;
            c = render_voice(v, c, n);
        }
        if (syn->fx) c = fx_apply(syn->fx, c, n);
        c = buffers(c, 0, B_INTER, n * 2u);
        c = emit(c, C_INTERLEAVE << 24, ((u32)B_MAIN_L << 16) | (u32)B_MAIN_R);
        c = buffers(c, 0, B_INTER, n * 4u);
        c = savebuf(c, (u32)(uintptr_t)outBuf + (u32)done * 4u);
        done += (s32)n;
    }
    *cmdLen = (s32)(c - cmdList);
    return c;
}
