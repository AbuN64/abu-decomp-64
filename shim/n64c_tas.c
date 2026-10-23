/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Controller movies keyed to the guest's own poll count, so a replay is the same
 * run at any host speed. N64C_TAS_PLAY names a raw movie that replaces channel 0
 * (one "BUTTONS STICK_X STICK_Y" line per poll, hex buttons, decimal stick);
 * after its last line the live input source takes over (neutral when there is
 * none), and N64C_TAS_EXIT_AFTER polls later the process exits 0. N64C_TAS_RECORD appends every poll's channel 0 in the
 * same format, flushed per line, so a crash leaves the polls it reached.
 * N64C_TAS_SHOT_EVERY and N64C_TAS_SHOT_DIR ask the frame loop for a
 * picture every N polls, to compare with the oracle's at the same poll.
 * N64C_TAS_HASH writes "<poll> <hash>" of all guest RDRAM at every poll and N64C_TAS_DUMP_AT=P with
 * N64C_TAS_DUMP_PATH writes RDRAM at poll P: two runs of one movie must give the same lines, and two dumps
 * at the first differing poll name the addresses that carry run-to-run state (seeds, clocks, host pointers).
 * N64C_INPUT=<path> is a live controller driven by another process (the TAS page): the port binds a Unix datagram
 * socket there ("@name": Linux abstract namespace). One input path, two sources: channel 0 comes from the movie until live input takes over, then from
 * the socket for the rest of the run. Without a movie live input holds from the first poll; with one, the datagram
 * "take" switches at the poll that reads it (the movie stops, N64C_TAS_EXIT_AFTER no longer applies, and the frame
 * loop drops to real time, n64c_tas_live_taken). Each datagram "SEQ BUTTONS STICK_X STICK_Y" (decimal SEQ, hex
 * buttons) is the pad from the next poll on; pads sent before a take are dropped. Every poll reads all waiting
 * datagrams: the pad is the newest, plus any button held in an older one, so a press shorter than one poll still
 * reaches the game. Polls are recorded like any other (N64C_TAS_RECORD), so a run taken over mid-movie replays as
 * the movie's prefix plus the human's polls; n64c_tas_live_seq names the newest SEQ applied, which the live picture
 * carries so a viewer can time key -> frame. */
#include "n64c_tas.h"
#include "n64c_os.h"
#include "n64c_memory.h"
#include <stdint.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stddef.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

static int started;
static FILE *play, *record;
static long exit_after = -1, polls, played;
static int play_done;
static long shot_every;
static const char *shot_dir;
static atomic_long shot_due = -1;
static FILE *trace, *hash;
static long dump_at = -1;
static const char *dump_path;
static int live = -1;
static OSContPad live_pad;
static unsigned live_held;
static int live_fresh;
static atomic_int taken;
static atomic_ulong live_seq;

/* "@name" is a Linux abstract socket (no file, no path length limit from the run folder); anything else a path.
 * A socket that cannot be bound leaves the run without live input (one warning), never stops it. */
static void live_open(const char *path)
{
    struct sockaddr_un addr = {.sun_family = AF_UNIX};
    size_t len = strlen(path);
    if (len >= sizeof addr.sun_path) {
        fprintf(stderr, "n64c-tas: N64C_INPUT name too long, no live input: %s\n", path);
        return;
    }
    socklen_t size = sizeof addr;
    if (path[0] == '@') {
        memcpy(addr.sun_path + 1, path + 1, len - 1); /* sun_path[0] stays 0: the abstract namespace */
        size = (socklen_t)(offsetof(struct sockaddr_un, sun_path) + len);
    } else {
        memcpy(addr.sun_path, path, len);
        unlink(path); /* a socket left by an earlier run of the same folder */
    }
    int fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd < 0 || bind(fd, (struct sockaddr *)&addr, size)) {
        fprintf(stderr, "n64c-tas: cannot bind N64C_INPUT %s, no live input\n", path);
        if (fd >= 0) close(fd);
        return;
    }
    live = fd;
    fprintf(stderr, "n64c-tas: live input on %s\n", path);
}

