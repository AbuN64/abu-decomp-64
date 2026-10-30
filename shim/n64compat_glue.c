/* SPDX-License-Identifier: GPL-3.0-or-later */
/* n64compat_glue: the host frame driver and RSP task dispatch. This is the only place that ties the
 * adapters together (sched, vi, pi, cont, gfx); every function is a ledger-tracked adapter, no framework. */
#include "n64c_prof.h"
#include "n64c_internal.h"
#include "n64c_diag.h"
#include "n64c_audio.h"
#include "n64c_memory.h"
#include "n64c_system.h"
#include <fenv.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static struct { N64cTaskFn fn; void *user; } handlers[3];
static N64cPresentFn present_fn;
static void *present_user;
static u64 *rendered_output; /* output buffer of the last graphics task the native RSP ran */

/* Native device callbacks execute host code, not guest CPU instructions. An RSP task must
 * not inherit CPU traps or leak host exception flags back into the CPU. Stack-local state
 * also preserves the immediate caller when callbacks nest. */
static void host_fpu_enter(fenv_t *saved)
{
    if (fegetenv(saved) == 0 && fesetenv(FE_DFL_ENV) == 0) return;
    fprintf(stderr, "boot-missing: host-FPU-enter\n");
    _Exit(78);
}
static void host_fpu_leave(const fenv_t *saved)
{
    if (fesetenv(saved) == 0) return;
    fprintf(stderr, "boot-missing: host-FPU-leave\n");
    _Exit(78);
}

void n64c_set_task_handler(u32 type, N64cTaskFn fn, void *user)
{
    if (type < 3) {
        handlers[type].fn = fn;
        handlers[type].user = user;
    }
}
void n64c_set_present(N64cPresentFn fn, void *user)
{
    present_fn = fn;
    present_user = user;
}
void osSpTaskLoad(OSTask *t) { (void)t; }
/* The RSP work runs inside the call, where guest time stands still; the game sees the SP (and DP, for graphics)
 * events a fixed guest duration later, as the oracle's tasks complete (graphics 8 ms, every other task 2 ms). The
 * audio task is handed to the audio adapter, which owns the PCM sink. */
enum { GFX_TASK_TICKS = OS_CPU_COUNTER / 125, RSP_TASK_TICKS = OS_CPU_COUNTER / 500 };
static void rsp_task_done(void *graphics)
{
    n64c_event_post(OS_EVENT_SP);
    if (graphics) n64c_event_post(OS_EVENT_DP);
}
void osSpTaskStartGo(OSTask *t)
{
    if (t->type < 3 && handlers[t->type].fn) {
        fenv_t saved;
        host_fpu_enter(&saved);
        handlers[t->type].fn(t, handlers[t->type].user);
        host_fpu_leave(&saved);
    }
    if (t->type == M_GFXTASK) rendered_output = t->output_buff;
    n64c_clock_schedule(n64c_clock_now() + (t->type == M_GFXTASK ? GFX_TASK_TICKS : RSP_TASK_TICKS), rsp_task_done,
                        t->type == M_GFXTASK ? (void *)t : NULL);
}
s32 osSpTaskYielded(OSTask *t) { (void)t; return 0; }
s32 osDpSetNextBuffer(void *buffer, u64 size)
{
    if (buffer && buffer == (void *)rendered_output) return 0;
    if (n64c_diag_stop && n64c_diag_stop("rdp-buffer", 1)) { /* run-on: the buffer is dropped, the DP completes */
        n64c_event_post(OS_EVENT_DP);
        return 0;
    }
    fprintf(stderr, "boot-missing: rdp-buffer %p size 0x%llx (no native RDP command interpreter)\n", buffer,
            (unsigned long long)size);
    fflush(stderr);
    _Exit(78);
}
/* N64C_GUEST_NS_PER_ENTRY: guest nanoseconds one function entry of the original game costs, the oracle's
 * HOST_GUEST_NS_PER_ENTRY (cpu.calibration of the project profile). Unset: no work is charged. */
static unsigned long long guest_work_per_entry(void)
{
    const char *text = getenv("N64C_GUEST_NS_PER_ENTRY");
    if (!text || !*text) return 0;
    unsigned long long fx = n64c_clock_parse_work(text);
    if (!fx) {
        fprintf(stderr, "boot-missing: N64C_GUEST_NS_PER_ENTRY must be a positive decimal number, got %s\n", text);
        _Exit(78);
    }
    return fx;
}
void n64c_init(const N64cConfig *cfg)
{
    n64c_sched_reset();
    n64c_system_reset();
    n64c_memory_close();
    n64c_audio_reset();
    n64c_vi_reset();
    n64c_pi_close();
    n64c_cont_reset(cfg);
    n64c_clock_set_work(guest_work_per_entry());
    memset(handlers, 0, sizeof handlers);
    present_fn = NULL;
    rendered_output = NULL;
    if (cfg && cfg->rom_path)
        n64c_pi_open(cfg->rom_path);
}
void n64c_shutdown(void)
{
    n64c_sched_reset();
    n64c_system_reset();
    n64c_memory_close();
    n64c_audio_reset();
    n64c_pi_close();
    n64c_cont_close();
}
void n64c_run_idle(void) { n64c_sched_run_idle(); }
uint32_t n64c_waiting_threads(void) { return n64c_sched_waiting(); }
int n64c_thread_count(void) { return n64c_sched_alive(); }
int n64c_frame(void)
{
    N64C_ZONE(zr1,"game-run");
    n64c_sched_run_idle();
    N64C_ZONE_END(zr1);
    N64C_ZONE(zvi,"vi-retrace");
    n64c_clock_advance_to(n64c_vi_next_retrace_time()); /* the retrace is a device event on the clock */
    N64C_ZONE_END(zvi);
    N64C_ZONE(zr2,"game-run");
    n64c_sched_run_idle();
    N64C_ZONE_END(zr2);
    if (present_fn) {
        fenv_t saved;
        host_fpu_enter(&saved);
        present_fn(n64c_vi_current_fb(), present_user);
        host_fpu_leave(&saved);
    }
    return n64c_sched_alive();
}
