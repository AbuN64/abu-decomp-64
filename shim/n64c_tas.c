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
 * at the first differing poll name the addresses that carry run-to-run state (seeds, clocks, host pointers). */
#include "n64c_tas.h"
#include "n64c_os.h"
#include "n64c_memory.h"
#include <stdint.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

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

/* 64-bit words folded with a multiply-xorshift: order sensitive and fast enough for every poll of 8 MB. */
uint64_t n64c_tas_hash_bytes(const void *data, size_t bytes)
{
    const unsigned char *p = data;
    uint64_t h = 0x9E3779B97F4A7C15ull ^ bytes;
    for (size_t i = 0; i + 8 <= bytes; i += 8) {
        uint64_t w;
        memcpy(&w, p + i, 8);
        h = (h ^ w) * 0xFF51AFD7ED558CCDull;
        h ^= h >> 32;
    }
    for (size_t i = bytes & ~(size_t)7; i < bytes; i++) h = (h ^ p[i]) * 0x100000001B3ull;
    return h;
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
    if (play) {
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
