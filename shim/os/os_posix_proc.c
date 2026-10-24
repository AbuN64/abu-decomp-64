/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Linux backend of the host seam, process part (os/n64c_host.h): the fixed-address guest stacks and the stderr tee. Kept
 * apart from os_posix.c, which needs the address-window module, so the scheduler links without it. */
#define _GNU_SOURCE
#include "n64c_host.h"
#if defined(__linux__)
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

int n64c_host_process_start(void) { return -1; }
int n64c_host_run(int (*body)(int, char **), int argc, char **argv) { return body(argc, argv); }

void n64c_host_sleep_until(const struct timespec *due)
{
    while (clock_nanosleep(CLOCK_MONOTONIC,TIMER_ABSTIME,due,NULL)!=0) {}
}

/* Each slot runs on its own stack at a fixed host address (a guard page below each), so host addresses the game
 * stores in guest RAM (pointers to its locals) do not depend on host allocation order or ASLR. */
#define STACK_BASE ((uintptr_t)0x60000000u) /* between the i386 brk heap and the top-down mmap area; guest windows start at 0x80000000 */
void *n64c_host_guest_stack(int slot,size_t bytes,size_t slot_bytes,size_t guard_bytes)
{
    static unsigned char *stacks;
    if (!stacks) {
        const size_t total=64u*slot_bytes;
        const int flags=MAP_PRIVATE|MAP_ANONYMOUS|MAP_NORESERVE;
        void *at=mmap((void *)STACK_BASE,total,PROT_NONE,flags|MAP_FIXED_NOREPLACE,-1,0);
        if (at==MAP_FAILED) {
            /* A position-independent host (the unit tests) can sit there; it still runs, only not reproducibly. */
            fprintf(stderr,"n64c-sched: 0x%08lx is taken (%s); guest thread stacks float, so host pointers in guest RAM "
                    "vary per run\n",(unsigned long)STACK_BASE,strerror(errno));
            at=mmap(NULL,total,PROT_NONE,flags,-1,0);
            if (at==MAP_FAILED) abort();
        }
        stacks=at;
    }
    unsigned char *base=stacks+(size_t)slot*slot_bytes+guard_bytes;
    if (mprotect(base,bytes,PROT_READ|PROT_WRITE)) abort();
    return base;
}

/* stderr also goes to path: a child process copies the pipe to both, so nothing is lost when this process dies,
 * and it ends when the last writer is gone. */
void n64c_host_tee_stderr(const char *path)
{
    int log=open(path,O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC,0644),fds[2];
    if (log<0) return;
    if (pipe(fds)) { close(log);return; }
    fflush(stderr);
    pid_t pid=fork();
    if (pid<0) { close(log);close(fds[0]);close(fds[1]);return; }
    if (!pid) {
        close(fds[1]);
        close(STDIN_FILENO);
        close(STDOUT_FILENO);
        signal(SIGINT,SIG_IGN);
        signal(SIGTERM,SIG_IGN);
        signal(SIGHUP,SIG_IGN);
        signal(SIGPIPE,SIG_IGN);
        char buf[4096];
        for (ssize_t n;(n=read(fds[0],buf,sizeof buf))!=0;) {
            if (n<0) {
                if (errno==EINTR) continue;
                break;
            }
            (void)!write(STDERR_FILENO,buf,(size_t)n);
            (void)!write(log,buf,(size_t)n);
        }
        _exit(0);
    }
    close(fds[0]);
    close(log);
    if (dup2(fds[1],STDERR_FILENO)>=0) setvbuf(stderr,NULL,_IONBF,0);
    close(fds[1]);
}
#endif /* __linux__ */
