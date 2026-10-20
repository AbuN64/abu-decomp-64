/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_BOOT_FRAME_H
#define N64C_BOOT_FRAME_H
#include "n64c_os.h"
/* audio_abi: N64aFlavour of the game's audio microcode, or -1 to refuse audio tasks. */
/* window_width/height 0: offscreen software rendering (capture); otherwise a visible window of that size. */
int n64c_boot_frame_start(u32 width,u32 height,u32 ram_bytes,int audio_abi,u32 window_width,u32 window_height);
/* Run the original threads at the VI rate, showing every presentation, until the window closes (1) or
 * no original thread is left (0). Original stops exit the process. */
int n64c_boot_frame_live(void);
int n64c_boot_frame_capture(const char *path,u32 retrace,u32 width,u32 height);
void n64c_boot_frame_close(void);
#endif
