/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_TAS_H
#define N64C_TAS_H
#include "n64c_cont.h"
#include <stddef.h>
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
/* The hash N64C_TAS_HASH writes per poll, over any bytes (exposed for tests and tools). */
uint64_t n64c_tas_hash_bytes(const void *data, size_t bytes);
#ifdef __cplusplus
}
#endif
#endif
