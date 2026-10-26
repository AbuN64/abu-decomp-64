/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Audio library subset: heap and list semantics, the sound player's state machine, voice priority, and the
 * command list that alAudioFrame builds, executed through the audio ABI core (n64a_abi) so the PCM it
 * implies is checked. The test RAM is a plain array: addresses in the commands are made relative to it
 * before the list runs. Cartridge bytes (sample data, sequences) sit as the PI layer leaves them, byte i at i; the codebook and
 * loop state halfwords are in the ABI's word-swapped layout, as the control-bank byte-order map delivers them. */
#include "../n64c_al.h"
#include "../audio_abi/n64a_abi.h"
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

enum { RAM_BYTES = 4 << 20, HEAP_OFF = 0x10000, HEAP_LEN = 0x100000, ROM_OFF = 0x200000, OUT_OFF = 0x300000, LIST_OFF = 0x310000, BOOK_OFF = 0x330000, LOOP_OFF = 0x331000 };
static _Alignas(16) u8 ram[RAM_BYTES];

static void put8(u32 a, u8 v) { ram[a] = v; }
/* A big-endian halfword of raw PCM, as a cartridge DMA leaves it. */
static void put_pcm(u32 a, s16 v) { ram[a] = (u8)((u16)v >> 8); ram[a + 1] = (u8)v; }
static s16 get16(u32 a) { return n64a_ram_s16(ram, a); }

static s32 rom_dma(s32 addr, s32 len, void *state)
{
    (void)len;
    (void)state;
    return ROM_OFF + addr;
}
static ALDMAproc rom_dma_new(void **state)
{
    *state = NULL;
    return rom_dma;
}

static ALHeap heap;
static ALGlobals globals;

static void start_synth(s32 voices)
{
    alHeapInit(&heap, ram + HEAP_OFF, HEAP_LEN);
    ALSynConfig c = {.maxVVoices = voices, .maxPVoices = voices, .maxUpdates = 0x100, .dmaproc = rom_dma_new,
                     .heap = &heap, .outputRate = 22050};
    alInit(&globals, &c);
}

/* Addresses in the commands are native pointers into ram: rebase them to offsets for the ABI core. */
static void rebase(Acmd *list, s32 n)
{
    for (s32 i = 0; i < n; i++) {
        switch (list[i].w0 >> 24) {
        case 1: case 3: case 5: case 11: case 15: case 14:
            list[i].w1 -= (u32)(uintptr_t)ram;
            break;
        case 4: case 6:   /* ring transfers carry pointers; DMA loads and the output save carry offsets */
            if (list[i].w1 >= (u32)(uintptr_t)ram) list[i].w1 -= (u32)(uintptr_t)ram;
            break;
        default: break;
        }
    }
}

/* One frame of `samples` stereo frames at OUT_OFF; returns the number of commands. */
static s32 run_frame(N64aAbi *abi, s32 samples)
{
    Acmd *list = (Acmd *)(ram + LIST_OFF);
    s32 n = 0;
    Acmd *end = alAudioFrame(list, &n, (s16 *)(uintptr_t)OUT_OFF, samples);
    CHECK(end == list + n);
    for (s32 i = 0; i < n; i++) CHECK(list[i].w0 >> 24 <= 16);
    if (n) {
        /* the save command carries the output offset directly; everything else is rebased */
        rebase(list, n);
        CHECK(n64a_abi_run(abi, ram, LIST_OFF, (u32)n * 8u) == 0);
    }
    return n;
}

static void test_heap_and_links(void)
{
    ALHeap h;
    alHeapInit(&h, ram + 0x1001, 256);          /* unaligned base */
    u8 *a = alHeapDBAlloc(NULL, 0, &h, 1, 10);
    u8 *b = alHeapDBAlloc(NULL, 0, &h, 3, 8);
    CHECK(a && b && ((uintptr_t)a & 15u) == 0 && ((uintptr_t)b & 15u) == 0 && b >= a + 16);
    for (int i = 0; i < 24; i++) CHECK(b[i] == 0);
    CHECK(h.count == 2);
    CHECK(alHeapDBAlloc(NULL, 0, &h, 1, 1 << 20) == NULL);
    CHECK(alHeapDBAlloc(NULL, 0, &h, 0, 8) == NULL);

    ALLink n0 = {0}, n1 = {0}, n2 = {0};
    alLink(&n1, &n0);
    alLink(&n2, &n1);
    CHECK(n0.next == &n1 && n1.next == &n2 && n2.next == NULL && n2.prev == &n1 && n1.prev == &n0);
    ALLink mid = {0};
    alLink(&mid, &n0);                           /* insert after n0 */
    CHECK(n0.next == &mid && mid.next == &n1 && n1.prev == &mid);
    alUnlink(&mid);
    CHECK(n0.next == &n1 && n1.prev == &n0);
    alUnlink(&n2);
    CHECK(n1.next == NULL);
}

