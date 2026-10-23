/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Public original-source frame capture. No replay inputs or fabricated tasks. Graphics tasks run
 * through the Fast3D renderer (fast3d/n64c_f3d.h) on an offscreen window; the capture reads back
 * the colour image the VI was last given. */
#include "frame.h"
#include "stop.h"
#include "trace.h"
#include "n64c_f3d.h"
#include "n64c_memory.h"
#include "n64c_diag.h"
#include "n64c_tas.h"
#include "n64c_dltrace.h"
#include "n64c_audio.h"
#include "audio_abi/n64a_abi.h"
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>

/* Movie comparison shots are small: enough to see a wrong screen, cheap to write every few polls. */
#define TAS_SHOT_WIDTH 320
#define TAS_SHOT_HEIGHT 240

static struct {
    u32 tasks,audio_tasks,ram_bytes,shown;
    u32 submitted,gfx_submitted,progress_tasks;
    int closed;
    u64 commands;
    void *presented;
    uint8_t *ram;
    N64aAbi *audio;
    N64cF3d *f3d;
    N64cGfxMem memory;
} frame;

/* A task the native handlers refuse ends the boot; run-on (the walk's diagnostic boot) records it and drops the task. */
static void task_stop(const char *subject,const char *message)
{
    if (n64c_diag_stop && n64c_diag_stop(subject,1)) return;
    fprintf(stderr,"boot-frame: %s\n",message);
    _Exit(78);
}
/* Capture the first submitted list before the renderer or SP completion can release it.
 * Oracle DL files contain little-endian 32-bit words, including unmodified addresses. */
static void first_graphics(const OSTask *task)
{
    fprintf(stderr,"boot-gfx: first submit seq=%u retrace=%u data=0x%08x bytes=%u audio_before=%u\n",
            frame.submitted,osViGetRetraceCount(),task ? (u32)(uintptr_t)task->data_ptr : 0,
            task ? task->data_size : 0,frame.audio_tasks);
    if (n64c_trace_event) n64c_trace_event("first graphics submission");
    const char *path=getenv("N64C_FIRST_GFX_DL");
    if (!path || !*path) return;
    u32 physical=task ? n64c_memory_physical(task->data_ptr) : 0;
    const unsigned char *data=task && task->data_ptr && task->data_size && !(task->data_size%8)
        ? n64c_memory_ram(physical,task->data_size) : NULL;
    FILE *out=data ? fopen(path,"wbx") : NULL;
    int ok=out!=NULL;
    for (u32 i=0;ok && i<task->data_size;i+=4) {
        u32 word;memcpy(&word,data+i,4);
        unsigned char le[4]={(unsigned char)word,(unsigned char)(word>>8),
                            (unsigned char)(word>>16),(unsigned char)(word>>24)};
        ok=fwrite(le,1,4,out)==4;
    }
    if (out && fclose(out)) ok=0;
    if (!ok) {
        fprintf(stderr,"boot-frame: first graphics dump failed: %s\n",path);
        fflush(stderr);
        _Exit(78);
    }
}
static void graphics(OSTask *task,void *user)
{
    (void)user;
    frame.submitted++;
    if (!frame.gfx_submitted++) first_graphics(task);
    if (!task || !task->data_ptr || !task->data_size || task->data_size%8) {
        task_stop("gfx-task-invalid","invalid graphics task");
        return;
    }
    N64cDlStats stats;
    FILE *draw_trace=n64c_tas_draw_trace();
    if (draw_trace) n64c_dltrace_begin(draw_trace);
    int commands=n64c_f3d_run(frame.f3d,&frame.memory,(u32)(uintptr_t)task->data_ptr,&stats);
    if (draw_trace) {
        n64c_dltrace_begin(NULL);
        fclose(draw_trace);
    }
    if (commands<=0) {
        task_stop("gfx-task-refused","graphics renderer refused task");
        return;
    }
    if (stats.unknown) {
        char subject[32],message[64];
        snprintf(subject,sizeof subject,"gfx-opcode-0x%02x",stats.unknown_op);
        snprintf(message,sizeof message,"unhandled graphics opcode 0x%02x",stats.unknown_op);
        task_stop(subject,message);
        return;
    }
    if (!frame.tasks && n64c_trace_event) n64c_trace_event("first graphics task");
    frame.tasks++;
    frame.commands+=(u32)commands;
}
/* The game's own audio C builds the command list; the open audio-ABI core runs it
 * against RDRAM and leaves PCM where the AI adapter reads it. Unknown commands refuse. */
