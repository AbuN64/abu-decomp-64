/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Sound player of the audio library subset: allocate sounds from a bank, select one, set its pitch, pan,
 * volume, effects send and priority, then play and stop it. Each allocated sound has its own state; the
 * envelope (attack, decay, release) is driven through the synth's volume ramps by timed events.
 * Calls take effect at once; the player runs on the audio thread inside alAudioFrame. */
#include "n64c_al.h"
#include <string.h>

enum { EV_DECAY = 1, EV_END };
enum { PH_NONE = 0, PH_ATTACK, PH_DECAY, PH_RELEASE };

typedef struct SndSlot {
    ALSound *sound;      /* NULL: free */
    N64cVoice *voice;
    s32 state;           /* AL_STOPPED / AL_PLAYING / AL_STOPPING */
    s32 phase;
    s32 gen;             /* bumped on every play so stale events are ignored */
    s16 vol;
    u8 pan, fxmix, priority;
    f32 pitch;
} SndSlot;

typedef struct N64cSndp {
    N64cPlayer player;   /* first: the synth hands this back to the handler */
    N64cEvtq evtq;
    SndSlot *slots;
    s32 maxSounds;
    s32 target;
} N64cSndp;

static N64cSndp *st_of(ALSndPlayer *sndp) { return sndp ? sndp->st : NULL; }

static SndSlot *slot_at(N64cSndp *sp, s32 id)
{
    return sp && id >= 0 && id < sp->maxSounds && sp->slots[id].sound ? &sp->slots[id] : NULL;
}

static int voice_live(const SndSlot *s) { return n64c_voice_valid(s->voice, s); }

/* Envelope level for the current phase, 0..127. */
static s32 env_level(const SndSlot *s)
{
    const ALEnvelope *e = s->sound->envelope;
    switch (s->phase) {
    case PH_ATTACK: return e ? e->attackVolume : 127;
    case PH_DECAY: return e ? e->decayVolume : 127;
    default: return 0;
    }
}

static s16 level_volume(const SndSlot *s, s32 level)
{
    s32 v = (s32)(s->vol < 0 ? 0 : s->vol);
    v = v * level / 127;   /* libultra sndplayer: envelope level x player volume; the sample volume is not applied */
    return (s16)(v > 0x7fff ? 0x7fff : v);
}

static void release_voice(SndSlot *s)
{
    if (voice_live(s)) n64c_voice_free(s->voice);
    s->voice = NULL;
}

static void stop_now(SndSlot *s)
{
    release_voice(s);
    s->state = AL_STOPPED;
    s->phase = PH_NONE;
    s->gen++;
}

static void handle_event(N64cSndp *sp, const N64cEvt *e)
{
    SndSlot *s = slot_at(sp, e->a);
    if (!s || s->gen != e->b) return;
    if (e->type == EV_DECAY && s->phase == PH_ATTACK) {
        const ALEnvelope *en = s->sound->envelope;
        s->phase = PH_DECAY;
        if (voice_live(s)) n64c_voice_set_vol(s->voice, level_volume(s, env_level(s)), en ? en->decayTime : 0);
    } else if (e->type == EV_END && s->phase == PH_RELEASE) {
        stop_now(s);
    }
}

static s32 sndp_handler(N64cPlayer *self, s32 elapsedMicros)
{
    N64cSndp *sp = (N64cSndp *)self;
    n64c_evtq_advance(&sp->evtq, elapsedMicros);
    N64cEvt e;
    while (n64c_evtq_pop(&sp->evtq, &e)) handle_event(sp, &e);
    int active = 0;
    for (s32 i = 0; i < sp->maxSounds; i++) {
        SndSlot *s = &sp->slots[i];
        if (!s->sound || s->state == AL_STOPPED) continue;
        if (!voice_live(s) || !n64c_voice_playing(s->voice)) { stop_now(s); continue; }
        active = 1;
    }
    s32 next = n64c_evtq_next_delta(&sp->evtq);
    s32 chunk = n64c_syn_chunk_micros(self->synth);
    if (active) return next >= 0 && next < chunk ? next : chunk;
    return next >= 0 ? next : 20000;
}

void alSndpNew(ALSndPlayer *sndp, ALSndpConfig *c)
{
    sndp->st = NULL;
    N64cSynth *syn = n64c_syn_global();
    if (!syn || !c || !c->heap || c->maxSounds <= 0) return;
    N64cSndp *sp = alHeapDBAlloc(NULL, 0, c->heap, 1, (s32)sizeof *sp);
    if (!sp) return;
    sp->slots = alHeapDBAlloc(NULL, 0, c->heap, c->maxSounds, (s32)sizeof(SndSlot));
    if (!sp->slots || n64c_evtq_init(&sp->evtq, c->heap, c->maxEvents > 0 ? c->maxEvents : 1) != 0) return;
    sp->maxSounds = c->maxSounds;
    sp->target = 0;
    sp->player.handler = sndp_handler;
    sndp->st = sp;
    n64c_syn_add_player(syn, &sp->player);
}

