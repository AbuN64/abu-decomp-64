/* SPDX-License-Identifier: GPL-3.0-or-later */
/* VI mode and special features (public osViSetMode/osViSetSpecialFeatures contract) and the RDP buffer handoff. */
#include "../n64c_internal.h"
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
int main(void)
{
    n64c_init(NULL);
    CHECK(n64c_vi_mode() == NULL && n64c_vi_control() == 0);
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
    n64c_shutdown();
    printf("n64c_vi_dp: %s\n", fails ? "FAIL" : "ok");
    return fails ? 1 : 0;
}