/* Drains the socket: a take switches the source; the newest pad wins, older held buttons count for this poll only. */
static void live_drain(void)
{
    char msg[64];
    for (;;) {
        ssize_t n = recv(live, msg, sizeof msg - 1, 0);
        if (n < 0) break;
        msg[n] = 0;
        if (!strncmp(msg, "take", 4)) {
            if (!atomic_exchange(&taken, 1)) fprintf(stderr, "n64c-tas: live input took over at poll %ld\n", polls + 1);
            continue;
        }
        unsigned long seq;
        unsigned buttons;
        int x, y;
        if (sscanf(msg, "%lu %x %d %d", &seq, &buttons, &x, &y) != 4 || buttons > 0xffff || x < -128 || x > 127 ||
            y < -128 || y > 127) {
            fprintf(stderr, "n64c-tas: bad live input %s\n", msg);
            continue;
        }
        if (!atomic_load(&taken)) continue;
        if (live_fresh) live_held |= live_pad.button;
        live_pad = (OSContPad){.button = (u16)buttons, .stick_x = (s8)x, .stick_y = (s8)y};
        atomic_store(&live_seq, seq);
        live_fresh = 1;
    }
}

static void start(void)
{
    started = 1;
    const char *path = getenv("N64C_TAS_PLAY");
    if (path && *path && !(play = fopen(path, "r"))) {
        fprintf(stderr, "n64c-tas: cannot read %s\n", path);
        _Exit(2);
    }
    path = getenv("N64C_TAS_RECORD");
    if (path && *path && !(record = fopen(path, "a"))) {
        fprintf(stderr, "n64c-tas: cannot append %s\n", path);
        _Exit(2);
    }
    const char *every = getenv("N64C_TAS_SHOT_EVERY");
    shot_dir = getenv("N64C_TAS_SHOT_DIR");
    if (every && *every && shot_dir && *shot_dir) shot_every = strtol(every, NULL, 10);
    path = getenv("N64C_TAS_TRACE");
    if (path && *path && !(trace = fopen(path, "w"))) {
        fprintf(stderr, "n64c-tas: cannot write %s\n", path);
        _Exit(2);
    }
    path = getenv("N64C_TAS_HASH");
    if (path && *path && !(hash = fopen(path, "w"))) {
        fprintf(stderr, "n64c-tas: cannot write %s\n", path);
        _Exit(2);
    }
    const char *at = getenv("N64C_TAS_DUMP_AT");
    dump_path = getenv("N64C_TAS_DUMP_PATH");
    if (at && *at) {
        if (!dump_path || !*dump_path) {
            fprintf(stderr, "n64c-tas: N64C_TAS_DUMP_AT needs N64C_TAS_DUMP_PATH\n");
            _Exit(2);
        }
        dump_at = strtol(at, NULL, 10);
    }
    path = getenv("N64C_INPUT");
    if (path && *path) {
        live_open(path);
        if (live >= 0 && !play) atomic_store(&taken, 1);
    }
    const char *after = getenv("N64C_TAS_EXIT_AFTER");
    if (after && *after) exit_after = strtol(after, NULL, 10);
}

/* Reads the next movie line into pad; returns 0 at the end of the movie. */
static int next(OSContPad *pad)
{
    char line[64];
    while (fgets(line, sizeof line, play)) {
        unsigned buttons;
        int x, y;
        if (line[0] == '#' || line[0] == '\n') continue;
        if (sscanf(line, "%x %d %d", &buttons, &x, &y) != 3 || buttons > 0xffff ||
            x < -128 || x > 127 || y < -128 || y > 127) {
            fprintf(stderr, "n64c-tas: bad movie line after poll %ld: %s", played, line);
            _Exit(2);
        }
        *pad = (OSContPad){0};
        pad->button = (u16)buttons;
        pad->stick_x = (s8)x;
        pad->stick_y = (s8)y;
        played++;
        return 1;
    }
    return 0;
}

