/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Windows backend of the host seam, process part (os/n64c_host.h): guest stacks and the stderr log. Apart from
 * os_win32.c, which needs the address-window module, so the scheduler links without it. */
#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include "n64c_host.h"
#include <fcntl.h>
#include <io.h>
#include <stdio.h>
#include <time.h>

typedef struct { int (*body)(int, char **); int argc; char **argv; int status; } Run;
static DWORD WINAPI run_thread(void *arg)
{
    Run *run=arg;
    run->status=run->body(run->argc,run->argv);
    return 0;
}
int n64c_host_run(int (*body)(int, char **), int argc, char **argv)
{
    Run run={body,argc,argv,1};
    HANDLE thread=CreateThread(NULL,16u<<20,run_thread,&run,STACK_SIZE_PARAM_IS_A_RESERVATION,NULL);
    if (!thread) return body(argc,argv);
    WaitForSingleObject(thread,INFINITE);
    CloseHandle(thread);
    return run.status;
}

/* A waitable timer with the high-resolution flag (Windows 10 1803 and later; the default timer ticks every 15.6 ms), else
 * a plain one, then a short sleep and a spin for the last millisecond. */
void n64c_host_sleep_until(const struct timespec *due)
{
    static HANDLE timer;
    static int tried;
    for (;;) {
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC,&now);
        const long long ns=(long long)(due->tv_sec-now.tv_sec)*1000000000ll+(due->tv_nsec-now.tv_nsec);
        if (ns<=0) return;
        if (!tried) {
            tried=1;
            timer=CreateWaitableTimerExW(NULL,NULL,0x2 /* CREATE_WAITABLE_TIMER_HIGH_RESOLUTION */,TIMER_ALL_ACCESS);
            if (!timer) timer=CreateWaitableTimerW(NULL,FALSE,NULL);
        }
        if (timer && ns>200000) {
            LARGE_INTEGER relative;
            relative.QuadPart=-(ns/100); /* 100 ns units, negative: relative */
            if (SetWaitableTimer(timer,&relative,0,NULL,NULL,FALSE)) {
                WaitForSingleObject(timer,INFINITE);
                continue;
            }
        }
        if (ns>1500000) Sleep((DWORD)(ns/1000000-1));
    }
}

void *n64c_host_guest_stack(int slot,size_t bytes,size_t slot_bytes,size_t guard_bytes)
{
    (void)slot;(void)bytes;(void)slot_bytes;(void)guard_bytes;
    return NULL; /* the system places thread stacks; winpthreads takes only the size */
}
/* Windows writes stderr to the log file itself: the program is a GUI process with no console to copy to, and a
 * file written by the process is complete even when it ends in a crash (a copying thread could lose the tail). */
void n64c_host_tee_stderr(const char *path)
{
    /* The program has no console, so the C runtime's stderr may be a closed stream: reopen it on the file itself. */
    if (!freopen(path,"wb",stderr)) return;
    setvbuf(stderr,NULL,_IONBF,0);
}
#endif /* _WIN32 */