static void test_events(void)
{
    ALHeap h;
    alHeapInit(&h, ram + 0x20000, 4096);
    N64cEvtq q;
    CHECK(n64c_evtq_init(&q, &h, 3) == 0);
    N64cEvt e = {.type = 1}, got;
    CHECK(n64c_evtq_post(&q, &e, 300) == 0);
    e.type = 2;
    CHECK(n64c_evtq_post(&q, &e, 100) == 0);
    e.type = 3;
    CHECK(n64c_evtq_post(&q, &e, 100) == 0);
    CHECK(n64c_evtq_post(&q, &e, 0) == -1);       /* pool of three is exhausted */
    CHECK(n64c_evtq_next_delta(&q) == 100);
    CHECK(!n64c_evtq_pop(&q, &got));
    n64c_evtq_advance(&q, 100);
    CHECK(n64c_evtq_pop(&q, &got) && got.type == 2);   /* equal times keep posting order */
    CHECK(n64c_evtq_pop(&q, &got) && got.type == 3);
    CHECK(!n64c_evtq_pop(&q, &got));
    CHECK(n64c_evtq_next_delta(&q) == 200);
    n64c_evtq_flush(&q, 1, 0);
    CHECK(n64c_evtq_next_delta(&q) == -1);
    CHECK(n64c_evtq_post(&q, &e, 0) == 0);         /* flushed slots are free again */
}

/* A raw 16-bit wave of `len` samples with value `v` at ROM offset off. */
static void make_raw(ALWaveTable *w, u32 off, u32 len, s16 v)
{
    for (u32 i = 0; i < len; i++) put_pcm(ROM_OFF + off + 2 * i, v);
    memset(w, 0, sizeof *w);
    w->base = (u8 *)(uintptr_t)off;
    w->len = (s32)(2 * len);
    w->type = AL_RAW16_WAVE;
}

static int stereo_peak(s32 frames, int ch)
{
    int p = 0;
    for (s32 i = 0; i < frames; i++) {
        int v = get16(OUT_OFF + (u32)(4 * i + 2 * ch));
        if (v < 0) v = -v;
        if (v > p) p = v;
    }
    return p;
}

