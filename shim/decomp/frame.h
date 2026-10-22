/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_BOOT_FRAME_H
#define N64C_BOOT_FRAME_H
#include "n64c_os.h"
/* audio_abi: N64aFlavour of the game's audio microcode, or -1 to refuse audio tasks. */
/* window_width/height 0: offscreen software rendering (capture); otherwise a visible window of that size. */
/* Program and user files for the renderer (shader archive, settings file; NULL: beside the executable) and an
 * offscreen live run (no visible window). Applies to the next n64c_boot_frame_start. */
void n64c_boot_frame_host(const char *shaders,const char *settings,int offscreen);
int n64c_boot_frame_start(u32 width,u32 height,u32 ram_bytes,int audio_abi,u32 window_width,u32 window_height);
/* Run the original threads at the VI rate, showing every presentation, until the window closes (1) or
 * no original thread is left (0). Original stops exit the process. */
int n64c_boot_frame_live(void);
/* Wall-clock pacing only: N64C_SPEED=k (default 1) makes retrace n due n*1001/(60000*k) s after start, 0 never
 * waits. The guest's clock advances per retrace either way, so the game's own timing is the same at any speed.
 * n64c_retrace_due returns 0 when there is no wait; n64c_parse_speed returns 0 for anything but 0..1000. */
struct timespec;
int n64c_retrace_due(const struct timespec *start,u32 n,u32 speed,struct timespec *due);
int n64c_parse_speed(const char *text,u32 *speed);
int n64c_boot_frame_capture(const char *path,u32 retrace,u32 width,u32 height);
void n64c_boot_frame_close(void);
#endif
