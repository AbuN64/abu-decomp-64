/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Compact sequence player of the audio library subset: bind a sequence and a bank, set channel programs
 * and the master volume, play and stop. Calls take effect at once; the player runs on the audio thread
 * inside alAudioFrame.
 *
 * Sequence layout (derived from the game's own sequence data, see LINEAGE.md): 16 big-endian u32 track
 * offsets from the start of the data (0 = unused), then a u32 ticks-per-quarter-note at +0x40. A track is a
 * stream of events, each a variable-length delta (7 bits per byte, high bit continues) followed by
 *   a status byte (high bit set; a data byte there repeats the previous channel status), then
 *   8x note off / Ax pressure / Bx controller / Ex bend: two data bytes, Cx program / Dx pressure: one,
 *   9x note on: note, velocity, then the note's length in ticks as a variable-length number;
 *   FF 51 + 3 bytes tempo (microseconds per quarter note), FF 2F track end,
 *   FF 2E + 2 bytes loop start, FF 2D + count, 1 byte, u32 distance back (measured from the end of this
 *   event) to the first event after the loop start; count 0xFF repeats forever.
 *   FE + 2 bytes is carried but has no effect here. */
#include "n64c_al.h"
#include <math.h>
#include <string.h>

enum { CSP_CHANNELS = 16, CSP_TRACKS = 16, SEQ_HEADER = 0x44 };

/* The sequence is a byte stream the game DMAs from the cartridge raw (the PI layer leaves byte data as it is), so
 * byte i is at base + i. */
static u8 rd8(const u8 *base, u32 off) { return base[off]; }
static u32 rd32be(const u8 *base, u32 off)
{
    return (u32)rd8(base, off) << 24 | (u32)rd8(base, off + 1) << 16 | (u32)rd8(base, off + 2) << 8 | rd8(base, off + 3);
}

typedef struct CspTrack {
    u32 pos;
    u32 nextTick;
    u32 loopsLeft;     /* 0: not counting */
    u8 run;            /* running status */
    u8 done;
} CspTrack;

typedef struct CspNote {
    N64cVoice *voice;
    u32 offTick;
    s32 releaseLeft;   /* micros of release remaining, 0: not releasing */
    ALSound *snd;
    u8 active, chan;
} CspNote;

typedef struct N64cCsp {
    N64cPlayer player;
    N64cEvtq evtq;
    s32 maxVoices;
    s32 maxChannels;
    s32 state;
    s16 vol;
    ALCSeq *seq;
    ALBank *bank;
    u8 program[CSP_CHANNELS];
    u8 chanVol[CSP_CHANNELS], chanPan[CSP_CHANNELS], chanFx[CSP_CHANNELS];
    CspTrack track[CSP_TRACKS];
    CspNote *notes;
    u32 tick;
    u64 tickNum;       /* micros * division not yet turned into ticks */
    u32 tempo;         /* micros per quarter note */
    u32 division;
} N64cCsp;

static u32 rd_varlen(const u8 *d, u32 *pos)
{
    u32 v = 0;
    for (int i = 0; i < 5; i++) {
        u8 c = rd8(d, (*pos)++);
        v = (v << 7) | (c & 0x7fu);
        if (!(c & 0x80)) break;
    }
    return v;
}

static void release_note(N64cCsp *cp, CspNote *n, ALSound *snd)
{
    (void)cp;
    s32 rel = snd && snd->envelope && snd->envelope->releaseTime > 0 ? snd->envelope->releaseTime : 1000;
    n64c_voice_set_vol(n->voice, 0, rel);
    n->releaseLeft = rel + 20000;
}

static void kill_notes(N64cCsp *cp)
{
    for (s32 i = 0; i < cp->maxVoices; i++) {
        CspNote *n = &cp->notes[i];
        if (n->active) n64c_voice_free(n->voice);
        n->active = 0;
    }
}

static const ALSound *pick_sound(const ALInstrument *in, int key, int vel)
{
    for (s32 i = 0; i < in->soundCount; i++) {
        const ALSound *s = in->soundArray[i];
        const ALKeyMap *k = s ? s->keyMap : NULL;
        if (!k || (key >= k->keyMin && key <= k->keyMax && vel >= k->velocityMin && vel <= k->velocityMax)) return s;
    }
    return in->soundCount > 0 ? in->soundArray[0] : NULL;
}

static void note_on(N64cCsp *cp, int chan, int key, int vel, u32 durTicks)
{
    if (vel == 0 || !cp->bank) return;
    int prog = cp->program[chan];
    if (prog >= cp->bank->instCount) return;
    const ALInstrument *in = cp->bank->instArray[prog];
    if (!in) return;
    const ALSound *snd = pick_sound(in, key, vel);
    if (!snd || !snd->wavetable) return;
    CspNote *slot = NULL;
    for (s32 i = 0; i < cp->maxVoices; i++) if (!cp->notes[i].active) { slot = &cp->notes[i]; break; }
    if (!slot) return;                      /* every note slot sounding: the new note is dropped */
    N64cSynth *syn = cp->player.synth;
    N64cVoice *v = n64c_voice_alloc(syn, slot, in->priority);
    if (!v) return;
    f32 rate = cp->bank->sampleRate > 0 ? (f32)cp->bank->sampleRate / (f32)n64c_syn_output_rate(syn) : 1.0f;
    const ALKeyMap *k = snd->keyMap;
    f32 semis = (f32)(key - (k ? k->keyBase : 60)) + (k ? (f32)k->detune / 100.0f : 0.0f);
    n64c_voice_start(v, snd->wavetable);
    n64c_voice_set_pitch(v, rate * powf(2.0f, semis / 12.0f));
    f32 g = (f32)vel / 127.0f * (f32)cp->chanVol[chan] / 127.0f * (f32)snd->sampleVolume / 127.0f *
            (f32)in->volume / 127.0f * (f32)cp->vol / 32767.0f;
    s32 tgt = (s32)(32767.0f * g);
    s32 pan = (s32)cp->chanPan[chan] + (s32)snd->samplePan - 64;
    n64c_voice_set_pan(v, (u8)(pan < 0 ? 0 : pan > 127 ? 127 : pan));
    n64c_voice_set_fxmix(v, cp->chanFx[chan]);
    s32 atk = snd->envelope ? snd->envelope->attackTime : 0;
    if (atk > 0) { n64c_voice_set_vol(v, 0, 0); n64c_voice_set_vol(v, (s16)tgt, atk); }
    else n64c_voice_set_vol(v, (s16)tgt, 0);
    slot->voice = v;
    slot->snd = (ALSound *)snd;
    slot->active = 1;
    slot->chan = (u8)chan;
    slot->releaseLeft = 0;
    slot->offTick = cp->tick + durTicks;
}

/* Executes one event of track t at its current position. */
static void run_event(N64cCsp *cp, CspTrack *t)
{
    const u8 *d = cp->seq->data;
    u8 s = rd8(d, t->pos);
    if (s & 0x80) { t->pos++; if (s != 0xff) t->run = s; }
    else s = t->run;
    const int hi = s & 0xf0, ch = s & 15;
    if (s == 0xff) {
        u8 m = rd8(d, t->pos++);
        if (m == 0x51) {
            cp->tempo = (u32)rd8(d, t->pos) << 16 | (u32)rd8(d, t->pos + 1) << 8 | rd8(d, t->pos + 2);
            if (cp->tempo == 0) cp->tempo = 500000;
            t->pos += 3;
        } else if (m == 0x2f) {
            t->done = 1;
            return;
        } else if (m == 0x2e) {
            t->pos += 2;
            t->loopsLeft = 0;
        } else if (m == 0x2d) {
            u8 cnt = rd8(d, t->pos);
            u32 back = rd32be(d, t->pos + 2);
            t->pos += 6;
            if (back <= t->pos) {
                if (cnt == 0xff) t->pos -= back;
                else {
                    if (t->loopsLeft == 0) t->loopsLeft = (u32)cnt + 1;
                    if (--t->loopsLeft > 0) t->pos -= back;
                }
            }
        } else t->done = 1;                 /* unknown meta: stop reading this track */
        return;
    }
    if (s == 0xfe) { t->pos += 2; return; }
    switch (hi) {
    case 0x90: {
        u8 note = rd8(d, t->pos), vel = rd8(d, t->pos + 1);
        t->pos += 2;
        u32 dur = rd_varlen(d, &t->pos);
        note_on(cp, ch, note, vel, dur);
        break;
    }
    case 0xb0: {
        u8 c = rd8(d, t->pos), v = rd8(d, t->pos + 1);
        t->pos += 2;
        if (c == 7) cp->chanVol[ch] = v;
        else if (c == 10) cp->chanPan[ch] = v;
        else if (c == 91) cp->chanFx[ch] = v;
        break;
    }
    case 0xc0: cp->program[ch] = rd8(d, t->pos++); break;
    case 0xd0: t->pos += 1; break;
    case 0x80: case 0xa0: case 0xe0: t->pos += 2; break;
    default: t->done = 1; break;           /* data byte with no status to repeat */
    }
}

static int track_read_delta(N64cCsp *cp, CspTrack *t)
{
    u32 p = t->pos;
    u32 dl = rd_varlen(cp->seq->data, &p);
    t->pos = p;
    t->nextTick += dl;
    return 0;
}

static void play_init(N64cCsp *cp)
{
    const u8 *d = cp->seq->data;
    kill_notes(cp);
    cp->tick = 0;
    cp->tickNum = 0;
    cp->tempo = 500000;
    cp->division = rd32be(d, 0x40);
    if (cp->division == 0) cp->division = 96;
    for (int i = 0; i < CSP_TRACKS; i++) {
        CspTrack *t = &cp->track[i];
        memset(t, 0, sizeof *t);
        u32 off = rd32be(d, (u32)i * 4u);
        if (off < SEQ_HEADER) { t->done = 1; continue; }
        t->pos = off;
        track_read_delta(cp, t);
    }
    for (int i = 0; i < CSP_CHANNELS; i++) { cp->chanVol[i] = 127; cp->chanPan[i] = 64; cp->chanFx[i] = 0; }
}

static s32 csp_handler(N64cPlayer *self, s32 elapsedMicros)
{
    N64cCsp *cp = (N64cCsp *)self;
    n64c_evtq_advance(&cp->evtq, elapsedMicros);
    N64cEvt e;
    while (n64c_evtq_pop(&cp->evtq, &e)) {}
    if (cp->state != AL_PLAYING) return 20000;
    cp->tickNum += (u64)(elapsedMicros > 0 ? elapsedMicros : 0) * cp->division;
    u32 adv = (u32)(cp->tickNum / cp->tempo);
    cp->tickNum %= cp->tempo;
    /* walk the ticks event by event so a tempo change applies from the tick it sits on */
    for (;;) {
        u32 soon = 0xffffffffu;
        int live = 0;
        for (int i = 0; i < CSP_TRACKS; i++) if (!cp->track[i].done) { live = 1; if (cp->track[i].nextTick < soon) soon = cp->track[i].nextTick; }
        if (!live || soon > cp->tick + adv) break;
        u32 step = soon > cp->tick ? soon - cp->tick : 0;
        adv -= step;
        cp->tick += step;
        for (int i = 0; i < CSP_TRACKS; i++) {
            CspTrack *t = &cp->track[i];
            int guard = 0;
            while (!t->done && t->nextTick <= cp->tick && guard++ < 4096) {
                run_event(cp, t);
                if (!t->done) track_read_delta(cp, t);
            }
        }
    }
    cp->tick += adv;
    int sounding = 0;
    for (s32 i = 0; i < cp->maxVoices; i++) {
        CspNote *n = &cp->notes[i];
        if (!n->active) continue;
        if (!n64c_voice_valid(n->voice, n) || !n64c_voice_playing(n->voice)) { n->active = 0; continue; }
        if (n->releaseLeft > 0) {
            n->releaseLeft -= elapsedMicros;
            if (n->releaseLeft <= 0) { n64c_voice_free(n->voice); n->active = 0; continue; }
        } else if (cp->tick >= n->offTick) {
            release_note(cp, n, n->snd);
        }
        sounding++;
    }
    int live = 0;
    for (int i = 0; i < CSP_TRACKS; i++) if (!cp->track[i].done) live = 1;
    if (!live && !sounding) cp->state = AL_STOPPED;
    return 5000;
}

void alCSPNew(ALCSPlayer *csp, ALSeqpConfig *c)
{
    csp->st = NULL;
    N64cSynth *syn = n64c_syn_global();
    if (!syn || !c || !c->heap) return;
    N64cCsp *cp = alHeapDBAlloc(NULL, 0, c->heap, 1, (s32)sizeof *cp);
    if (!cp || n64c_evtq_init(&cp->evtq, c->heap, c->maxEvents > 0 ? c->maxEvents : 1) != 0) return;
    cp->maxVoices = c->maxVoices > 0 ? c->maxVoices : 1;
    cp->notes = alHeapDBAlloc(NULL, 0, c->heap, cp->maxVoices, (s32)sizeof(CspNote));
    if (!cp->notes) return;
    cp->maxChannels = c->maxChannels;
    cp->vol = 0x7fff;
    cp->state = AL_STOPPED;
    cp->tempo = 500000;
    cp->division = 96;
    for (int i = 0; i < CSP_CHANNELS; i++) { cp->chanVol[i] = 127; cp->chanPan[i] = 64; }
    cp->player.handler = csp_handler;
    csp->st = cp;
    n64c_syn_add_player(syn, &cp->player);
}

void alCSeqNew(ALCSeq *seq, u8 *data)
{
    memset(seq, 0, sizeof *seq);
    seq->data = data;
}

void alCSPSetSeq(ALCSPlayer *csp, ALCSeq *seq)
{
    if (csp->st) csp->st->seq = seq;
}

void alCSPSetBank(ALCSPlayer *csp, void *bank)
{
    if (csp->st) csp->st->bank = bank;
}

void alCSPSetChlProgram(ALCSPlayer *csp, u8 chan, u8 program)
{
    if (csp->st && chan < CSP_CHANNELS) csp->st->program[chan] = program;
}

void alCSPSetVol(ALCSPlayer *csp, s16 vol)
{
    if (csp->st) csp->st->vol = vol < 0 ? 0 : vol;
}

void alCSPPlay(ALCSPlayer *csp)
{
    N64cCsp *cp = csp->st;
    if (!cp || !cp->seq || !cp->seq->data || !cp->bank) return;
    play_init(cp);
    cp->state = AL_PLAYING;
}

void alCSPStop(ALCSPlayer *csp)
{
    if (!csp->st) return;
    kill_notes(csp->st);
    csp->st->state = AL_STOPPED;
}

s32 alCSPGetState(ALCSPlayer *csp)
{
    return csp->st ? csp->st->state : AL_STOPPED;
}
