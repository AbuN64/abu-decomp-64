/* SPDX-License-Identifier: GPL-3.0-or-later */
/* n64c_vi: video interface timing. Retraces are events on the virtual clock at 60 Hz of guest time, retrace 1 at
 * time 0, like the oracle's (hosts/n64recomp/RUNTIME.md "Virtual guest time");
 * osViSwapBuffer takes effect at the next retrace, like the hardware latching the origin register. */
#include "n64c_internal.h"
#include "n64c_diag.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>

static struct {
    OSMesgQueue *mq;
    OSMesg msg;
    u32 every;        /* deliver on every Nth retrace */
    u32 retraces;
    int notify_pending;
    void *current, *next;
    int swap_pending;
    int black;
    f32 yscale;
    OSViMode mode;
    int mode_set;
    u32 control;
} vi;

static void vi_due(void);
void n64c_vi_reset(void)
{
    vi.mq = NULL;
    vi.every = 1;
    vi.retraces = 0;
    vi.notify_pending = 0;
    vi.current = vi.next = NULL;
    vi.swap_pending = 0;
    vi.black = 0;
    vi.yscale = 1.0f;
    vi.mode_set = 0;
    vi.mode = (OSViMode){0};
    vi.control = 0;
    n64c_clock_arm_vi(0, vi_due);
}
static _Noreturn void stop(const char *what, u32 value)
{
    char subject[64];
    snprintf(subject, sizeof subject, "vi-%s", what);
    if (n64c_diag_stop) (void)n64c_diag_stop(subject, 0); /* run-on: recorded; no way on */
    fprintf(stderr, "boot-missing: vi-%s 0x%x\n", what, (unsigned)value);
    fflush(stderr);
    _Exit(78);
}
/* The mode's register set replaces the current one and its control word becomes the VI control; special
 * features set afterwards adjust that control word (osViSetSpecialFeatures). */
void osViSetMode(OSViMode *mode)
{
    if (!mode) stop("mode", 0);
    vi.mode = *mode; /* comRegs.ctrl stays the mode's own control word */
    vi.mode_set = 1;
    vi.control = mode->comRegs.ctrl;
}
void osViSetSpecialFeatures(u32 func)
{
    static const struct { u32 on, off, bit; } features[] = {
        {OS_VI_GAMMA_ON, OS_VI_GAMMA_OFF, VI_CTRL_GAMMA_ON},
        {OS_VI_GAMMA_DITHER_ON, OS_VI_GAMMA_DITHER_OFF, VI_CTRL_GAMMA_DITHER_ON},
        {OS_VI_DIVOT_ON, OS_VI_DIVOT_OFF, VI_CTRL_DIVOT_ON},
        {OS_VI_DITHER_FILTER_ON, OS_VI_DITHER_FILTER_OFF, VI_CTRL_DITHER_FILTER_ON},
    };
    if (func & ~0xFFu) stop("features", func);
    for (unsigned i = 0; i < sizeof features / sizeof features[0]; i++) {
        if (func & features[i].on) vi.control |= features[i].bit;
        if (func & features[i].off) vi.control &= ~features[i].bit; /* OFF wins when both are given */
    }
    /* The dither filter works in anti-alias mode 0 only: ON selects it, OFF restores the mode's own. */
    if (func & OS_VI_DITHER_FILTER_ON) vi.control &= ~VI_CTRL_ANTIALIAS_MASK;
    if (func & OS_VI_DITHER_FILTER_OFF)
        vi.control = (vi.control & ~VI_CTRL_ANTIALIAS_MASK) | (vi.mode.comRegs.ctrl & VI_CTRL_ANTIALIAS_MASK);
}
const OSViMode *n64c_vi_mode(void) { return vi.mode_set ? &vi.mode : NULL; }
u32 n64c_vi_control(void) { return vi.control; }
u32 n64c_vi_rows(void)
{
    if (!vi.mode_set) return 0;
    const u32 v = vi.mode.fldRegs[0].vStart, first = v >> 16 & 0x3FFu, last = v & 0x3FFu;
    if (last <= first) return 0;
    const double rows = (last - first) / 2.0 * (vi.mode.fldRegs[0].yScale & 0xFFFu) / 1024.0 * vi.yscale;
    return rows > 0.0 ? (u32)ceil(rows) : 0;
}
/* The native host driver already owns VI and timer servicing from n64c_init.
 * It runs between guest dispatches, so there is no manager guest thread to
 * assign a priority to. Creation is idempotent and must not reset live VI state. */
void osCreateViManager(OSPri priority) { (void)priority; }
void osViSetEvent(OSMesgQueue *mq, OSMesg msg, u32 retraceCount)
{
    vi.mq = mq;
    vi.msg = msg;
    vi.every = retraceCount ? retraceCount : 1;
}
void osViSwapBuffer(void *fb)
{
    vi.next = fb;
    vi.swap_pending = 1;
}
void osViBlack(u8 active) { vi.black = active; }
void osViSetYScale(f32 y) { vi.yscale = y; }
void *osViGetCurrentFramebuffer(void) { return vi.current; }
void *osViGetNextFramebuffer(void) { return vi.swap_pending ? vi.next : vi.current; }
u32 osViGetRetraceCount(void) { return vi.retraces; }
void *n64c_vi_current_fb(void) { return vi.black ? NULL : vi.current; }
/* Absolute time of retrace number n (1-based): (n - 1) / 60 s, exact in Count ticks. */
static OSTime retrace_time(u32 n) { return (OSTime)(n - 1) * (OS_CPU_COUNTER / 60u); }
OSTime n64c_vi_next_retrace_time(void) { return retrace_time(vi.retraces + 1); }
/* The next retrace is armed before this one runs: the message it sends can move the baton to another guest thread. */
static void vi_due(void)
{
    n64c_clock_arm_vi(retrace_time(vi.retraces + 2), vi_due);
    n64c_vi_retrace();
}
void n64c_vi_retrace(void)
{
    vi.retraces++;
    if (vi.swap_pending) {
        vi.current = vi.next;
        vi.swap_pending = 0;
    }
    if (vi.retraces % vi.every == 0) vi.notify_pending = 1;
    n64c_event_post(OS_EVENT_VI);
}
void n64c_vi_interrupt(void)
{
    int notify = vi.notify_pending;
    vi.notify_pending = 0;
    if (notify && vi.mq) osSendMesg(vi.mq, vi.msg, OS_MESG_NOBLOCK);
}