static void test_sound_player_raw(void)
{
    start_synth(4);
    N64cSynth *syn = n64c_syn_global();
    CHECK(syn != NULL && globals.drvr == syn);
    ALSndPlayer sp;
    ALSndpConfig sc = {.maxSounds = 3, .maxEvents = 16, .heap = &heap};
    alSndpNew(&sp, &sc);
    ALWaveTable w;
    make_raw(&w, 0x1000, 4096, 8000);
    ALEnvelope env = {.attackTime = 0, .decayTime = 0, .releaseTime = 20000, .attackVolume = 127, .decayVolume = 127};
    /* the sound player ignores the sample volume (libultra sndplayer): half of it leaves the level unchanged */
    ALSound snd = {.envelope = &env, .wavetable = &w, .samplePan = 64, .sampleVolume = 64};
    s16 id = alSndpAllocate(&sp, &snd);
    CHECK(id == 0);
    alSndpSetSound(&sp, id);
    CHECK(alSndpGetState(&sp) == AL_STOPPED);

    N64aAbi *abi = n64a_abi_create(N64A_FLAVOUR_AUDIO);
    memset(ram + OUT_OFF, 0, 4096);
    CHECK(run_frame(abi, 0) == 0);                  /* nothing requested, nothing built */
    s32 n = run_frame(abi, 256);
    CHECK(n > 0);
    CHECK(stereo_peak(256, 0) == 0 && stereo_peak(256, 1) == 0);   /* silent without voices */

    alSndpSetPan(&sp, 64);
    alSndpPlay(&sp);
    CHECK(alSndpGetState(&sp) == AL_PLAYING);
    run_frame(abi, 256);
    /* second chunk is steady state: gain 0x7fff * cos(pan) twice through Q15 */
    int l = get16(OUT_OFF + 4u * 200u), r = get16(OUT_OFF + 4u * 200u + 2u);
    CHECK(l > 5400 && l < 5800);
    CHECK(r > 5400 && r < 5800);
    /* hard left: the right channel falls silent after the ramp */
    alSndpSetPan(&sp, 0);
    run_frame(abi, 256);
    run_frame(abi, 256);
    CHECK(stereo_peak(256, 0) > 7500 && stereo_peak(256, 1) < 100);
    alSndpSetPan(&sp, 127);
    run_frame(abi, 256);
    run_frame(abi, 256);
    CHECK(stereo_peak(256, 1) > 7500 && stereo_peak(256, 0) < 100);
    /* half volume is a quarter of the loudness: libaudio squares it (vol * vol >> 15) */
    alSndpSetPan(&sp, 64);
    alSndpSetVol(&sp, 0x4000);
    run_frame(abi, 256);
    run_frame(abi, 256);
    l = get16(OUT_OFF + 4u * 200u), r = get16(OUT_OFF + 4u * 200u + 2u);
    CHECK(l > 1300 && l < 1500);
    CHECK(r > 1300 && r < 1500);

    alSndpStop(&sp);
    CHECK(alSndpGetState(&sp) == AL_STOPPING);
    for (int i = 0; i < 4; i++) run_frame(abi, 256);   /* 1024 samples = 46 ms > the 20 ms release */
    CHECK(alSndpGetState(&sp) == AL_STOPPED);
    run_frame(abi, 256);
    CHECK(stereo_peak(256, 0) == 0 && stereo_peak(256, 1) == 0);

    /* a one-shot wave runs out by itself and the sound stops */
    ALWaveTable shortw;
    make_raw(&shortw, 0x4000, 300, 1000);
    ALSound ss = {.envelope = NULL, .wavetable = &shortw, .samplePan = 64, .sampleVolume = 127};
    s16 id2 = alSndpAllocate(&sp, &ss);
    CHECK(id2 == 1);
    alSndpSetSound(&sp, id2);
    alSndpPlay(&sp);
    CHECK(alSndpGetState(&sp) == AL_PLAYING);
    for (int i = 0; i < 4; i++) run_frame(abi, 256);
    CHECK(alSndpGetState(&sp) == AL_STOPPED);

    /* allocation limits and deallocation */
    CHECK(alSndpAllocate(&sp, &snd) == 2);
    CHECK(alSndpAllocate(&sp, &snd) == -1);
    alSndpDeallocate(&sp, 1);
    CHECK(alSndpAllocate(&sp, &snd) == 1);
    n64a_abi_destroy(abi);
    alClose(&globals);
    CHECK(n64c_syn_global() == NULL);
}

/* VADPCM wave with an all-zero predictor book: each nibble n at scale s decodes to n << s, so the decoded
 * stream is known sample by sample, and any slip in frame alignment, carry-over or loop handling shows. */
static int plateau(u32 k) { return (int)((k / 8u) % 6u) - 2; }   /* -2..3, constant over 8 samples */
static s16 adpcm_expect(u32 k) { return (s16)(plateau(k) * 256); }

static void make_adpcm(ALWaveTable *w, ALADPCMBook *book, ALADPCMloop *loop, u32 off, u32 frames)
{
    for (u32 f = 0; f < frames; f++) {
        put8(ROM_OFF + off + f * 9, 8u << 4);   /* scale 8, predictor 0 */
        for (u32 j = 0; j < 8; j++) {
            u32 k = f * 16 + 2 * j;
            u8 hi = (u8)plateau(k) & 0xf, lo = (u8)plateau(k + 1) & 0xf;
            put8(ROM_OFF + off + f * 9 + 1 + j, (u8)((hi << 4) | lo));
        }
    }
    memset(w, 0, sizeof *w);
    w->base = (u8 *)(uintptr_t)off;
    w->len = (s32)(frames * 9);
    w->type = AL_ADPCM_WAVE;
    w->waveInfo.adpcm.book = book;
    w->waveInfo.adpcm.loop = loop;
}

