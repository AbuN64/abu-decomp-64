/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Shared internals of the n64compat adapters. Not for use outside platform/n64compat. */
#ifndef N64C_INTERNAL_H
#define N64C_INTERNAL_H
#include "n64c_os.h"
/* n64c_sched.c */
void n64c_sched_reset(void);
void n64c_sched_run_idle(void);               /* host: step guest threads until all blocked */
int n64c_sched_alive(void);
uint32_t n64c_sched_waiting(void);
void n64c_event_post(u32 event);              /* non-blocking send to the queue set by osSetEventMesg */
void n64c_clock_advance_to(OSTime t);         /* fire timers due up to t, stepping threads after each */
OSTime n64c_clock_now(void);                /* the virtual Count, including the guest work done so far */
void n64c_clock_work(void);                 /* one function entry of the original code ran (boot/trace.c's entry hook) */
void n64c_clock_arm_ai(OSTime t, void (*due)(void)); /* call due once the clock reaches t (one slot: the audio FIFO) */
void n64c_sched_trace(void (*sink)(const char *line, void *user), void *user);
/* n64c_vi.c */
void n64c_vi_reset(void);
OSTime n64c_vi_next_retrace_time(void);
void n64c_vi_interrupt(void);                 /* deliver the pending VI manager notification */
void n64c_vi_retrace(void);                   /* count, deliver VI message, swap buffer */
void *n64c_vi_current_fb(void);
/* n64c_pi.c */
int n64c_pi_open(const char *rom_path);
int n64c_pi_read(u32 address,void *destination,u32 bytes);
int n64c_pi_dma(u32 address,void *destination,u32 bytes); /* a guest DMA: n64c_pi_read + run-on layout check */
void n64c_pi_close(void);
/* n64c_cont.c */
void n64c_cont_reset(const N64cConfig *cfg);
void n64c_cont_close(void);
#endif