static void audio(OSTask *task,void *user)
{
    (void)user;
    frame.submitted++;
    u32 list=task && task->data_ptr ? n64c_memory_physical(task->data_ptr) : 0;
    if (!list || !task->data_size || task->data_size%8 || list>frame.ram_bytes ||
        task->data_size>frame.ram_bytes-list) {
        task_stop("audio-task-invalid","invalid audio task");
        return;
    }
    unsigned before=n64a_abi_warnings(frame.audio);
    if (n64a_abi_run(frame.audio,frame.ram,list,task->data_size)!=0 ||
        n64a_abi_warnings(frame.audio)!=before) {
        task_stop("audio-task-refused","audio ABI refused task");
        return;
    }
    if (!frame.audio_tasks && n64c_trace_event) n64c_trace_event("first audio task");
    frame.audio_tasks++;
}
static void unsupported_task(OSTask *task,void *user)
{
    (void)user;
    char subject[32],message[64];
    snprintf(subject,sizeof subject,"task-type-%u",(unsigned)task->type);
    snprintf(message,sizeof message,"task type %u has no native handler",(unsigned)task->type);
    task_stop(subject,message);
}
/* Every retrace the window shows what the VI shows: the colour image it was last given (black while
 * it is blanked or that image was never drawn). */
static void present(void *fb,void *user)
{
    (void)user;
    frame.presented=fb;
    u32 physical=fb ? n64c_memory_physical(fb) : 0;
    if (!n64c_f3d_present(frame.f3d,physical)) frame.closed=1;
    /* Only new, successfully rendered work across a frame boundary proves
     * progress. An idle VI repaint must not conceal a blocked game thread. */
    if (!frame.closed && frame.tasks!=frame.progress_tasks) {
        frame.progress_tasks=frame.tasks;
        if (n64c_diag_rendered_frame) n64c_diag_rendered_frame();
    }
    if (fb && frame.tasks && !frame.shown++)
        fprintf(stderr,"boot-step: first presentation retrace=%u surface=0x%08x tasks=%u\n",
                osViGetRetraceCount(),physical,frame.tasks);
}
static void view_write(int force);
/* What the window shows once the run has stopped: the VI's last image under the stop's own status text,
 * in both buffers of the double-buffered window, so it stays on screen while a debugger holds the process. */
static void show_stop(const char *symbol)
{
    char text[256];
    snprintf(text,sizeof text,"BOOT STOP\n%s\nRETRACE %u TASKS %u",symbol ? symbol : "?",
             osViGetRetraceCount(),frame.tasks);
    view_write(1);
    u32 physical=frame.presented ? n64c_memory_physical(frame.presented) : 0;
    for (int i=0;i<2;i++) (void)n64c_f3d_present_text(frame.f3d,physical,text);
    fprintf(stderr,"boot-step: stop shown: %s\n",symbol ? symbol : "?");
    if (n64c_trace_summary) n64c_trace_summary();
    fflush(stderr);
}
/* Live stop hook. The window belongs to the host thread: a stop on a guest thread parks that thread and the
 * live loop shows it when the retrace returns; a stop on the host thread (original boot) shows it at once. */
