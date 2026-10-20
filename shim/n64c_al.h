/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Audio library surface of the shim (heap, synthesizer driver, sound player, sequence player).
 * Written from the public description of the N64 audio library API (names, signatures, documented
 * behaviour, bank file layout) and from how the game calls it; no vendor source. Pointers are native,
 * so a -m32 build keeps the sizes the game reserves. Only platform/n64compat may include this. */
#ifndef N64C_AL_H
#define N64C_AL_H
#include "n64c_os.h"
#ifdef __cplusplus
extern "C" {
#endif

/* Doubly linked list node, embedded at the start of the objects it links. */
typedef struct ALLink {
    struct ALLink *next;
    struct ALLink *prev;
} ALLink;

/* Bump allocator over a caller-supplied block. */
typedef struct ALHeap {
    u8 *base;
    u8 *cur;
    s32 len;
    s32 count;
} ALHeap;

void alHeapInit(ALHeap *hp, u8 *base, s32 len);
/* num * size bytes, zeroed, 16-byte aligned; NULL when the block is exhausted. file/line are diagnostics. */
void *alHeapDBAlloc(u8 *file, s32 line, ALHeap *hp, s32 num, s32 size);
void alLink(ALLink *ln, ALLink *to);   /* insert ln after to */
void alUnlink(ALLink *ln);

/* ---- bank file layout (public ctl format, native pointers once relocated) ---- */
typedef struct ALEnvelope {
    s32 attackTime;   /* microseconds */
    s32 decayTime;
    s32 releaseTime;
    u8 attackVolume;  /* 0..127 */
    u8 decayVolume;
} ALEnvelope;

typedef struct ALKeyMap {
    u8 velocityMin, velocityMax, keyMin, keyMax, keyBase;
    s8 detune;
} ALKeyMap;

typedef struct ALADPCMloop {
    u32 start, end, count;
    s16 state[16];
} ALADPCMloop;

typedef struct ALADPCMBook {
    s32 order;
    s32 npredictors;
    s16 book[1];      /* order * npredictors * 8 halfwords */
} ALADPCMBook;

typedef struct ALRawLoop {
    u32 start, end, count;
} ALRawLoop;

enum { AL_ADPCM_WAVE = 0, AL_RAW16_WAVE = 1 };

typedef struct ALWaveTable {
    u8 *base;         /* ROM address of the sample data (what the DMA callback is asked for) */
    s32 len;          /* bytes */
    u8 type;
    u8 flags;
    union {
        struct { ALADPCMloop *loop; ALADPCMBook *book; } adpcm;
        struct { ALRawLoop *loop; } raw;
    } waveInfo;
} ALWaveTable;

typedef struct ALSound_s {
    ALEnvelope *envelope;
    ALKeyMap *keyMap;
    ALWaveTable *wavetable;
    u8 samplePan;
    u8 sampleVolume;
    u8 flags;
} ALSound;

typedef struct ALInstrument {
    u8 volume, pan, priority, flags;
    u8 tremType, tremRate, tremDepth, tremDelay;
    u8 vibType, vibRate, vibDepth, vibDelay;
    s16 bendRange;
    s16 soundCount;
    ALSound *soundArray[1];
} ALInstrument;

typedef struct ALBank {
    s16 instCount;
    u8 flags, pad;
    s32 sampleRate;
    ALInstrument *percussion;
    ALInstrument *instArray[1];
} ALBank;

typedef struct ALBankFile {
    s16 revision;
    s16 bankCount;
    ALBank *bankArray[1];
} ALBankFile;
/* Bank reader. The control bank is read as it sits in the ROM, big-endian whatever the host, and rebuilt as native
 * structures in the heap (offsets inside the bank, 0 = none, the high flag bits of an offset ignored). `ctl` is the
 * start of the container that holds the bank files, `fileOffset` the byte offset of one ALBankFile in it (revision,
 * bank count, bank offsets). Sample data offsets become `sampleBase + offset`. Returns NULL when the heap is
 * exhausted or any offset or count leaves [0, len). */
ALBankFile *n64c_al_bnkf_read(ALHeap *heap, const u8 *ctl, s32 len, u32 fileOffset, u32 sampleBase);
/* The container header: a count, then per file an offset (flag bits included) and one auxiliary word. Returns the
 * count (0 when malformed) and stores the n-th file's byte offset in *fileOffset (may be NULL). */
s32 n64c_al_ctl_file(const u8 *ctl, s32 len, s32 n, u32 *fileOffset);
/* Byte-order map of a whole container used in place (the PI layer turns its fields to host order on DMA): widths[i]
 * is 2 or 4 where a multibyte field of a header, bank file, bank, instrument, sound, envelope, wave table, ADPCM loop,
 * ADPCM book or raw loop starts, else 0. Every record reachable from the header is visited once. Returns 0 when the
 * container is malformed (an offset, count or field outside [0, len), misaligned, or two widths at one byte). */
int n64c_al_ctl_widths(const u8 *ctl, s32 len, u8 *widths);
/* ---- synthesizer driver ---- */
typedef s32 (*ALDMAproc)(s32 addr, s32 len, void *state);
typedef ALDMAproc (*ALDMANew)(void **state);

typedef struct ALSynConfig {
    s32 maxVVoices;
    s32 maxPVoices;
    s32 maxUpdates;
    s32 maxFXbusses;
    ALDMANew dmaproc;
    ALHeap *heap;
    s32 outputRate;
    u8 fxType;
    void *params;
} ALSynConfig;

typedef struct N64cSynth N64cSynth;
typedef struct N64cVoice N64cVoice;

/* Storage the game reserves for the global (0x4C bytes): the driver lives in the heap. */
typedef struct ALGlobals {
    N64cSynth *drvr;
    u8 reserved[0x48];
} ALGlobals;

typedef struct Acmd {
    u32 w0, w1;
} Acmd;

void alInit(ALGlobals *g, ALSynConfig *c);
void alClose(ALGlobals *g);
Acmd *alAudioFrame(Acmd *cmdList, s32 *cmdLen, s16 *outBuf, s32 outLen);

/* Internal player/voice interface shared by the sound and sequence players. */
typedef struct N64cPlayer {
    ALLink node;
    /* Called when due with the microseconds since the previous call; returns microseconds until the next. */
    s32 (*handler)(struct N64cPlayer *self, s32 elapsedMicros);
    s32 samplesLeft;
    s32 samplesSince;
    N64cSynth *synth;
} N64cPlayer;

void n64c_syn_add_player(N64cSynth *syn, N64cPlayer *p);
void n64c_syn_remove_player(N64cSynth *syn, N64cPlayer *p);
N64cSynth *n64c_syn_global(void);
s32 n64c_syn_output_rate(const N64cSynth *syn);
s32 n64c_syn_chunk_micros(const N64cSynth *syn);     /* length of one mixing chunk */
/* Voices: allocate one (priority decides stealing; tag identifies the user), then drive it.
 * Returns NULL when none can be had. A stolen or finished voice no longer matches its tag. */
N64cVoice *n64c_voice_alloc(N64cSynth *syn, void *tag, s32 priority);
int n64c_voice_valid(const N64cVoice *v, const void *tag);
void n64c_voice_start(N64cVoice *v, ALWaveTable *wave);
void n64c_voice_free(N64cVoice *v);                  /* stop and give the voice back now */
void n64c_voice_set_pitch(N64cVoice *v, f32 ratio);  /* resample ratio against the output rate */
void n64c_voice_set_vol(N64cVoice *v, s16 vol, s32 rampMicros);
void n64c_voice_set_pan(N64cVoice *v, u8 pan);
void n64c_voice_set_fxmix(N64cVoice *v, u8 mix);
int n64c_voice_playing(const N64cVoice *v);          /* started and the sample has not ended */
void n64c_voice_set_priority(N64cVoice *v, s32 priority);

/* ---- event queue shared by the players ---- */
typedef struct N64cEvt {
    ALLink node;
    s64 due;         /* absolute time on the queue clock, microseconds */
    s32 type;
    s32 a, b, c;
    void *p;
} N64cEvt;
typedef struct N64cEvtq {
    ALLink freeList;
    ALLink waiting;  /* head sentinel; ordered by due time */
    N64cEvt *pool;
    s32 count;
    s64 now;         /* queue clock, microseconds */
} N64cEvtq;
int n64c_evtq_init(N64cEvtq *q, ALHeap *heap, s32 maxEvents);
int n64c_evtq_post(N64cEvtq *q, const N64cEvt *e, s32 delta);   /* 0 on success, -1 when full */
/* Pops the head when it is due (due <= now); returns 1 and fills e. */
int n64c_evtq_pop(N64cEvtq *q, N64cEvt *e);
s32 n64c_evtq_next_delta(const N64cEvtq *q);                    /* microseconds to the head, or -1 */
void n64c_evtq_advance(N64cEvtq *q, s32 micros);                /* time passes */
void n64c_evtq_flush(N64cEvtq *q, s32 type, s32 a);             /* drop queued events with this type and a */

/* ---- sound player ---- */
typedef struct ALSndpConfig {
    s32 maxSounds;
    s32 maxEvents;
    ALHeap *heap;
} ALSndpConfig;
/* Storage the game reserves per player: 0x54 bytes; the state is in the heap. */
typedef struct ALSndPlayer {
    struct N64cSndp *st;
    u8 reserved[0x50];
} ALSndPlayer;

void alSndpNew(ALSndPlayer *sndp, ALSndpConfig *c);
s16 alSndpAllocate(ALSndPlayer *sndp, ALSound *sound);
void alSndpDeallocate(ALSndPlayer *sndp, s16 id);
void alSndpSetSound(ALSndPlayer *sndp, s16 id);
s32 alSndpGetState(ALSndPlayer *sndp);
void alSndpPlay(ALSndPlayer *sndp);
void alSndpStop(ALSndPlayer *sndp);
void alSndpSetVol(ALSndPlayer *sndp, s16 vol);
void alSndpSetPitch(ALSndPlayer *sndp, f32 pitch);
void alSndpSetPan(ALSndPlayer *sndp, u8 pan);
void alSndpSetPriority(ALSndPlayer *sndp, s16 id, u8 priority);
void alSndpSetFXMix(ALSndPlayer *sndp, u8 mix);
enum { AL_STOPPED = 0, AL_PLAYING = 1, AL_STOPPING = 2 };

/* ---- compact sequence player ---- */
/* The game reserves 0xF8 bytes per sequence object and 0x7C per player. */
typedef struct ALCSeq {
    const u8 *data;
    u8 reserved[0xF4];
} ALCSeq;
typedef struct ALCSPlayer {
    struct N64cCsp *st;
    u8 reserved[0x78];
} ALCSPlayer;
typedef struct ALSeqpConfig {
    s32 maxVoices;
    s32 maxEvents;
    u8 maxChannels;
    u8 debugFlags;
    ALHeap *heap;
    void *initOsc, *updateOsc, *stopOsc;
} ALSeqpConfig;
void alCSPNew(ALCSPlayer *csp, ALSeqpConfig *c);
void alCSeqNew(ALCSeq *seq, u8 *data);
void alCSPSetSeq(ALCSPlayer *csp, ALCSeq *seq);
void alCSPSetBank(ALCSPlayer *csp, void *bank);
void alCSPSetChlProgram(ALCSPlayer *csp, u8 chan, u8 program);
void alCSPSetVol(ALCSPlayer *csp, s16 vol);
void alCSPPlay(ALCSPlayer *csp);
void alCSPStop(ALCSPlayer *csp);
s32 alCSPGetState(ALCSPlayer *csp);

#ifdef __cplusplus
}
#endif
#endif
