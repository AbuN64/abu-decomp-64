/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Public AI path: channel order, conversion, chunking, rates, and completion. */
#include "../n64c_audio.h"
#include "../n64c_internal.h"
#include "../n64c_mmio.h"
#include <stdio.h>
#include <string.h>

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)
typedef struct {
    size_t frames, calls;
    u32 rate;
    OSMesgQueue *events;
} Sink;
static void receive(void *user, const int16_t *samples, size_t count, uint32_t rate)
{
    Sink *sink = user;
    CHECK(rate == sink->rate);
    CHECK(count > 0 && count <= 256);
    OSMesg msg;
    CHECK(osRecvMesg(sink->events, &msg, OS_MESG_NOBLOCK) == -1);
    for (size_t i = 0; i < count; i++) {
        CHECK(samples[2*i] == (int16_t)((sink->frames+i) - 150));
        CHECK(samples[2*i+1] == (int16_t)(30000 - (sink->frames+i)));
    }
    sink->frames += count;
    sink->calls++;
}
static void drop(void *user, const int16_t *samples, size_t count, uint32_t rate)
{
    (void)user; (void)samples; (void)count; (void)rate;
}
int main(int argc, char **argv)
{
    (void)argv;
    _Alignas(8) u8 ram[300*4];
    for (size_t i = 0; i < 300; i++) {
        const int16_t left = (int16_t)(i - 150), right = (int16_t)(30000 - i);
        memcpy(ram + (i*4 ^ 2u), &left, 2);
        memcpy(ram + ((i*4+2) ^ 2u), &right, 2);
    }
    n64c_init(NULL);
    OSMesgQueue events;
    OSMesg messages[4], message;
    osCreateMesgQueue(&events, messages, 4);
    osSetEventMesg(OS_EVENT_AI, &events, (OSMesg){.data32=42});
    Sink sink = {.rate=32000, .events=&events};
    if (argc == 1) n64c_set_audio_sink(receive, &sink);
    CHECK(osAiSetNextBuffer(NULL, 4) == -1);
    CHECK(osAiSetNextBuffer(ram, 0) == -1);
    CHECK(osAiSetNextBuffer(ram, 6) == -1);
    CHECK(osAiSetNextBuffer(ram+1, 4) == -1);
    CHECK(osAiSetNextBuffer(ram, 0x1000004u) == -1);
    CHECK(osAiSetFrequency(0) == -1);
    CHECK(osAiSetFrequency(192001) == -1);
    CHECK(osAiSetNextBuffer(ram, sizeof ram) == 0);
    if (argc == 1) CHECK(sink.frames == 300 && sink.calls == 2);
    CHECK(osAiGetLength() == 0);
    CHECK(osRecvMesg(&events, &message, OS_MESG_NOBLOCK) == 0 && message.data32 == 42);
    CHECK(osRecvMesg(&events, &message, OS_MESG_NOBLOCK) == -1);
    CHECK(osAiSetFrequency(44100) == 44100);
    sink.rate = 44100;
    sink.frames = sink.calls = 0;
    CHECK(osAiSetNextBuffer(ram, 4) == 0);
    if (argc == 1) CHECK(sink.frames == 1 && sink.calls == 1);
    CHECK(osRecvMesg(&events, &message, OS_MESG_NOBLOCK) == 0 && message.data32 == 42);
    n64c_shutdown();
    /* Init restores the default rate and releases any SDL device. */
    n64c_init(NULL);
    osCreateMesgQueue(&events, messages, 4);
    osSetEventMesg(OS_EVENT_AI, &events, (OSMesg){.data32=42});
    sink.frames = sink.calls = 0;
    sink.rate = 32000;
    if (argc == 1) n64c_set_audio_sink(receive, &sink);
    CHECK(osAiSetNextBuffer(ram, 4) == 0);
    if (argc == 1) CHECK(sink.frames == 1);
    n64c_shutdown();
    /* Two-entry FIFO: full with two held buffers, refuses a third, drains as virtual time plays them. */
    n64c_init(NULL);
    n64c_set_audio_sink(drop, NULL);
    CHECK(osAiGetStatus() == 0);
    CHECK(osAiSetFrequency(32000) == 32000);
    CHECK(osAiSetNextBuffer(ram, 32*4) == 0);   /* 1 ms of audio: 46875 ticks */
    CHECK(osAiGetStatus() == AI_STATUS_DMA_BUSY);
    CHECK(osAiSetNextBuffer(ram, 64*4) == 0);   /* 2 ms more, ends at 140625 */
    CHECK(osAiGetStatus() == (AI_STATUS_FIFO_FULL | AI_STATUS_DMA_BUSY));
    CHECK(osAiSetNextBuffer(ram, 4) == -1);
    u32 status;
    CHECK(n64c_mmio_read(0x0450000Cu, &status) && status == osAiGetStatus());
    n64c_clock_advance_to(46875);
    CHECK(osAiGetStatus() == AI_STATUS_DMA_BUSY);
    CHECK(osAiSetNextBuffer(ram, 4) == 0);      /* queued after the 2 ms buffer */
    CHECK(osAiGetStatus() & AI_STATUS_FIFO_FULL);
    n64c_clock_advance_to(140625);
    CHECK(osAiGetStatus() == AI_STATUS_DMA_BUSY);
    n64c_clock_advance_to(150000);
    CHECK(osAiGetStatus() == 0);
    n64c_shutdown();
    /* The tap (N64C_AUDIO_OUT): every frame the sink gets is appended, the rate kept beside it; reset closes it. */
    n64c_init(NULL);
    n64c_set_audio_sink(drop, NULL);
    const char *tap_path = "n64c_test_audio.pcm", *rate_path = "n64c_test_audio.pcm.rate";
    (void)remove(tap_path);
    (void)remove(rate_path);
    CHECK(n64c_audio_tap(tap_path));
    CHECK(osAiSetFrequency(22050) == 22050);
    CHECK(osAiSetNextBuffer(ram, sizeof ram) == 0);
    n64c_shutdown();
    FILE *pcm = fopen(tap_path, "rb");
    int16_t got[601] = {0};
    size_t words = pcm ? fread(got, sizeof *got, 601, pcm) : 0;
    if (pcm) fclose(pcm);
    CHECK(words == 600);
    for (size_t i = 0; i < 300 && words == 600; i++)
        CHECK(got[2*i] == (int16_t)(i - 150) && got[2*i+1] == (int16_t)(30000 - i));
    FILE *rate = fopen(rate_path, "r");
    char line[16] = {0};
    CHECK(rate && fgets(line, sizeof line, rate) && !strcmp(line, "22050\n"));
    if (rate) fclose(rate);
    CHECK(!n64c_audio_tap("/nonexistent-dir/x.pcm"));
    CHECK(remove(tap_path) == 0 && remove(rate_path) == 0);
    printf("n64c_audio: %s\n", fails ? "FAIL" : "ok");
    return fails ? 1 : 0;
}