static void live_stop(const char *symbol)
{
    if (n64c_sched_on_guest()) n64c_sched_halt();
    show_stop(symbol);
}
static struct {
    const char *shaders,*settings;
    int offscreen;
} host;
void n64c_boot_frame_host(const char *shaders,const char *settings,int offscreen)
{
    host.shaders=shaders;host.settings=settings;host.offscreen=offscreen;
}
int n64c_boot_frame_start(u32 width,u32 height,u32 ram_bytes,int audio_abi,u32 window_width,u32 window_height)
{
    if (window_width>4096 || window_height>4096 || !window_width!=!window_height) return 0;
    if (!width || !height || width>4096 || height>4096) return 0;
    if (audio_abi<-1 || audio_abi>N64A_FLAVOUR_NAUDIO) return 0;
    void *base=n64c_memory_ram(0,ram_bytes);
    if (!base || !ram_bytes) return 0;
    n64c_boot_frame_close();
    frame.ram=base;frame.ram_bytes=ram_bytes;
    if (audio_abi>=0 && !(frame.audio=n64a_abi_create((N64aFlavour)audio_abi))) return 0;
    const int live=window_width!=0;
    const N64cF3dConfig config={.window_w=live ? window_width : width,.window_h=live ? window_height : height,
                                .fb_w=width,.fb_h=height,.software=!live || host.offscreen,
                                .offscreen=!live || host.offscreen,.shaders=host.shaders,.settings=host.settings};
    if (!(frame.f3d=n64c_f3d_open(&config))) { n64c_boot_frame_close();return 0; }
    frame.memory=(N64cGfxMem){base,ram_bytes-1,0};
    n64c_set_task_handler(M_GFXTASK,graphics,NULL);
    n64c_set_task_handler(M_AUDTASK,frame.audio ? audio : unsupported_task,NULL);
    n64c_set_present(present,NULL);
    if (live) n64c_boot_stop_hook=live_stop;
    return 1;
}
/* The VI shows whichever color image it was given; double buffering presents an earlier rendered surface,
 * never the last one by assumption. Reads it back scaled to width x height (caller frees), NULL when never rendered. */
static uint32_t *read_presented(u32 width,u32 height,int quiet)
{
    u32 physical=n64c_memory_physical(frame.presented);
    uint32_t *pixels=malloc((size_t)width*height*sizeof *pixels);
    if (!pixels) return NULL;
    if (!n64c_f3d_read(frame.f3d,physical,pixels,width,height)) {
        if (!quiet) fprintf(stderr,"boot-frame: presented surface 0x%08x was never rendered\n",physical);
        free(pixels);
        return NULL;
    }
    return pixels;
}
/* Writes the presented image as a binary PPM; never replaces an existing file. */
static int write_presented(const char *path,u32 width,u32 height)
{
    uint32_t *pixels=read_presented(width,height,0);
    if (!pixels) return 0;
    FILE *out=fopen(path,"wbx");
    if (!out) { free(pixels);perror("boot-frame output");return 0; }
    int ok=fprintf(out,"P6\n%u %u\n255\n",width,height)>0;
    for (size_t i=0;i<(size_t)width*height && ok;i++) {
        unsigned char rgb[3]={(unsigned char)pixels[i],(unsigned char)(pixels[i]>>8),(unsigned char)(pixels[i]>>16)};
        ok=fwrite(rgb,1,3,out)==3;
    }
    free(pixels);
    if (fclose(out)) ok=0;
    if (!ok) (void)remove(path);
    return ok;
}
static void put_le(unsigned char *p,uint32_t v,int bytes)
{
    for (int i=0;i<bytes;i++) p[i]=(unsigned char)(v>>(8*i));
}
int n64c_write_bmp(const char *path,const uint32_t *pixels,u32 width,u32 height)
{
    return n64c_write_bmp_tagged(path,pixels,width,height,0);
}
int n64c_write_bmp_tagged(const char *path,const uint32_t *pixels,u32 width,u32 height,u32 tag)
{
    if (!path || !pixels || !width || !height || width>4096 || height>4096) return 0;
    const u32 row=(width*3+3)&~3u,size=54+row*height;
    unsigned char head[54]={'B','M'};
    put_le(head+2,size,4);put_le(head+6,tag,4);put_le(head+10,54,4);put_le(head+14,40,4);
    put_le(head+18,width,4);put_le(head+22,height,4);put_le(head+26,1,2);put_le(head+28,24,2);
    put_le(head+34,row*height,4);
    unsigned char *line=calloc(1,row);
    FILE *out=line ? fopen(path,"wb") : NULL;
    int ok=out && fwrite(head,1,sizeof head,out)==sizeof head;
    for (u32 y=height;ok && y-->0;) { /* bottom-up rows, BGR */
        const uint32_t *src=pixels+(size_t)y*width;
        for (u32 x=0;x<width;x++) {
            line[3*x]=(unsigned char)(src[x]>>16);line[3*x+1]=(unsigned char)(src[x]>>8);line[3*x+2]=(unsigned char)src[x];
        }
        ok=fwrite(line,1,row,out)==row;
    }
    free(line);
    if (out && fclose(out)) ok=0;
    if (!ok && out) (void)remove(path);
    return ok;
}
/* N64C_VIEW: the latest presented picture for a live viewer, replaced whole (written beside it, then renamed) at
 * most N64C_VIEW_FPS times a second of wall time (default 15, so a turbo replay pays for ~15 pictures a second, not
 * one per retrace; a played session asks for 60). The BMP's reserved header word carries n64c_tas_live_seq.
 * Host-only: guest RAM and the virtual clock are untouched, so the per-poll RAM hashes are the same with it on. */
