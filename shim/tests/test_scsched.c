/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Scheduler goldens, derived by hand from the public contract: audio runs before graphics, a task owes the
 * RSP and RDP until both report, the last graphics task swaps the buffer and tells its owner, every client
 * hears each retrace, a removed client does not. */
#include "../n64c_os.h"
#include "../n64c_internal.h"
#include <stdio.h>
#include <string.h>

static char logbuf[256];
static int fails;
static void L(const char *s) { strncat(logbuf, s, sizeof logbuf - strlen(logbuf) - 1); }
static void expect(const char *name, const char *want)
{
    if (strcmp(logbuf, want)) {
        printf("FAIL %s\n  want: %s\n  got:  %s\n", name, want, logbuf);
        fails++;
    } else
        printf("ok   %s: %s\n", name, logbuf);
    logbuf[0] = 0;
}
static void check(const char *name, int ok)
{
    if (!ok) {
        printf("FAIL %s\n", name);
        fails++;
    } else
        printf("ok   %s\n", name);
}
static OSSched sc;
static OSScClient client, client2;
static OSMesgQueue cq, cq2, done;
static OSMesg cbuf[8], cbuf2[8], dbuf[8];
static int hooks;
static void gfx_handler(OSTask *t, void *u) { (void)t; (void)u; L("g"); }
static void aud_handler(OSTask *t, void *u) { (void)t; (void)u; L("a"); }
static void hook(void) { hooks++; }
/* Guest work: the clock when a retrace step begins, ends and starts a task. */
static OSTime retrace_at, retraced_at, started_at, rsp_done_at, rdp_done_at;
static int pre_retrace(void) { retrace_at = n64c_clock_now(); return 0; }
static void retraced(void) { retraced_at = n64c_clock_now(); }
static void started(OSSched *s, OSScTask *t, N64cScTaskEvent e)
{
    (void)s, (void)t;
    if (e == N64C_SC_TASK_START)
        started_at = n64c_clock_now();
    else if (e == N64C_SC_RSP_DONE)
        rsp_done_at = n64c_clock_now();
    else if (e == N64C_SC_RDP_DONE)
        rdp_done_at = n64c_clock_now();
}
static OSScTask task(u32 type, u32 flags, void *fb)
{
    OSScTask t;
    memset(&t, 0, sizeof t);
    t.list.type = type;
    t.flags = flags;
    t.framebuffer = fb;
    t.msgQ = &done;
    t.msg.data32 = type * 100 + (flags & OS_SC_LAST_TASK ? 1 : 0);
    return t;
}
static void submit(OSScTask *t)
{
    osSendMesg(&sc.commandQueue, (OSMesg){.ptr = t}, OS_MESG_NOBLOCK);
    osSendMesg(&sc.interruptQueue, (OSMesg){.data32 = N64C_SC_CMD_MSG}, OS_MESG_NOBLOCK);
}
static int drain_done(void)
{
    OSMesg m;
    int n = 0;
    while (osRecvMesg(&done, &m, OS_MESG_NOBLOCK) == 0)
        n = n * 1000 + (int)m.data32;
    return n;
}
#define GFX_LAST (OS_SC_NEEDS_RSP | OS_SC_NEEDS_RDP | OS_SC_LAST_TASK | OS_SC_SWAPBUFFER)
/* The RSP finishes a task a fixed guest time after it starts (graphics 8 ms, audio 2 ms): let the clock run past
 * that, a chained task included, and let the woken scheduler thread act. */
static void settle(void)
{
    for (int i = 0; i < 3; i++) {
        n64c_run_idle();
        n64c_clock_advance_to(n64c_clock_now() + OS_CPU_COUNTER / 125);
    }
    n64c_run_idle();
}