/* Eight independent 32-bit lanes (multiply-xorshift), folded into 64 bits at the end: order sensitive, and the lanes
 * run in parallel (SIMD where the CPU has a 32-bit vector multiply), so hashing 8 MB every poll stays cheap. */
#if (defined(__i386__) || defined(__x86_64__)) && defined(__GNUC__)
__attribute__((target_clones("avx2", "sse4.1", "default")))
#endif
uint64_t n64c_tas_hash_bytes(const void *data, size_t bytes)
{
    const unsigned char *p = data;
    uint32_t lane[8] = {1, 2, 3, 4, 5, 6, 7, 8};
    size_t i = 0;
    for (; i + 32 <= bytes; i += 32)
        for (int k = 0; k < 8; k++) {
            uint32_t w;
            memcpy(&w, p + i + 4 * k, 4);
            lane[k] = (lane[k] ^ w) * 0x9E3779B1u;
            lane[k] ^= lane[k] >> 15;
        }
    uint64_t h = 0x9E3779B97F4A7C15ull ^ bytes;
    for (int k = 0; k < 8; k++) h = (h ^ lane[k]) * 0xFF51AFD7ED558CCDull;
    for (; i < bytes; i++) h = (h ^ p[i]) * 0x100000001B3ull;
    return h ^ (h >> 32);
}

static void ram_state(void)
{
    u32 size = n64c_memory_ram_size();
    const void *ram = size ? n64c_memory_ram(0, size) : NULL;
    if (hash) {
        fprintf(hash, "%ld %016llx\n", polls, ram ? (unsigned long long)n64c_tas_hash_bytes(ram, size) : 0ull);
        fflush(hash);
    }
    if (polls == dump_at) {
        FILE *f = fopen(dump_path, "wb");
        if (!f || !ram || fwrite(ram, 1, size, f) != size || fclose(f)) {
            fprintf(stderr, "n64c-tas: dump at poll %ld to %s failed\n", polls, dump_path);
            _Exit(2);
        }
        fprintf(stderr, "n64c-tas: dumped %u bytes at poll %ld\n", size, polls);
    }
}

void n64c_tas_poll(OSContPad *pad)
{
    if (!started) start();
    if (live >= 0) live_drain();
    if (play && !atomic_load(&taken)) {
        if (!play_done && !next(pad)) {
            play_done = 1;
            fprintf(stderr, "n64c-tas: movie ended at poll %ld\n", played);
        }
        if (play_done) {
            if (exit_after >= 0 && polls - played >= exit_after) {
                fprintf(stderr, "n64c-tas: done polls=%ld\n", polls);
                fflush(stderr);
                _Exit(0);
            }
        }
    }
    if (atomic_load(&taken)) {
        *pad = live_pad;
        pad->button |= (u16)live_held;
        live_held = 0;
        live_fresh = 0;
    }
    polls++;
    if (hash || dump_at >= 0) ram_state();
    if (trace) { fprintf(trace, "%ld %u\n", polls, (unsigned)osViGetRetraceCount()); fflush(trace); }
    if (shot_every > 0 && polls % shot_every == 0) atomic_store(&shot_due, polls);
    if (record) {
        fprintf(record, "%04x %d %d\n", pad->button, pad->stick_x, pad->stick_y);
        fflush(record);
    }
}

long n64c_tas_take_shot(void) { return atomic_exchange(&shot_due, -1); }
const char *n64c_tas_shot_dir(void) { return shot_dir; }
unsigned long n64c_tas_live_seq(void) { return atomic_load(&live_seq); }
int n64c_tas_live_taken(void) { return atomic_load(&taken); }
