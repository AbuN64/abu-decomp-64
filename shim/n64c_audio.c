/* SPDX-License-Identifier: GPL-3.0-or-later */
/* AI DMA submission to a native S16 stereo sink; playback is owned by SDL3.
 * Completion means copied to the host sink, not played by the physical device.
 * The two-entry DMA FIFO is modeled on the virtual clock (osAiGetStatus); interrupt timing is not. */
#include "n64c_audio.h"
#include "n64c_internal.h"
#ifdef N64C_AUDIO_SDL3
#include <SDL3/SDL.h>
#endif

static N64aPcmSink pcm_sink;
static void *pcm_user;
static u32 sample_rate = 32000;
static int sink_failed;
static OSTime fifo_end[2]; /* virtual time each held buffer finishes playing, oldest first */
static u32 fifo_count;
static void fifo_drain(void)
{
    OSTime now = osGetTime();
    while (fifo_count && fifo_end[0] <= now) {
        fifo_end[0] = fifo_end[1];
        fifo_count--;
    }
}
#ifdef N64C_AUDIO_SDL3
static SDL_AudioStream *stream;
static int audio_initialized;
static u32 stream_rate;

static void close_stream(void)
{
    if (stream) SDL_DestroyAudioStream(stream);
    stream = NULL;
    if (audio_initialized) SDL_QuitSubSystem(SDL_INIT_AUDIO);
    audio_initialized = 0;
    stream_rate = 0;
}
#endif

static void default_sink(void *user, const int16_t *frames, size_t count, uint32_t rate)
{
    (void)user;
#ifdef N64C_AUDIO_SDL3
    if (sink_failed) return;
    if (!audio_initialized) {
        if (!SDL_InitSubSystem(SDL_INIT_AUDIO)) { sink_failed = 1; return; }
        audio_initialized = 1;
    }
    const SDL_AudioSpec spec = {.format = SDL_AUDIO_S16, .channels = 2, .freq = (int)rate};
    if (!stream) {
        stream = SDL_OpenAudioDeviceStream(SDL_AUDIO_DEVICE_DEFAULT_PLAYBACK, &spec, NULL, NULL);
        if (!stream || !SDL_ResumeAudioStreamDevice(stream)) {
            close_stream();
            sink_failed = 1;
            return;
        }
        stream_rate = rate;
    } else if (stream_rate != rate) {
        if (!SDL_SetAudioStreamFormat(stream, &spec, NULL)) { sink_failed = 1; return; }
        stream_rate = rate;
    }
    /* n64a_ai_submit supplies at most 256 frames per call. SDL copies the data. */
    if (!SDL_PutAudioStreamData(stream, frames, (int)(count * 2 * sizeof *frames))) sink_failed = 1;
#else
    (void)frames;
    (void)count;
    (void)rate;
#endif
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
    sample_rate = 32000;
    sink_failed = 0;
    fifo_count = 0;
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
    sink_failed = 0;
    n64a_ai_submit(buffer, 0, bytes, sample_rate, pcm_sink ? pcm_sink : default_sink, pcm_user);
    if (sink_failed) return -1;
    /* Plays after the buffer ahead of it, at the rate in force now; frames * counter / rate in exact integers. */
    OSTime start = fifo_count ? fifo_end[fifo_count - 1] : osGetTime();
    fifo_end[fifo_count++] = start + ((OSTime)(bytes / 4u) * OS_CPU_COUNTER + sample_rate - 1) / sample_rate;
    n64c_event_post(OS_EVENT_AI);
    return 0;
}

u32 osAiGetLength(void) { return 0; } /* DMA has been copied before submission returns. */
u32 osAiGetStatus(void)
{
    fifo_drain();
    return (fifo_count == 2 ? AI_STATUS_FIFO_FULL : 0u) | (fifo_count ? AI_STATUS_DMA_BUSY : 0u);
}
