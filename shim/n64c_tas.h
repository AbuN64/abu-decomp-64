/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_TAS_H
#define N64C_TAS_H
#include "n64c_cont.h"
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
#ifdef __cplusplus
}
#endif
#endif
