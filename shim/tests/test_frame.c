/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "../decomp/frame.h"
#include "../decomp/stop.h"
#include "../fast3d/n64c_f3d.h"
#include "../n64c_memory.h"
#include "../n64c_diag.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>
static unsigned checks,render_progress;
void n64c_diag_rendered_frame(void) { render_progress++; }
#define CHECK(x) do { checks++;if (!(x)) { fprintf(stderr,"FAIL:%d:%s\n",__LINE__,#x);exit(1); } } while (0)
static N64cTaskFn tasks[3];
static N64cPresentFn presenter;
static unsigned char ram[4096];
static u32 retraces;
static int scenario,dump_test;
void n64c_set_task_handler(u32 kind,N64cTaskFn fn,void *user) { (void)user;tasks[kind]=fn; }
void n64c_set_present(N64cPresentFn fn,void *user) { (void)user;presenter=fn; }
void *n64c_memory_ram(u32 address,u32 size) { return ((!address && size==sizeof ram) || (address && address<=sizeof ram && size<=sizeof ram-address)) ? ram+address : NULL; }
u32 n64c_memory_physical(const void *p) { return (u32)(uintptr_t)p & 0x1fffffffu; }
/* Renderer stand-in: one handle, tasks counted, the presented colour image (physical 0x300) was
 * rendered unless scenario 4. */
