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
#ifdef __cplusplus
}
#endif
#endif