int main(void)
{
    N64cConfig cfg = {0};
    char fba[4], fbb[4];
    n64c_init(&cfg);
    n64c_set_task_handler(M_GFXTASK, gfx_handler, 0);
    n64c_set_task_handler(M_AUDTASK, aud_handler, 0);
    osCreateMesgQueue(&cq, cbuf, 8);
    osCreateMesgQueue(&cq2, cbuf2, 8);
    osCreateMesgQueue(&done, dbuf, 8);
    osCreateScheduler(&sc, 0, 40, 0, 1);
    n64c_sc_set_retrace_hook(hook);
    osScAddClient(&sc, &client, &cq);
    osScAddClient(&sc, &client2, &cq2);
    n64c_run_idle();
    check("get_cmdq_is_interrupt_queue", osScGetCmdQ(&sc) == &sc.interruptQueue);
    check("client_list_newest_first", sc.clientList == &client2 && client2.next == &client);

    OSScTask g = task(M_GFXTASK, GFX_LAST, fba);
    submit(&g);
    settle();
    expect("last_gfx_task_runs", "g");
    check("last_gfx_done_message", drain_done() == 101);
    check("task_owes_nothing", g.state == 0 && !sc.curRSPTask && !sc.curRDPTask);
    check("swap_requested", osViGetNextFramebuffer() == fba);

    OSScTask first = task(M_GFXTASK, OS_SC_NEEDS_RSP | OS_SC_NEEDS_RDP, fbb);
    submit(&first);
    settle();
    expect("first_of_two_runs", "g");
    check("first_of_two_silent", drain_done() == 0 && osViGetNextFramebuffer() == fba);

    /* both queued behind one command: audio goes first, then graphics once the RSP frees */
    OSScTask g2 = task(M_GFXTASK, GFX_LAST, fbb), a = task(M_AUDTASK, OS_SC_NEEDS_RSP, 0);
    osSendMesg(&sc.commandQueue, (OSMesg){.ptr = &g2}, OS_MESG_NOBLOCK);
    osSendMesg(&sc.commandQueue, (OSMesg){.ptr = &a}, OS_MESG_NOBLOCK);
    osSendMesg(&sc.interruptQueue, (OSMesg){.data32 = N64C_SC_CMD_MSG}, OS_MESG_NOBLOCK);
    settle();
    expect("audio_before_graphics", "ag");
    check("both_done_audio_first", drain_done() == 200 * 1000 + 101);

    /* a task posted straight to the queue osScGetCmdQ returned */
    OSScTask direct = task(M_AUDTASK, OS_SC_NEEDS_RSP, 0);
    osSendMesg(osScGetCmdQ(&sc), (OSMesg){.ptr = &direct}, OS_MESG_NOBLOCK);
    settle();
    expect("direct_post_runs", "a");
    check("direct_post_done", drain_done() == 200);

    /* retrace: every client hears the scheduler's retrace record; the hook runs once */
    OSMesg m;
    while (osRecvMesg(&cq, &m, OS_MESG_NOBLOCK) == 0) {} /* retraces that came due while the RSP tasks ran */
    while (osRecvMesg(&cq2, &m, OS_MESG_NOBLOCK) == 0) {}
    const int hooks0 = hooks;
    const u32 frames0 = sc.frameCount;
    n64c_frame();
    check("client_hears_retrace", osRecvMesg(&cq, &m, OS_MESG_NOBLOCK) == 0 && m.ptr == &sc && *(s32 *)m.ptr == 1);
    check("second_client_hears_retrace", osRecvMesg(&cq2, &m, OS_MESG_NOBLOCK) == 0 && m.ptr == &sc);
    check("hook_ran_once", hooks == hooks0 + 1);
    check("frame_counted", sc.frameCount == frames0 + 1);

    /* a task waiting at retrace is started there */
    OSScTask g3 = task(M_GFXTASK, GFX_LAST, fba);
    osSendMesg(&sc.commandQueue, (OSMesg){.ptr = &g3}, OS_MESG_NOBLOCK);
    n64c_frame();
    settle();
    expect("retrace_drains_commands", "g");
    check("retrace_task_done", drain_done() == 101);

    osScRemoveClient(&sc, &client2);
    while (osRecvMesg(&cq, &m, OS_MESG_NOBLOCK) == 0) {}
    while (osRecvMesg(&cq2, &m, OS_MESG_NOBLOCK) == 0) {}
    n64c_frame();
    check("removed_client_silent", osRecvMesg(&cq2, &m, OS_MESG_NOBLOCK) == -1);
    check("remaining_client_hears", osRecvMesg(&cq, &m, OS_MESG_NOBLOCK) == 0);
    check("list_after_remove", sc.clientList == &client && client.next == NULL);
    /* A queued swap and the displayed framebuffer both exclude rendering.
     * Audio still completes; the original task stays queued exactly once and
     * resumes on the retrace that releases its buffer. */
    char fbc[4];
    osViSwapBuffer(fbb);
    OSScTask pending = task(M_GFXTASK, OS_SC_NEEDS_RSP | OS_SC_NEEDS_RDP | OS_SC_LAST_TASK, fbb);
    OSScTask audio = task(M_AUDTASK, OS_SC_NEEDS_RSP, NULL);
    submit(&pending);
    submit(&audio);
    settle();
    expect("audio_bypasses_pending_framebuffer", "a");
    check("pending_buffer_keeps_task_queued", sc.gfxListHead == &pending && sc.gfxListTail == &pending && !pending.next);
    check("blocked_graphics_has_no_completion", drain_done() == 200);
    n64c_frame();
    expect("displayed_buffer_remains_blocked", "");
    check("displayed_buffer_still_owned", osViGetCurrentFramebuffer() == fbb && sc.gfxListHead == &pending);
    osViSwapBuffer(fbc);
    n64c_frame();
    expect("retrace_releases_waiting_graphics", "g");
    settle();
    check("released_task_completes_once", drain_done() == 101 && !sc.gfxListHead && !sc.gfxListTail);
    n64c_frame();
    expect("released_task_not_replayed", "");
    check("no_duplicate_completion", drain_done() == 0);

    /* The sample scheduler's entries, 64 ns (3 Count ticks) each: an idle retrace makes one pass over the lists
     * (__scSchedule); one that takes a task makes a second pass and starts it (__scExec). */
    n64c_clock_set_work(64ull << N64C_WORK_FRAC);
    n64c_sc_set_pre_retrace_hook(pre_retrace);
    n64c_sc_set_retrace_hook(retraced);
    n64c_sc_set_task_hook(started);
    n64c_frame();
    check("idle_retrace_charges_one_pass", retraced_at - retrace_at == 3);
    OSScTask g4 = task(M_GFXTASK, GFX_LAST, fba);
    osSendMesg(&sc.commandQueue, (OSMesg){.ptr = &g4}, OS_MESG_NOBLOCK);
    n64c_frame();
    check("retrace_start_charges_two_passes_and_exec", started_at - retrace_at == 9);
    /* A frame's last graphics task: the RSP's completion, a pass over the lists, then the RDP's completion. The shim
     * charges only its own stand-ins; the game's SetN64_OnFrameComplete is charged by its own entry. */
    settle();
    check("frame_units_charge_only_shim_entries", rdp_done_at - rsp_done_at == 6);
    n64c_shutdown();
    return fails ? 1 : 0;
}