static void test_adpcm_stream(void)
{
    start_synth(2);
    ALSndPlayer sp;
    ALSndpConfig sc = {.maxSounds = 2, .maxEvents = 8, .heap = &heap};
    alSndpNew(&sp, &sc);
    /* the book and the loop record live in the ABI's RAM: the commands carry their addresses */
    ALADPCMBook *book = (ALADPCMBook *)(ram + BOOK_OFF);
    memset(ram + BOOK_OFF, 0, 256);
    book->order = 2;
    book->npredictors = 1;
    ALADPCMloop *lp = (ALADPCMloop *)(ram + LOOP_OFF);
    memset(lp, 0, sizeof *lp);
    ALADPCMloop *loop = lp;
    ALWaveTable w;
    make_adpcm(&w, book, NULL, 0x8000, 40);     /* 640 samples, no loop */
    ALSound snd = {.wavetable = &w, .samplePan = 0, .sampleVolume = 127};   /* hard left */
    s16 id = alSndpAllocate(&sp, &snd);
    alSndpSetSound(&sp, id);
    alSndpPlay(&sp);
    N64aAbi *abi = n64a_abi_create(N64A_FLAVOUR_AUDIO);
    s16 out[1024];
    for (int f = 0; f < 4; f++) {
        run_frame(abi, 128);
        for (int i = 0; i < 128; i++) out[f * 128 + i] = get16(OUT_OFF + 4u * (u32)i);
    }
    /* steady state (after the volume ramp of the first chunk): the 4-tap resampler at phase 0 weights the
     * four stream samples before the output index, so inside a plateau the output equals the decoded value */
    int checked = 0, good = 0;
    for (int i = 200; i < 480; i++) {
        if ((i - 4) % 8 > 4) continue;
        checked++;
        if (abs(out[i] - adpcm_expect((u32)(i - 4))) <= 3) good++;
    }
    CHECK(checked > 150 && good == checked);
    /* the one-shot ends at sample 640: the sound stops itself */
    for (int f = 0; f < 3; f++) run_frame(abi, 128);
    CHECK(alSndpGetState(&sp) == AL_STOPPED);
    n64a_abi_destroy(abi);

    /* the same wave looping over [32, 96): it never ends and the stream stays on the pattern */
    ALWaveTable lw;
    loop->start = 32;
    loop->end = 96;
    loop->count = 0xFFFFFFFFu;
    make_adpcm(&lw, book, loop, 0x8000, 40);
    ALSound lsnd = {.wavetable = &lw, .samplePan = 0, .sampleVolume = 127};
    s16 lid = alSndpAllocate(&sp, &lsnd);
    alSndpSetSound(&sp, lid);
    alSndpPlay(&sp);
    abi = n64a_abi_create(N64A_FLAVOUR_AUDIO);
    for (int f = 0; f < 12; f++) run_frame(abi, 128);
    CHECK(alSndpGetState(&sp) == AL_PLAYING);
    CHECK(stereo_peak(128, 0) > 300);
    n64a_abi_destroy(abi);
    alClose(&globals);
}

static void test_voice_priority(void)
{
    start_synth(2);
    N64cSynth *syn = n64c_syn_global();
    int tag[4];
    N64cVoice *a = n64c_voice_alloc(syn, &tag[0], 10);
    N64cVoice *b = n64c_voice_alloc(syn, &tag[1], 20);
    CHECK(a && b && a != b);
    CHECK(n64c_voice_alloc(syn, &tag[2], 5) == NULL);        /* all taken by higher priority */
    N64cVoice *c = n64c_voice_alloc(syn, &tag[3], 15);        /* steals the lowest (10) */
    CHECK(c == a);
    CHECK(!n64c_voice_valid(a, &tag[0]));
    CHECK(n64c_voice_valid(c, &tag[3]) && n64c_voice_valid(b, &tag[1]));
    n64c_voice_free(b);
    CHECK(!n64c_voice_valid(b, &tag[1]));
    alClose(&globals);
}

/* The game's reverb table (six delay lines, the last with a low-pass), as the oracle's RAM holds it. */
static const s32 fx_table[2 + 6 * 8] = {
    6, 4800,
    0xa0, 0x3b0, 0x2666, -0x2666, 0xaff, 0, 0, 0x3fff,
    0x1a0, 0x300, 0x3333, -0x3333, 0, 0, 0, 0,
    0x310, 0x390, 0x4ccc, -0x4ccc, 0, 0, 0, 0,
    0x680, 0xb30, 0x3333, -0x3333, 0xa9f, 0, 0, 0x3fff,
    0x7c0, 0x9c0, 0x371b, -0x371b, 0, 0, 0, 0,
    0x40, 0x11f0, 0x4bb8, 0, 0x4fff, 0, 0, 0x54ff,
};

