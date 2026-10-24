/* SPDX-License-Identifier: GPL-3.0-or-later */
/* AI DMA submission to a native S16 stereo sink; playback is owned by SDL3.
 * Completion means copied to the host sink, not played by the physical device.
 * The two-entry DMA FIFO is modeled on the virtual clock (osAiGetStatus); the AI interrupt follows each buffer's end on it. */
#include "n64c_audio.h"
#include "n64c_internal.h"
#include "n64c_system.h"
#include <stdio.h>
#include <fenv.h>
#include <time.h>
#ifdef N64C_AUDIO_SDL3
#include <SDL3/SDL.h>
#endif

static N64aPcmSink pcm_sink;
static void *pcm_user;
static u32 sample_rate = 32000;
static OSTime fifo_end[2]; /* virtual time each held buffer finishes playing, oldest first */
static u32 fifo_count;
static void fifo_drain_to(OSTime now)
{
    while (fifo_count && fifo_end[0] <= now) {
        fifo_end[0] = fifo_end[1];
        fifo_count--;
    }
}
static void fifo_drain(void) { fifo_drain_to(osGetTime()); }
static void buffer_done(void);
#ifdef N64C_AUDIO_SDL3
/* The device opens on a helper thread: probing the audio drivers (dlopen of each backend library, a connect to a
 * sound server) can be slow or hang where there is no sound server and no card. The caller waits OPEN_WAIT_MS; a
 * device that is not up by then, or never comes up, leaves the sink silent. The guest's AI FIFO runs on the virtual
 * clock and never sees the sink, so a silent sink changes no guest timing. The outcome is latched: a failed open is
 * not retried per buffer. A device that opens late takes over from the next buffer on. */
enum { OPEN_NONE, OPEN_PENDING, OPEN_READY, OPEN_FAILED, OPEN_ABANDONED };
#define OPEN_WAIT_MS 1500
static SDL_AudioStream *stream;
static SDL_AtomicInt open_state;
static u32 stream_rate;
static int open_rate;

static void close_stream(void)
{
    int expected = OPEN_PENDING;
    if (SDL_CompareAndSwapAtomicInt(&open_state, expected, OPEN_ABANDONED)) return; /* the helper cleans up after itself */
    if (stream) SDL_DestroyAudioStream(stream);
    stream = NULL;
    if (SDL_GetAtomicInt(&open_state) == OPEN_READY) SDL_QuitSubSystem(SDL_INIT_AUDIO);
    SDL_SetAtomicInt(&open_state, OPEN_NONE);
    stream_rate = 0;
}

static int open_worker(void *data)
{
    SDL_Semaphore *done = data;
    const SDL_AudioSpec spec = {.format = SDL_AUDIO_S16, .channels = 2, .freq = open_rate};
    SDL_AudioStream *s = NULL;
    if (SDL_InitSubSystem(SDL_INIT_AUDIO)) {
        s = SDL_OpenAudioDeviceStream(SDL_AUDIO_DEVICE_DEFAULT_PLAYBACK, &spec, NULL, NULL);
        if (s && !SDL_ResumeAudioStreamDevice(s)) {
            SDL_DestroyAudioStream(s);
            s = NULL;
        }
        if (!s) SDL_QuitSubSystem(SDL_INIT_AUDIO);
    }
    stream = s;
    int expected = OPEN_PENDING;
    if (!SDL_CompareAndSwapAtomicInt(&open_state, expected, s ? OPEN_READY : OPEN_FAILED)) { /* abandoned while opening */
        if (s) {
            SDL_DestroyAudioStream(s);
            SDL_QuitSubSystem(SDL_INIT_AUDIO);
        }
        stream = NULL;
        SDL_SetAtomicInt(&open_state, OPEN_NONE);
    }
    SDL_SignalSemaphore(done);
    return 0;
}

static void start_open(u32 rate)
{
    SDL_Semaphore *done = SDL_CreateSemaphore(0);
    open_rate = (int)rate;
    SDL_SetAtomicInt(&open_state, OPEN_PENDING);
    SDL_Thread *thread = done ? SDL_CreateThread(open_worker, "n64c-audio-open", done) : NULL;
    if (!thread) {
        SDL_SetAtomicInt(&open_state, OPEN_FAILED);
        if (done) SDL_DestroySemaphore(done);
        return;
    }
    SDL_DetachThread(thread);
    (void)SDL_WaitSemaphoreTimeout(done, OPEN_WAIT_MS);
    /* The semaphore is left to the helper's lifetime: a tiny one-off allocation per open attempt. */
    stream_rate = rate;
}
#endif

#if defined(_WIN32) || defined(__ANDROID__)
static void default_sink_inner(void *user, const int16_t *frames, size_t count, uint32_t rate);
/* The guest runs with floating point traps enabled (osSetFpcCsr); SDL's audio code computes with values that raise invalid-
 * operation flags harmlessly. Its calls run with the traps masked (n64c_system.h). */