s16 alSndpAllocate(ALSndPlayer *sndp, ALSound *sound)
{
    N64cSndp *sp = st_of(sndp);
    if (!sp || !sound) return -1;
    for (s32 i = 0; i < sp->maxSounds; i++) {
        SndSlot *s = &sp->slots[i];
        if (s->sound) continue;
        memset(s, 0, sizeof *s);
        s->sound = sound;
        s->vol = 0x7fff;
        s->pitch = 1.0f;
        s->pan = sound->samplePan > 127 ? 127 : sound->samplePan;
        s->state = AL_STOPPED;
        return (s16)i;
    }
    return -1;
}

void alSndpDeallocate(ALSndPlayer *sndp, s16 id)
{
    N64cSndp *sp = st_of(sndp);
    SndSlot *s = slot_at(sp, id);
    if (!s) return;
    stop_now(s);
    s->sound = NULL;
}

void alSndpSetSound(ALSndPlayer *sndp, s16 id)
{
    N64cSndp *sp = st_of(sndp);
    if (sp && id >= 0 && id < sp->maxSounds) sp->target = id;
}

s32 alSndpGetState(ALSndPlayer *sndp)
{
    N64cSndp *sp = st_of(sndp);
    SndSlot *s = slot_at(sp, sp ? sp->target : -1);
    return s ? s->state : AL_STOPPED;
}

void alSndpPlay(ALSndPlayer *sndp)
{
    N64cSndp *sp = st_of(sndp);
    SndSlot *s = slot_at(sp, sp ? sp->target : -1);
    if (!s || s->state == AL_PLAYING) return;
    if (s->state == AL_STOPPING) stop_now(s);
    ALWaveTable *wave = s->sound->wavetable;
    N64cVoice *v = wave ? n64c_voice_alloc(sp->player.synth, s, s->priority) : NULL;
    if (!v) return;
    const ALEnvelope *en = s->sound->envelope;
    s->voice = v;
    s->gen++;
    s->phase = PH_ATTACK;
    s->state = AL_PLAYING;
    n64c_voice_start(v, wave);
    n64c_voice_set_pitch(v, s->pitch);
    n64c_voice_set_pan(v, s->pan);
    n64c_voice_set_fxmix(v, s->fxmix);
    n64c_voice_set_vol(v, 0, 0);
    n64c_voice_set_vol(v, level_volume(s, env_level(s)), en ? en->attackTime : 0);
    N64cEvt e = {.type = EV_DECAY, .a = (s32)(s - sp->slots), .b = s->gen};
    n64c_evtq_post(&sp->evtq, &e, en ? en->attackTime : 0);
}

void alSndpStop(ALSndPlayer *sndp)
{
    N64cSndp *sp = st_of(sndp);
    SndSlot *s = slot_at(sp, sp ? sp->target : -1);
    if (!s || s->state != AL_PLAYING) return;
    if (!voice_live(s)) { stop_now(s); return; }
    const ALEnvelope *en = s->sound->envelope;
    s->phase = PH_RELEASE;
    s->state = AL_STOPPING;
    n64c_voice_set_vol(s->voice, 0, en ? en->releaseTime : 0);
    N64cEvt e = {.type = EV_END, .a = (s32)(s - sp->slots), .b = s->gen};
    n64c_evtq_post(&sp->evtq, &e, en ? en->releaseTime : 0);
}

static void refresh_volume(N64cSndp *sp, SndSlot *s)
{
    if (voice_live(s) && s->phase != PH_RELEASE)
        n64c_voice_set_vol(s->voice, level_volume(s, env_level(s)), n64c_syn_chunk_micros(sp->player.synth));
}

void alSndpSetVol(ALSndPlayer *sndp, s16 vol)
{
    N64cSndp *sp = st_of(sndp);
    SndSlot *s = slot_at(sp, sp ? sp->target : -1);
    if (!s) return;
    s->vol = vol < 0 ? 0 : vol;
    refresh_volume(sp, s);
}

void alSndpSetPitch(ALSndPlayer *sndp, f32 pitch)
{
    N64cSndp *sp = st_of(sndp);
    SndSlot *s = slot_at(sp, sp ? sp->target : -1);
    if (!s) return;
    s->pitch = pitch;
    if (voice_live(s)) n64c_voice_set_pitch(s->voice, pitch);
}

void alSndpSetPan(ALSndPlayer *sndp, u8 pan)
{
    N64cSndp *sp = st_of(sndp);
    SndSlot *s = slot_at(sp, sp ? sp->target : -1);
    if (!s) return;
    s->pan = pan > 127 ? 127 : pan;
    if (voice_live(s)) n64c_voice_set_pan(s->voice, s->pan);
}

void alSndpSetPriority(ALSndPlayer *sndp, s16 id, u8 priority)
{
    N64cSndp *sp = st_of(sndp);
    SndSlot *s = slot_at(sp, id);
    if (!s) return;
    s->priority = priority;
    if (voice_live(s)) n64c_voice_set_priority(s->voice, priority);
}

void alSndpSetFXMix(ALSndPlayer *sndp, u8 mix)
{
    N64cSndp *sp = st_of(sndp);
    SndSlot *s = slot_at(sp, sp ? sp->target : -1);
    if (!s) return;
    s->fxmix = mix > 127 ? 127 : mix;
    if (voice_live(s)) n64c_voice_set_fxmix(s->voice, s->fxmix);
}
