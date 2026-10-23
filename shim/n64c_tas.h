/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_TAS_H
#define N64C_TAS_H
#include "n64c_cont.h"
#include <stddef.h>
#include <stdio.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
/* Called once per guest controller poll with channel 0's pad after the live
 * input source; replays or records it per N64C_TAS_PLAY / N64C_TAS_RECORD. */
void n64c_tas_poll(OSContPad *pad);
/* With N64C_TAS_SHOT_EVERY=N and N64C_TAS_SHOT_DIR, every Nth poll asks the frame loop for a picture: this
 * returns that poll's number once (then -1) and the folder to write poll-<n>.ppm into. */
long n64c_tas_take_shot(void);
const char *n64c_tas_shot_dir(void);
/* N64C_TAS_TRACE_DRAWS=POLL:PATH: the first graphics task at or after controller poll POLL writes its draw trace
 * (n64c_dltrace.h) to PATH. Returns the open file once, when that task is due, else NULL; the caller closes it. */
FILE *n64c_tas_draw_trace(void);
/* With N64C_INPUT, the SEQ of the newest live input a poll applied (0 before any). */
unsigned long n64c_tas_live_seq(void);
/* 1 once live input (N64C_INPUT) drives channel 0: from the start without a movie, else after a "take" or at poll
 * N64C_TAS_TAKE_AT. */
int n64c_tas_live_taken(void);
/* The hash N64C_TAS_HASH writes per poll, over any bytes (exposed for tests and tools). */
uint64_t n64c_tas_hash_bytes(const void *data, size_t bytes);
#ifdef __cplusplus
}
#endif
#endif