static void start_fx_synth(const s32 *params)
{
    alHeapInit(&heap, ram + HEAP_OFF, HEAP_LEN);
    ALSynConfig c = {.maxVVoices = 2, .maxPVoices = 2, .maxUpdates = 0x100, .dmaproc = rom_dma_new,
                     .heap = &heap, .outputRate = 22050, .fxType = 6, .params = (void *)params};
    alInit(&globals, &c);
}

/* After the voice is gone, a wet send leaves a tail only when the network exists; the tail dies out. */
static long fx_tail_energy(const s32 *params)
{
    memset(ram, 0, HEAP_OFF);
    start_fx_synth(params);
    ALSndPlayer sp;
    ALSndpConfig sc = {.maxSounds = 1, .maxEvents = 8, .heap = &heap};
    alSndpNew(&sp, &sc);
    ALWaveTable w;
    make_raw(&w, 0x1000, 600, 8000);              /* a short burst, then silence */
    ALSound snd = {.wavetable = &w, .samplePan = 64, .sampleVolume = 127};
    alSndpSetSound(&sp, alSndpAllocate(&sp, &snd));
    alSndpSetFXMix(&sp, 127);
    alSndpPlay(&sp);
    N64aAbi *abi = n64a_abi_create(N64A_FLAVOUR_AUDIO);
    long tail = 0;
    for (int f = 0; f < 40; f++) {
        run_frame(abi, 256);
        if (f >= 4) for (int i = 0; i < 256; i++) tail += labs(get16(OUT_OFF + 4u * (u32)i));
    }
    CHECK(alSndpGetState(&sp) == AL_STOPPED);
    /* far later (more than the ring length) the network has decayed to a small fraction */
    long late = 0;
    for (int f = 0; f < 60; f++) {
        run_frame(abi, 256);
        if (f >= 50) for (int i = 0; i < 256; i++) late += labs(get16(OUT_OFF + 4u * (u32)i));
    }
    CHECK(late <= tail / 2 + 1);
    n64a_abi_destroy(abi);
    alClose(&globals);
    return tail;
}

static void test_reverb(void)
{
    long dry = fx_tail_energy(NULL);
    long wet = fx_tail_energy(fx_table);
    CHECK(dry == 0);
    CHECK(wet > 20000);
}

/* The game writes one byte at +0x1c and leaves the following padding alone. */
static void test_fx_config_padding(void)
{
    _Static_assert(sizeof(((ALSynConfig *)0)->fxType) == 1, "fxType is a byte");
#if UINTPTR_MAX == UINT32_MAX
    _Static_assert(offsetof(ALSynConfig, fxType) == 0x1c, "game fxType offset");
    _Static_assert(offsetof(ALSynConfig, params) == 0x20, "game effects pointer");
#endif
    Acmd expected[2048], actual[2048];
    s32 expected_len = 0;
    size_t expected_used = 0;
    const u8 patterns[] = {0, 0x90, 0xff};
    for (size_t trial = 0; trial < sizeof patterns; trial++) {
        ALSynConfig c = {.maxVVoices = 2, .maxPVoices = 2, .maxUpdates = 0x100,
            .dmaproc = rom_dma_new, .heap = &heap, .outputRate = 22050,
            .fxType = 6, .params = (void *)fx_table};
        u8 *bytes = (u8 *)&c;
        for (size_t i = offsetof(ALSynConfig, fxType) + 1;
             i < offsetof(ALSynConfig, params); i++) bytes[i] = patterns[trial];
        alHeapInit(&heap, ram + HEAP_OFF, HEAP_LEN);
        alInit(&globals, &c);
        CHECK(globals.drvr != NULL);
        s32 n = 0;
        alAudioFrame(actual, &n, (s16 *)(uintptr_t)OUT_OFF, 128);
        /* A dry silent frame has five commands; the custom network adds work. */
        CHECK(n > 5 && n <= 2048);
        size_t used = (size_t)(heap.cur - heap.base);
        if (trial == 0) {
            expected_len = n;
            expected_used = used;
            memcpy(expected, actual, (size_t)n * sizeof *actual);
        } else {
            CHECK(n == expected_len);
            CHECK(used == expected_used);
            CHECK(memcmp(expected, actual, (size_t)n * sizeof *actual) == 0);
        }
        alClose(&globals);
    }
}

