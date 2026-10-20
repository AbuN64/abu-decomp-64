/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Scheduler goldens, derived by hand from the public contract: audio runs before graphics, a task owes the
 * RSP and RDP until both report, the last graphics task swaps the buffer and tells its owner, every client
 * hears each retrace, a removed client does not. */
#include "../n64c_os.h"
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
    n64c_run_idle();
    expect("last_gfx_task_runs", "g");
    check("last_gfx_done_message", drain_done() == 101);
    check("task_owes_nothing", g.state == 0 && !sc.curRSPTask && !sc.curRDPTask);
    check("swap_requested", osViGetNextFramebuffer() == fba);

    OSScTask first = task(M_GFXTASK, OS_SC_NEEDS_RSP | OS_SC_NEEDS_RDP, fbb);
    submit(&first);
    n64c_run_idle();
    expect("first_of_two_runs", "g");
    check("first_of_two_silent", drain_done() == 0 && osViGetNextFramebuffer() == fba);

    /* both queued behind one command: audio goes first, then graphics once the RSP frees */
    OSScTask g2 = task(M_GFXTASK, GFX_LAST, fbb), a = task(M_AUDTASK, OS_SC_NEEDS_RSP, 0);
    osSendMesg(&sc.commandQueue, (OSMesg){.ptr = &g2}, OS_MESG_NOBLOCK);
    osSendMesg(&sc.commandQueue, (OSMesg){.ptr = &a}, OS_MESG_NOBLOCK);
    osSendMesg(&sc.interruptQueue, (OSMesg){.data32 = N64C_SC_CMD_MSG}, OS_MESG_NOBLOCK);
    n64c_run_idle();
    expect("audio_before_graphics", "ag");
    check("both_done_audio_first", drain_done() == 200 * 1000 + 101);

    /* a task posted straight to the queue osScGetCmdQ returned */
    OSScTask direct = task(M_AUDTASK, OS_SC_NEEDS_RSP, 0);
    osSendMesg(osScGetCmdQ(&sc), (OSMesg){.ptr = &direct}, OS_MESG_NOBLOCK);
    n64c_run_idle();
    expect("direct_post_runs", "a");
    check("direct_post_done", drain_done() == 200);

    /* retrace: every client hears the scheduler's retrace record; the hook runs once */
    n64c_frame();
    OSMesg m;
    check("client_hears_retrace", osRecvMesg(&cq, &m, OS_MESG_NOBLOCK) == 0 && m.ptr == &sc && *(s32 *)m.ptr == 1);
    check("second_client_hears_retrace", osRecvMesg(&cq2, &m, OS_MESG_NOBLOCK) == 0 && m.ptr == &sc);
    check("hook_ran_once", hooks == 1);
    check("frame_counted", sc.frameCount == 1);

    /* a task waiting at retrace is started there */
    OSScTask g3 = task(M_GFXTASK, GFX_LAST, fba);
    osSendMesg(&sc.commandQueue, (OSMesg){.ptr = &g3}, OS_MESG_NOBLOCK);
    n64c_frame();
    n64c_run_idle();
    expect("retrace_drains_commands", "g");
    check("retrace_task_done", drain_done() == 101);

    osScRemoveClient(&sc, &client2);
    osRecvMesg(&cq, &m, OS_MESG_NOBLOCK);
    osRecvMesg(&cq2, &m, OS_MESG_NOBLOCK);
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
    n64c_run_idle();
    expect("audio_bypasses_pending_framebuffer", "a");
    check("pending_buffer_keeps_task_queued", sc.gfxListHead == &pending && sc.gfxListTail == &pending && !pending.next);
    check("blocked_graphics_has_no_completion", drain_done() == 200);
    n64c_frame();
    expect("displayed_buffer_remains_blocked", "");
    check("displayed_buffer_still_owned", osViGetCurrentFramebuffer() == fbb && sc.gfxListHead == &pending);
    osViSwapBuffer(fbc);
    n64c_frame();
    expect("retrace_releases_waiting_graphics", "g");
    check("released_task_completes_once", drain_done() == 101 && !sc.gfxListHead && !sc.gfxListTail);
    n64c_frame();
    expect("released_task_not_replayed", "");
    check("no_duplicate_completion", drain_done() == 0);
    n64c_shutdown();
    return fails ? 1 : 0;
}
