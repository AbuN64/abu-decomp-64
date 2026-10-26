/* SPDX-License-Identifier: GPL-3.0-or-later */
/* VI mode and special features (public osViSetMode/osViSetSpecialFeatures contract) and the RDP buffer handoff. */
#define _GNU_SOURCE
#include "../n64c_internal.h"
#include <fenv.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)
static int rendered;
static void gfx(OSTask *t, void *user) { (void)t; (void)user; rendered++; }
/* Exit status of fn run in a child: the refusals must exit 78, never return. */
static int child_status(void (*fn)(void))
{
    fflush(stdout);
    pid_t pid = fork();
    if (pid == 0) {
        fn();
        _exit(0);
    }
    int status = 0;
    waitpid(pid, &status, 0);
    return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}
static void bad_features(void) { osViSetSpecialFeatures(0x100); }
static void null_mode(void) { osViSetMode(NULL); }
static u64 foreign[4];
static void foreign_buffer(void) { osDpSetNextBuffer(foreign, sizeof foreign); }
static int native_tasks, native_presentations;
static unsigned read_mxcsr(void)
{
    unsigned value;
    __asm__ volatile ("stmxcsr %0" : "=m" (value));
    return value;
}
static void write_mxcsr(unsigned value)
{
    __asm__ volatile ("ldmxcsr %0" : : "m" (value));
}
static void host_defaults(void)
{
    CHECK(fegetexcept() == 0);
    CHECK(fegetround() == FE_TONEAREST);
    CHECK(fetestexcept(FE_ALL_EXCEPT) == 0);
    CHECK((read_mxcsr() & ((1u << 15) | (1u << 6))) == 0);
    /* This would deliver SIGFPE under the guest CPU's invalid-operation trap. */
    CHECK(feraiseexcept(FE_INVALID | FE_INEXACT) == 0);
    CHECK(fesetround(FE_UPWARD) == 0);
}
static void native_task(OSTask *task, void *user)
{
    (void)user;
    host_defaults();
    native_tasks++;
    if (task->type == M_GFXTASK) {
        OSTask nested = {0};
        nested.type = M_AUDTASK;
        osSpTaskStartGo(&nested);
        CHECK(fegetexcept() == 0);
        CHECK(fegetround() == FE_UPWARD);
        CHECK(fetestexcept(FE_INVALID | FE_INEXACT) == (FE_INVALID | FE_INEXACT));
    }
}
static void native_present(void *fb, void *user)
{
    (void)fb;
    (void)user;
    host_defaults();
    native_presentations++;
}
static void guest_fpu_unchanged(void)
{
    CHECK(fegetexcept() == FE_INVALID);
    CHECK(fegetround() == FE_DOWNWARD);
    CHECK(fetestexcept(FE_ALL_EXCEPT) == FE_INEXACT);
    CHECK((read_mxcsr() & ((1u << 15) | (1u << 6))) == ((1u << 15) | (1u << 6)));
}
static void callback_fpu_isolation(void)
{
    fenv_t saved;
    CHECK(fegetenv(&saved) == 0);
    CHECK(fedisableexcept(FE_ALL_EXCEPT) >= 0);
    CHECK(feclearexcept(FE_ALL_EXCEPT) == 0);
    CHECK(fesetround(FE_DOWNWARD) == 0);
    CHECK(feraiseexcept(FE_INEXACT) == 0);
    CHECK(feenableexcept(FE_INVALID) >= 0);
    write_mxcsr(read_mxcsr() | (1u << 15) | (1u << 6));
    n64c_set_task_handler(M_GFXTASK, native_task, NULL);
    n64c_set_task_handler(M_AUDTASK, native_task, NULL);
    n64c_set_present(native_present, NULL);
    OSTask task = {0};
    task.type = M_GFXTASK;
    osSpTaskStartGo(&task);
    CHECK(native_tasks == 2);
    guest_fpu_unchanged();
    (void)n64c_frame();
    CHECK(native_presentations == 1);
    guest_fpu_unchanged();
    CHECK(fesetenv(&saved) == 0);
}

int main(void)
{
    n64c_init(NULL);
    CHECK(n64c_vi_mode() == NULL && n64c_vi_control() == 0 && n64c_vi_rows() == 0);
    OSViMode mode;
    memset(&mode, 0, sizeof mode);
    mode.type = 2;
    mode.comRegs.ctrl = 0x0000311Eu; /* 16-bit, gamma dither, gamma, divot, anti-alias mode 1 */
    mode.comRegs.width = 320;
    mode.fldRegs[1].origin = 640;
    osViSetMode(&mode);
    mode.comRegs.width = 1; /* the VI keeps its own copy */
    CHECK(n64c_vi_mode() && n64c_vi_mode()->comRegs.width == 320 && n64c_vi_mode()->fldRegs[1].origin == 640);
    CHECK(n64c_vi_control() == 0x311Eu);
    osViSetSpecialFeatures(OS_VI_DITHER_FILTER_ON);
    CHECK(n64c_vi_control() == 0x1301Eu); /* dither filter selects anti-alias mode 0 */
    osViSetSpecialFeatures(OS_VI_DIVOT_OFF);
    osViSetSpecialFeatures(OS_VI_GAMMA_OFF);
    CHECK(n64c_vi_control() == 0x13006u);
    osViSetSpecialFeatures(OS_VI_DITHER_FILTER_OFF);
    CHECK(n64c_vi_control() == 0x3106u); /* the mode's own anti-alias mode comes back */
    osViSetSpecialFeatures(OS_VI_GAMMA_ON | OS_VI_GAMMA_OFF | OS_VI_DIVOT_ON);
    CHECK(n64c_vi_control() == 0x3116u); /* OFF wins over ON in one call */
    CHECK(n64c_vi_mode()->comRegs.ctrl == 0x311Eu);
    /* Rows shown: half-lines 57..498 at scale 1.0 are 220.5 lines (221 rows); the hi-res field at scale 0x68B shows
     * 218.5 lines x 1.636 (358 rows); osViSetYScale multiplies. */
    CHECK(n64c_vi_rows() == 0);
    mode.fldRegs[0].vStart = 57u << 16 | 498u;
    mode.fldRegs[0].yScale = 0x400u;
    osViSetMode(&mode);
    CHECK(n64c_vi_rows() == 221);
    mode.fldRegs[0].vStart = 58u << 16 | 495u;
    mode.fldRegs[0].yScale = 0x0200068Bu;
    osViSetMode(&mode);
    CHECK(n64c_vi_rows() == 358);
    osViSetYScale(0.5f);
    CHECK(n64c_vi_rows() == 179);
    osViSetYScale(1.0f);
    CHECK(child_status(bad_features) == 78);
    CHECK(child_status(null_mode) == 78);

    /* The RDP may only be handed the output of the graphics task the native RSP already rendered. */
    n64c_set_task_handler(M_GFXTASK, gfx, NULL);
    static u64 output[16], output_size;
    OSTask task;
    memset(&task, 0, sizeof task);
    task.type = M_GFXTASK;
    task.output_buff = output;
    task.output_buff_size = &output_size;
    CHECK(child_status(foreign_buffer) == 78);
    osSpTaskStartGo(&task);
    CHECK(rendered == 1);
    CHECK(osDpSetNextBuffer(output, output_size) == 0);
    CHECK(child_status(foreign_buffer) == 78);
    callback_fpu_isolation();
    n64c_shutdown();
    printf("n64c_vi_dp: %s\n", fails ? "FAIL" : "ok");
    return fails ? 1 : 0;
}