/* Compact sequence: one track with a tempo, a program change, a pan, then two notes (the second one beat later,
 * a back-up copy of the first's five bytes) and a track end; played through a one-instrument bank over a raw wave. */
enum { SEQ_OFF = 0x340000, BANK_OFF = 0x341000 };
static u32 put_seq(u32 at, const u8 *b, u32 n) { for (u32 i = 0; i < n; i++) put8(at + i, b[i]); return at + n; }

/* The bank's sampleRate is a label: a note plays the wavetable at its own rate, so the same song sounds the same
 * (an octave up if the field scaled the pitch) for a bank marked 22050 or 44100 over a 22050 Hz output. */
static void test_csp(u32 bank_rate)
{
    start_synth(4);
    for (u32 i = 0; i < 0x80; i++) put8(SEQ_OFF + i, 0);
    put8(SEQ_OFF + 3, 0x44);                  /* track 0 at 0x44 */
    put8(SEQ_OFF + 0x42, 0x01); put8(SEQ_OFF + 0x43, 0xe0);   /* 480 ticks per quarter */
    static const u8 trk[] = {
        0x00, 0xff, 0x51, 0x07, 0xa1, 0x20,    /* 500000 us per quarter */
        0x00, 0xc0, 0x00,                      /* program 0 */
        0x00, 0xb0, 0x0a, 0x40,                /* pan centre */
        0x00, 0x90, 0x3c, 0x7f, 0x83, 0x60,    /* middle C for 480 ticks */
        0x83, 0x60, 0xfe, 0x00, 0x07, 0x05,    /* one beat later: replay the 5 bytes 7 + 4 back (the note above) */
        0x83, 0x60, 0xff, 0x2f };
    put_seq(SEQ_OFF + 0x44, trk, sizeof trk);
    ALWaveTable w;
    make_raw(&w, 0x1000, 8000, 6000);
    ALEnvelope env = {.releaseTime = 10000, .attackVolume = 127, .decayVolume = 127};
    ALKeyMap km = {.velocityMax = 127, .keyMax = 127, .keyBase = 60};
    ALSound snd = {.envelope = &env, .keyMap = &km, .wavetable = &w, .samplePan = 64, .sampleVolume = 127};
    ALInstrument *inst = (ALInstrument *)(ram + BANK_OFF);
    memset(inst, 0, sizeof *inst);
    inst->volume = 127; inst->soundCount = 1; inst->soundArray[0] = &snd;
    ALBank *bank = (ALBank *)(ram + BANK_OFF + 0x100);
    memset(bank, 0, sizeof *bank);
    bank->instCount = 1; bank->sampleRate = (s32)bank_rate; bank->instArray[0] = inst;

    ALCSPlayer csp;
    ALSeqpConfig sc = {.maxVoices = 4, .maxEvents = 16, .maxChannels = 16, .heap = &heap};
    alCSPNew(&csp, &sc);
    ALCSeq seq;
    alCSeqNew(&seq, ram + SEQ_OFF);
    alCSPSetSeq(&csp, &seq);
    alCSPSetBank(&csp, bank);
    CHECK(alCSPGetState(&csp) == AL_STOPPED);
    alCSPSetVol(&csp, 0x7fff);
    alCSPPlay(&csp);
    CHECK(alCSPGetState(&csp) == AL_PLAYING);
    N64aAbi *abi = n64a_abi_create(N64A_FLAVOUR_AUDIO);
    int peak_first = 0, peak_gap = 0, peak_second = 0, peak_late = 0;
    /* 22050 Hz: one beat is 11025 samples; run 3.2 beats in 256-sample frames */
    for (int f = 0; f < 140; f++) {
        run_frame(abi, 256);
        int p = stereo_peak(256, 0);
        int at = f * 256;
        if (at > 2000 && at < 9000 && p > peak_first) peak_first = p;
        if (at > 5000 && at < 7000 && p > peak_late) peak_late = p; /* 8000 samples last to 8000 at unity pitch */
        if (at > 12500 && at < 13000 && p > peak_gap) peak_gap = p;
        if (at > 14000 && at < 20000 && p > peak_second) peak_second = p;
    }
    CHECK(peak_first > 1000);                  /* first note sounding */
    CHECK(peak_first > 3700 && peak_first < 4100); /* __vsVol at full levels (31503), squared, centre pan */
    CHECK(peak_late > 1000);                   /* the 8000-sample wave still sounding at 0.25 s: unity pitch */
    CHECK(peak_second > 1000);                 /* second note one beat later */
    CHECK(alCSPGetState(&csp) == AL_STOPPED);  /* track ended, notes released */
    /* stop mid-song */
    alCSPPlay(&csp);
    for (int f = 0; f < 4; f++) run_frame(abi, 256);
    alCSPStop(&csp);
    CHECK(alCSPGetState(&csp) == AL_STOPPED);
    n64a_abi_destroy(abi);
    alClose(&globals);
    (void)peak_gap;
}

