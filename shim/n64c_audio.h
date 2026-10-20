/* SPDX-License-Identifier: GPL-3.0-or-later */
/* AI output adapter. The guest buffer has the audio ABI's word-swapped layout. */
#ifndef N64C_AUDIO_H
#define N64C_AUDIO_H
#include "n64c_os.h"
#include "audio_abi/n64a_abi.h"

/* Called synchronously on the guest thread. NULL restores the default sink:
 * SDL3 in a full build, a null sink in a shim-only build. Set while idle. */
void n64c_set_audio_sink(N64aPcmSink sink, void *user);
void n64c_audio_reset(void);
#endif