static void default_sink(void *user, const int16_t *frames, size_t count, uint32_t rate)
{
    const int enabled = n64c_fp_traps_mask();
    default_sink_inner(user, frames, count, rate);
    n64c_fp_traps_restore(enabled);
}
static void default_sink_inner(void *user, const int16_t *frames, size_t count, uint32_t rate)
#else
static void default_sink(void *user, const int16_t *frames, size_t count, uint32_t rate)
#endif
{
    (void)user;
#ifdef N64C_AUDIO_SDL3
    if (SDL_GetAtomicInt(&open_state) == OPEN_NONE) start_open(rate);
    if (SDL_GetAtomicInt(&open_state) != OPEN_READY) return; /* silent: still opening, or no device */
    const SDL_AudioSpec spec = {.format = SDL_AUDIO_S16, .channels = 2, .freq = (int)rate};
    if (stream_rate != rate) {
        if (!SDL_SetAudioStreamFormat(stream, &spec, NULL)) return;
        stream_rate = rate;
    }
    /* Above a quarter second queued (a guest faster than real time, N64C_SPEED) new frames are dropped,
     * so the queue and its latency stay bounded; the guest's AI FIFO runs on the virtual clock regardless. */
    if (SDL_GetAudioStreamQueued(stream) > (int)rate) return;
    /* n64a_ai_submit supplies at most 256 frames per call. SDL copies the data. */
    (void)SDL_PutAudioStreamData(stream, frames, (int)(count * 2 * sizeof *frames));
#else
    (void)frames;
    (void)count;
    (void)rate;
#endif
}

/* The tap (N64C_AUDIO_OUT): what the sink gets, appended as raw s16 stereo; <path>.rate holds "<rate>\n", replaced
 * whole whenever the rate changes, so a reader can wrap the growing file as WAV while it plays. It keeps to wall
 * time: a block is written only while the file holds less than the wall time since its first block plus TAP_LEAD_MS,
 * so a guest faster than real time (N64C_SPEED) is heard in real-time snippets, skipping the rest. Host-only: the
 * guest's AI FIFO and clock never see it. */
#define TAP_LEAD_MS 250
static FILE *tap;
static u32 tap_rate;
static double tap_seconds; /* audio written, in seconds */
static struct timespec tap_start;
static int tap_started;
static char tap_rate_path[1024];
static void tap_close(void)
{
    if (tap) fclose(tap);
    tap = NULL;
    tap_rate = 0;
}
int n64c_audio_tap(const char *path)
{
    tap_close();
    if (!path) return 1;
    if (snprintf(tap_rate_path, sizeof tap_rate_path, "%s.rate", path) >= (int)sizeof tap_rate_path) return 0;
    tap = fopen(path, "ab");
    tap_seconds = 0;
    tap_started = 0;
    return tap != NULL;
}
static int tap_note_rate(u32 rate)
{
    char tmp[sizeof tap_rate_path + 4];
    snprintf(tmp, sizeof tmp, "%s.tmp", tap_rate_path);
    FILE *f = fopen(tmp, "w");
    int ok = f && fprintf(f, "%u\n", (unsigned)rate) > 0;
    if (f && fclose(f)) ok = 0;
    return ok && !rename(tmp, tap_rate_path);
}
static void tap_sink(void *user, const int16_t *frames, size_t count, uint32_t rate)
{
    (void)user;
    if (tap && rate != tap_rate) {
        if (tap_note_rate(rate)) tap_rate = rate;
        else tap_close();
    }
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    if (tap && !tap_started) {
        tap_start = now;
        tap_started = 1;
    }
    double wall = (double)(now.tv_sec - tap_start.tv_sec) + (now.tv_nsec - tap_start.tv_nsec) / 1e9;
    if (tap && rate && tap_seconds < wall + TAP_LEAD_MS / 1000.0) {
        if (fwrite(frames, 2 * sizeof *frames, count, tap) != count || fflush(tap)) tap_close();
        else tap_seconds += (double)count / rate;
    }
    (pcm_sink ? pcm_sink : default_sink)(pcm_user, frames, count, rate);
}

void n64c_set_audio_sink(N64aPcmSink sink, void *user)
{
#ifdef N64C_AUDIO_SDL3
    close_stream();
#endif
    pcm_sink = sink;
    pcm_user = user;
}

void n64c_audio_reset(void)
{
    n64c_set_audio_sink(NULL, NULL);
#ifdef N64C_AUDIO_SDL3
    close_stream();
#endif
    tap_close();
    sample_rate = 32000;
    fifo_count = 0;
}

/* The AI interrupt: the buffer at the head of the FIFO has played, so a slot is free for the next one. */
static void buffer_done(void)
{
    fifo_drain_to(n64c_clock_now());
    if (fifo_count) n64c_clock_arm_ai(fifo_end[0], buffer_done);
    n64c_event_post(OS_EVENT_AI);
}
s32 osAiSetFrequency(u32 frequency)
{
    if (frequency < 4000 || frequency > 192000) return -1;
    sample_rate = frequency;
    return (s32)sample_rate;
}

s32 osAiSetNextBuffer(void *buffer, u32 bytes)
{
    /* Whole stereo frames, and the converter's 24-bit addressing limit. */
    if (!buffer || !bytes || (bytes & 3u) || bytes > 0x1000000u || ((uintptr_t)buffer & 3u)) return -1;
    fifo_drain();
    if (fifo_count == 2) return -1; /* both FIFO entries held */
    n64a_ai_submit(buffer, 0, bytes, sample_rate, tap ? tap_sink : pcm_sink ? pcm_sink : default_sink, pcm_user);
    /* Plays after the buffer ahead of it, at the rate in force now; frames * counter / rate in exact integers. */
    OSTime start = fifo_count ? fifo_end[fifo_count - 1] : osGetTime();
    fifo_end[fifo_count++] = start + ((OSTime)(bytes / 4u) * OS_CPU_COUNTER + sample_rate - 1) / sample_rate;
    n64c_clock_arm_ai(fifo_end[0], buffer_done); /* the AI interrupt follows when the oldest held buffer has played */
    return 0;
}

u32 osAiGetLength(void) { return 0; } /* DMA has been copied before submission returns. */
u32 osAiGetStatus(void)
{
    fifo_drain();
    return (fifo_count == 2 ? AI_STATUS_FIFO_FULL : 0u) | (fifo_count ? AI_STATUS_DMA_BUSY : 0u);
}