static int renderer,window_mode;
static unsigned presents;
static uint32_t shown;
N64cF3d *n64c_f3d_open(const N64cF3dConfig *c)
{
    CHECK(c->fb_w==2 && c->fb_h==2 && !renderer);
    if (window_mode) CHECK(c->window_w==4 && c->window_h==3 && !c->offscreen && !c->software);
    else CHECK(c->window_w==2 && c->window_h==2 && c->offscreen && c->software);
    renderer=1;
    return (N64cF3d *)&renderer;
}
void n64c_f3d_close(N64cF3d *f) { CHECK(!f || (f==(N64cF3d *)&renderer && renderer));renderer=0; }
int n64c_f3d_run(N64cF3d *f,const N64cGfxMem *m,uint32_t address,N64cDlStats *st)
{
    CHECK(f==(N64cF3d *)&renderer && m->base==ram && !m->replay && m->mask==sizeof ram-1 && address==0x100);
    if (dump_test) memset(ram+0x100,0xA5,32); /* completion may recycle the list */
    *st=(N64cDlStats){.commands=4};
    if (scenario==6) { st->unknown=1;st->unknown_op=0x42; }
    return scenario==5 ? -1 : 4;
}
int n64c_f3d_read(N64cF3d *f,uint32_t phys,uint32_t *out,uint32_t w,uint32_t h)
{
    CHECK(f==(N64cF3d *)&renderer && phys==0x300);
    if (scenario==4) return 0;
    for (uint32_t i=0;i<w*h;i++) out[i]=0xff0000ff;
    return 1;
}
/* Window stand-in: records what the VI showed; a live window is closed by its third presentation. */
int n64c_f3d_present(N64cF3d *f,uint32_t phys)
{
    CHECK(f==(N64cF3d *)&renderer);
    shown=phys;presents++;
    return !(window_mode && presents>=3);
}
/* Status frames go to a pipe: the stop ends the (forked) process, the parent reads what was shown. */
static int text_fd=-1;
int n64c_f3d_present_text(N64cF3d *f,uint32_t phys,const char *text)
{
    CHECK(f==(N64cF3d *)&renderer && text_fd>=0);
    char line[128];int n=snprintf(line,sizeof line,"%x:%s|",phys,text);
    CHECK(n>0 && (size_t)n<sizeof line && write(text_fd,line,(size_t)n)==n);
    return 1;
}
/* Scheduler stand-in: scenario 13 is a guest halt (the hook already parked the thread), 14 a host stop. */
static int halted;
int n64c_sched_halted(void) { return halted; }
int n64c_sched_on_guest(void) { return 0; }
void n64c_sched_halt(void) { abort(); }
u32 osViGetRetraceCount(void) { return retraces; }
int n64c_frame(void)
{
    retraces++;
    if (scenario!=2) {
        OSTask task={.type=M_GFXTASK,.data_ptr=(void *)(uintptr_t)0x100,.data_size=32};
        if (scenario==7) task.data_size=3;
        if (scenario>=8 && scenario<=11) task.type=M_AUDTASK;
        if (scenario==9) task.data_size=3;
        if (scenario==10) { task.data_size=8;memcpy(ram+0x100,"\x00\x00\x00\xff\x00\x00\x00\x00",8); } /* opcode 0xff */
        if (scenario==11) task.data_ptr=(void *)(uintptr_t)0x0ff8; /* list runs past RDRAM */
        tasks[task.type](&task,NULL);
        if (scenario==13) { n64c_boot_stop_symbol="func_guest";halted=1; }
        if (scenario==14) n64c_boot_missing("func_host");
        if (scenario==12) { /* a valid audio list (one no-op command) beside the graphics task */
            OSTask sound={.type=M_AUDTASK,.data_ptr=(void *)(uintptr_t)0x200,.data_size=8};
            tasks[M_AUDTASK](&sound,NULL);
        }
    }
    presenter(scenario==3 ? NULL : (void *)(uintptr_t)0xa0000300,NULL);
    return scenario!=1;
}
static void start(int mode)
{
    scenario=mode;retraces=0;presents=0;shown=0;render_progress=0;
    CHECK(n64c_boot_frame_start(2,2,sizeof ram,mode==8 ? -1 : 0,window_mode ? 4 : 0,window_mode ? 3 : 0));
}
int main(int argc,char **argv)
{
    CHECK(argc==2);
    char path[1024];snprintf(path,sizeof path,"%s/frame-%ld.ppm",argv[1],(long)getpid());
    CHECK(!n64c_boot_frame_start(0,2,sizeof ram,0,0,0));
    CHECK(!n64c_boot_frame_start(2,5000,sizeof ram,0,0,0));
    CHECK(!n64c_boot_frame_start(2,2,1,0,0,0));
    CHECK(!n64c_boot_frame_start(2,2,sizeof ram,4,0,0));
    CHECK(!n64c_boot_frame_start(2,2,sizeof ram,0,4,0));
    CHECK(!n64c_boot_frame_start(2,2,sizeof ram,0,5000,3));
    start(0);
    CHECK(!n64c_boot_frame_capture(path,0,2,2));
    CHECK(n64c_boot_frame_capture(path,3,2,2));
    CHECK(retraces==3 && presents==3 && shown==0x300);
    FILE *f=fopen(path,"rb");CHECK(f!=NULL);
    unsigned char data[24]={0};size_t n=fread(data,1,sizeof data,f);fclose(f);
    CHECK(n==23 && !memcmp(data,"P6\n2 2\n255\n",11));
    for (unsigned i=11;i<23;i+=3) CHECK(data[i]==255 && data[i+1]==0 && data[i+2]==0);
    CHECK(!n64c_boot_frame_capture(path,3,2,2)); /* never overwrite accepted evidence */
    CHECK(unlink(path)==0);
    char dlpath[1024];snprintf(dlpath,sizeof dlpath,"%s/first-%ld.dl",argv[1],(long)getpid());
    CHECK(setenv("N64C_FIRST_GFX_DL",dlpath,1)==0);
    uint32_t commands[8]={0xdb060000,0,0xde000000,0x800d0f10,0xdf000000,0,0,0};
    memcpy(ram+0x100,commands,sizeof commands);dump_test=1;
    start(0);CHECK(n64c_boot_frame_capture(path,3,2,2));CHECK(unlink(path)==0);
    FILE *dl=fopen(dlpath,"rb");CHECK(dl!=NULL);
    unsigned char dumped[33];size_t bytes=fread(dumped,1,sizeof dumped,dl);CHECK(fclose(dl)==0);
    CHECK(bytes==32);
    for (unsigned i=0;i<32;i++) CHECK(dumped[i]==(unsigned char)(commands[i/4]>>(8*(i%4))));
    CHECK(ram[0x100]==0xA5); /* snapshot precedes parsing and only the first task is written */
    pid_t duplicate=fork();CHECK(duplicate>=0);
    if (!duplicate) { start(0);(void)n64c_boot_frame_capture(path,3,2,2);_Exit(1); }
    int duplicate_status;CHECK(waitpid(duplicate,&duplicate_status,0)==duplicate);
    CHECK(WIFEXITED(duplicate_status) && WEXITSTATUS(duplicate_status)==78);
    CHECK(unlink(dlpath)==0);CHECK(unsetenv("N64C_FIRST_GFX_DL")==0);dump_test=0;
    for (int mode=1;mode<=4;mode++) {
        start(mode);CHECK(!n64c_boot_frame_capture(path,3,2,2));CHECK(access(path,F_OK)!=0);
        if (mode==3) CHECK(presents==3 && shown==0); /* the VI was blanked: the window shows black */
    }
    window_mode=1;
    start(0);CHECK(n64c_boot_frame_live()==1 && presents==3 && retraces==3 && shown==0x300);
    start(1);CHECK(n64c_boot_frame_live()==0 && retraces==1);
    n64c_boot_frame_close();
    CHECK(!n64c_boot_stop_hook);
    /* A live stop shows itself in both window buffers, then ends in the debugger hold point. */
    const char *want[2]={"300:BOOT STOP\nfunc_guest\nRETRACE 1 TASKS 1|300:BOOT STOP\nfunc_guest\nRETRACE 1 TASKS 1|",
                         "0:BOOT STOP\nfunc_host\nRETRACE 1 TASKS 1|0:BOOT STOP\nfunc_host\nRETRACE 1 TASKS 1|"};
    for (int mode=13;mode<=14;mode++) {
        int fds[2];CHECK(pipe(fds)==0);
        pid_t pid=fork();CHECK(pid>=0);
        if (!pid) { close(fds[0]);text_fd=fds[1];start(mode);(void)n64c_boot_frame_live();_Exit(1); }
        close(fds[1]);
        char got[256]={0};size_t used=0;ssize_t r;
        while (used<sizeof got-1 && (r=read(fds[0],got+used,sizeof got-1-used))>0) used+=(size_t)r;
        close(fds[0]);
        int status;CHECK(waitpid(pid,&status,0)==pid);
        CHECK(WIFEXITED(status) && WEXITSTATUS(status)==78);
        CHECK(!strcmp(got,want[mode-13]));
    }
    window_mode=0;
    start(12);CHECK(n64c_boot_frame_capture(path,3,2,2));CHECK(unlink(path)==0);
    for (int mode=5;mode<=11;mode++) {
        pid_t pid=fork();CHECK(pid>=0);
        if (!pid) { start(mode);(void)n64c_boot_frame_capture(path,3,2,2);_Exit(1); }
        int status;CHECK(waitpid(pid,&status,0)==pid);
        CHECK(WIFEXITED(status) && WEXITSTATUS(status)==78);
        CHECK(access(path,F_OK)!=0);
    }
    /* Progress comes from new graphics reaching a host frame boundary. No
     * sleeps or clock thresholds: idle repaints and audio cannot hide a stall. */
    start(0);
    CHECK(n64c_frame()==1 && render_progress==1);
    presenter((void *)(uintptr_t)0xa0000300,NULL);
    CHECK(render_progress==1);
    OSTask more={.type=M_GFXTASK,.data_ptr=(void *)(uintptr_t)0x100,.data_size=32};
    tasks[M_GFXTASK](&more,NULL);
    CHECK(render_progress==1); /* rendering alone cannot release the watchdog */
    CHECK(n64c_frame()==1 && render_progress==2);
    OSTask sound={.type=M_AUDTASK,.data_ptr=(void *)(uintptr_t)0x200,.data_size=8};
    tasks[M_AUDTASK](&sound,NULL);
    presenter((void *)(uintptr_t)0xa0000300,NULL);
    CHECK(render_progress==2);
    start(2);
    CHECK(n64c_frame()==1 && render_progress==0); /* retrace without rendering */
    start(3);
    CHECK(n64c_frame()==1 && render_progress==1); /* black VI can still make progress */
    window_mode=1;start(0);
    CHECK(n64c_frame()==1 && n64c_frame()==1 && n64c_frame()==1);
    CHECK(render_progress==2); /* a closed window produces no progress */
    n64c_boot_frame_close();CHECK(!tasks[1] && !tasks[2] && !presenter && !renderer);
    printf("frame checks=%u\n",checks);
    return 0;
}