/* libultra counts a loop down in the sequence data: FF 2D count left u32-back; left 1 -> 0 on the jump, reset to
 * the count when the loop ends. */
static void test_csp_loop(void)
{
    start_synth(4);
    for (u32 i = 0; i < 0x80; i++) put8(SEQ_OFF + i, 0);
    put8(SEQ_OFF + 3, 0x44);
    put8(SEQ_OFF + 0x42, 0x01); put8(SEQ_OFF + 0x43, 0xe0);
    static const u8 trk[] = {
        0x00, 0xff, 0x51, 0x07, 0xa1, 0x20,
        0x00, 0xc0, 0x00,
        0x00, 0xff, 0x2e, 0x00, 0x00,          /* loop start */
        0x00, 0x90, 0x3c, 0x7f, 0x10,          /* +14: a 16-tick note */
        0x10, 0xff, 0x2d, 0x01, 0x01, 0x00, 0x00, 0x00, 0x0e,  /* loop end at +20, left at +23, back to +14 */
        0x00, 0xff, 0x2f };
    put_seq(SEQ_OFF + 0x44, trk, sizeof trk);
    ALWaveTable w;
    make_raw(&w, 0x1000, 8000, 6000);
    ALEnvelope env = {.releaseTime = 1000, .attackVolume = 127, .decayVolume = 127};
    ALKeyMap km = {.velocityMax = 127, .keyMax = 127, .keyBase = 60};
    ALSound snd = {.envelope = &env, .keyMap = &km, .wavetable = &w, .samplePan = 64, .sampleVolume = 127};
    ALInstrument *inst = (ALInstrument *)(ram + BANK_OFF);
    memset(inst, 0, sizeof *inst);
    inst->volume = 127; inst->soundCount = 1; inst->soundArray[0] = &snd;
    ALBank *bank = (ALBank *)(ram + BANK_OFF + 0x100);
    memset(bank, 0, sizeof *bank);
    bank->instCount = 1; bank->sampleRate = 22050; bank->instArray[0] = inst;
    ALCSPlayer csp;
    ALSeqpConfig sc = {.maxVoices = 4, .maxEvents = 16, .maxChannels = 16, .heap = &heap};
    alCSPNew(&csp, &sc);
    ALCSeq seq;
    alCSeqNew(&seq, ram + SEQ_OFF);
    alCSPSetSeq(&csp, &seq);
    alCSPSetBank(&csp, bank);
    alCSPSetVol(&csp, 0x7fff);
    alCSPPlay(&csp);
    N64aAbi *abi = n64a_abi_create(N64A_FLAVOUR_AUDIO);
    const u32 left = SEQ_OFF + 0x44 + 23;
    int least = 0xff;
    for (int f = 0; f < 40; f++) {
        run_frame(abi, 256);
        if (ram[left] < least) least = ram[left];
    }
    CHECK(least == 0);                         /* the jump counted the loop down in place */
    CHECK(ram[left] == 1);                     /* and the loop end reset it */
    CHECK(alCSPGetState(&csp) == AL_STOPPED);
    n64a_abi_destroy(abi);
    alClose(&globals);
}

int main(void)
{
    test_heap_and_links();
    test_events();
    test_sound_player_raw();
    test_adpcm_stream();
    test_voice_priority();
    test_reverb();
    test_fx_config_padding();
    test_csp(22050);
    test_csp(44100);
    test_csp_loop();
    printf("n64c_al: %s\n", fails ? "FAIL" : "ok");
    return fails ? 1 : 0;
}
