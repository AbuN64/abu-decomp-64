/* SPDX-License-Identifier: GPL-3.0-or-later */
/* AI output adapter. The guest buffer has the audio ABI's word-swapped layout. */
#ifndef N64C_AUDIO_H
#define N64C_AUDIO_H
#include "n64c_os.h"
#include "audio_abi/n64a_abi.h"

/* Called synchronously on the guest thread. NULL restores the default sink:
 * SDL3 in a full build, a null sink in a shim-only build. Set while idle. */
void n64c_set_audio_sink(N64aPcmSink sink, void *user);
void n64c_audio_reset(void); /* also closes the tap */
/* Appends every PCM frame the sink gets to path (s16 stereo, host order) and keeps path.rate = "<rate>\n";
 * NULL closes it. Returns 0 when path cannot be opened. A write error closes the tap, never the audio. The tap keeps
 * to wall time (at most 250 ms ahead of it): a guest faster than real time is tapped in snippets, the rest skipped. */
int n64c_audio_tap(const char *path);
#endif
