/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Controller movies keyed to the guest's own poll count, so a replay is the same
 * run at any host speed. N64C_TAS_PLAY names a raw movie that replaces channel 0
 * (one "BUTTONS STICK_X STICK_Y" line per poll, hex buttons, decimal stick);
 * after its last line the pad is neutral, and N64C_TAS_EXIT_AFTER polls later
 * the process exits 0. N64C_TAS_RECORD appends every poll's channel 0 in the
 * same format, flushed per line, so a crash leaves the polls it reached.
 * N64C_TAS_SHOT_EVERY and N64C_TAS_SHOT_DIR ask the frame loop for a
 * picture every N polls, to compare with the oracle's at the same poll. */
#include "n64c_tas.h"
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>

static int started;
static FILE *play, *record;
static long exit_after = -1, polls, played;
static int play_done;
static long shot_every;
static const char *shot_dir;
static atomic_long shot_due = -1;

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

void n64c_tas_poll(OSContPad *pad)
{
    if (!started) start();
    if (play) {
        if (!play_done && !next(pad)) {
            play_done = 1;
            fprintf(stderr, "n64c-tas: movie ended at poll %ld\n", played);
        }
        if (play_done) {
            *pad = (OSContPad){0};
            if (exit_after >= 0 && polls - played >= exit_after) {
                fprintf(stderr, "n64c-tas: done polls=%ld\n", polls);
                fflush(stderr);
                _Exit(0);
            }
        }
    }
    polls++;
    if (shot_every > 0 && polls % shot_every == 0) atomic_store(&shot_due, polls);
    if (record) {
        fprintf(record, "%04x %d %d\n", pad->button, pad->stick_x, pad->stick_y);
        fflush(record);
    }
}

long n64c_tas_take_shot(void) { return atomic_exchange(&shot_due, -1); }
const char *n64c_tas_shot_dir(void) { return shot_dir; }