#define VIEW_FPS_DEFAULT 15
static struct {
    const char *path;
    char tmp[1024];
    struct timespec last;
    long long interval_ns;
    int written,failed;
} view;
int n64c_parse_view_fps(const char *text,u32 *fps)
{
    if (!text || !*text) { *fps=VIEW_FPS_DEFAULT; return 1; }
    char *end;
    unsigned long v=strtoul(text,&end,10);
    if (*end || v<1 || v>120) return 0;
    *fps=(u32)v;
    return 1;
}
static void view_open(void)
{
    const char *path=getenv("N64C_VIEW");
    view=(typeof(view)){0};
    if (!path || !*path) return;
    u32 fps;
    if (!n64c_parse_view_fps(getenv("N64C_VIEW_FPS"),&fps)) {
        fprintf(stderr,"boot-frame: N64C_VIEW_FPS must be 1..120, got %s; using %d\n",getenv("N64C_VIEW_FPS"),VIEW_FPS_DEFAULT);
        fps=VIEW_FPS_DEFAULT;
    }
    /* A little under the period, so a picture due on a retrace is not pushed to the next one by timer jitter. */
    view.interval_ns=1000000000ll/fps-2000000ll;
    if (snprintf(view.tmp,sizeof view.tmp,"%s.tmp",path)>=(int)sizeof view.tmp) {
        fprintf(stderr,"boot-frame: N64C_VIEW path too long\n");
        return;
    }
    view.path=path;
}
static void view_write(int force)
{
    if (!view.path || view.failed || !frame.presented || !frame.tasks) return;
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC,&now);
    if (!force && view.written &&
        (long long)(now.tv_sec-view.last.tv_sec)*1000000000ll+(now.tv_nsec-view.last.tv_nsec)<view.interval_ns) return;
    /* Early on the VI can show a surface not drawn yet: no picture this time, try again next interval. */
    uint32_t *pixels=read_presented(TAS_SHOT_WIDTH,TAS_SHOT_HEIGHT,1);
    if (!pixels) return;
    view.last=now;view.written=1;
    int ok=n64c_write_bmp_tagged(view.tmp,pixels,TAS_SHOT_WIDTH,TAS_SHOT_HEIGHT,(u32)n64c_tas_live_seq()) &&
           !rename(view.tmp,view.path);
    free(pixels);
    if (!ok) {
        view.failed=1; /* one warning, never a stop: the viewer is not the run */
        fprintf(stderr,"n64c-view: cannot write %s; view off\n",view.path);
    }
}
int n64c_boot_frame_capture(const char *path,u32 retrace,u32 width,u32 height)
{
    if (!path || !retrace || !width || !height || width>4096 || height>4096) return 0;
    while (osViGetRetraceCount()<retrace) {
        if (!n64c_frame()) {
            fprintf(stderr,"boot-frame: no live original threads\n");
            return 0;
        }
    }
    if (osViGetRetraceCount()!=retrace || !frame.tasks || !frame.presented) {
        fprintf(stderr,"boot-frame: no accepted presentation at retrace %u\n",retrace);
        return 0;
    }
    if (!write_presented(path,width,height)) return 0;
    u32 physical=n64c_memory_physical(frame.presented);
    fprintf(stderr,"boot-frame: retrace=%u tasks=%u audio=%u commands=%llu surface=0x%08x\n",
            retrace,frame.tasks,frame.audio_tasks,(unsigned long long)frame.commands,physical);
    return 1;
}
int n64c_retrace_due(const struct timespec *start,u32 n,u32 speed,struct timespec *due)
{
    if (!speed) return 0;
    uint64_t ns=(uint64_t)start->tv_nsec+(uint64_t)n*50050000u/(3u*(uint64_t)speed);
    due->tv_sec=start->tv_sec+(time_t)(ns/1000000000u);
    due->tv_nsec=(long)(ns%1000000000u);
    return 1;
}
int n64c_parse_speed(const char *text,u32 *speed)
{
    if (!text || !*text) { *speed=1; return 1; }
    char *end;
    unsigned long v=strtoul(text,&end,10);
    if (*end || v>1000) return 0;
    *speed=(u32)v;
    return 1;
}
int n64c_parse_pak(const char *text,u32 profile_bytes,u32 *ram)
{
    if (!text || !*text) *ram=profile_bytes;
    else if (!strcmp(text,"on")) *ram=0x800000u;
    else if (!strcmp(text,"off")) *ram=0x400000u;
    else return 0;
    return 1;
}
static void wait_retrace(const struct timespec *start,u32 n,u32 speed)
{
    struct timespec due;
    if (!n64c_retrace_due(start,n,speed,&due)) return;
    while (clock_nanosleep(CLOCK_MONOTONIC,TIMER_ABSTIME,&due,NULL)!=0) {}
}
int n64c_boot_frame_live(void)
{
    u32 speed;
    if (!n64c_parse_speed(getenv("N64C_SPEED"),&speed)) {
        fprintf(stderr,"boot-frame: N64C_SPEED must be 0..1000, got %s\n",getenv("N64C_SPEED"));
        return 0;
    }
    view_open();
    const char *audio_out=getenv("N64C_AUDIO_OUT");
    if (audio_out && *audio_out && !n64c_audio_tap(audio_out))
        fprintf(stderr,"boot-frame: cannot write N64C_AUDIO_OUT %s\n",audio_out);
    struct timespec start;
    clock_gettime(CLOCK_MONOTONIC,&start);
    u32 base=0; /* retraces before the pacing restarted (a run taken over by live input plays at 1x from there) */
    for (u32 n=1;!frame.closed;n++) {
        if (speed!=1 && n64c_tas_live_taken()) {
            fprintf(stderr,"boot-frame: live input: real time from retrace %u\n",osViGetRetraceCount());
            speed=1;base=n-1;clock_gettime(CLOCK_MONOTONIC,&start);
        }
        if (n64c_tas_live_taken() && view.path) view.interval_ns=1000000000ll/60-2000000ll;
        const int alive=n64c_frame();
        if (n64c_sched_halted()) {
            show_stop(n64c_boot_stop_symbol);
            if (n64c_coverage_dump) n64c_coverage_dump();
            n64c_boot_stopped();
        }
        if (!alive) {
            fprintf(stderr,"boot-frame: no live original threads at retrace %u\n",osViGetRetraceCount());
            return 0;
        }
        long shot=n64c_tas_take_shot();
        if (shot>=0 && frame.presented && frame.tasks) {
            char path[1024];
            snprintf(path,sizeof path,"%s/poll-%06ld.ppm",n64c_tas_shot_dir(),shot);
            if (!write_presented(path,TAS_SHOT_WIDTH,TAS_SHOT_HEIGHT)) fprintf(stderr,"n64c-tas: shot %ld failed\n",shot);
        }
        view_write(0);
        if (n%600==0)
            fprintf(stderr,"boot-step: retrace=%u tasks=%u audio=%u commands=%llu\n",osViGetRetraceCount(),
                    frame.tasks,frame.audio_tasks,(unsigned long long)frame.commands);
        wait_retrace(&start,n-base,speed);
    }
    fprintf(stderr,"boot-frame: window closed at retrace %u\n",osViGetRetraceCount());
    return 1;
}
void n64c_boot_frame_close(void)
{
    n64c_boot_stop_hook=NULL;
    n64c_set_present(NULL,NULL);
    n64c_set_task_handler(M_GFXTASK,NULL,NULL);
    n64c_set_task_handler(M_AUDTASK,NULL,NULL);
    n64a_abi_destroy(frame.audio);
    n64c_f3d_close(frame.f3d);
    frame=(typeof(frame)){0};
}
