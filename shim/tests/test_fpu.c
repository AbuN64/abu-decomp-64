/* SPDX-License-Identifier: GPL-3.0-or-later */
#define _GNU_SOURCE
#include "n64c_system.h"
#include <fenv.h>
#include <math.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>
#include <xmmintrin.h>

static unsigned checks;
#define CHECK(x) do { checks++; if (!(x)) { fprintf(stderr,"FAIL:%d:%s\n",__LINE__,#x); exit(1); } } while (0)
/* This test exercises native FPU state without a ROM or the I/O implementation. */
s32 osPiReadIo(u32 address,u32 *value) { (void)address; (void)value; abort(); }

static void failure(unsigned kind,const char *message,int signum)
{
    int fd[2]; CHECK(pipe(fd)==0);
    pid_t pid=fork(); CHECK(pid>=0);
    if (!pid) {
        close(fd[0]); dup2(fd[1],2); close(fd[1]);
        if (kind<32) (void)n64c_fpu_set_control(1u<<kind);
        if (kind==32) {
            (void)feraiseexcept(FE_INEXACT);
            (void)n64c_fpu_get_control();
        }
        if (kind==33) {
            _mm_setcsr(_mm_getcsr() | (1u<<15));
            (void)n64c_fpu_get_control();
        }
        if (kind==34) {
            _mm_setcsr(_mm_getcsr() ^ (1u<<13));
            (void)n64c_fpu_get_control();
        }
        if (kind==35) {
            _mm_setcsr(_mm_getcsr() ^ (1u<<9));
            (void)n64c_fpu_get_control();
        }
        if (kind==36 || kind==37) {
            (void)n64c_fpu_set_control(kind==36 ? 0x400u : 0x800u);
            volatile float zero=0.0f,one=1.0f;
            volatile float result=kind==36 ? one/zero : zero/zero;
            (void)result;
        }
        _Exit(1);
    }
    close(fd[1]); char text[128]={0};
    ssize_t bytes=read(fd[0],text,sizeof text-1); close(fd[0]); CHECK(bytes>=0);
    int status; CHECK(waitpid(pid,&status,0)==pid);
    if (signum) CHECK(WIFSIGNALED(status) && WTERMSIG(status)==signum);
    else {
        CHECK(WIFEXITED(status) && WEXITSTATUS(status)==78);
        CHECK(strcmp(text,message)==0);
    }
}

static void *thread_mode(void *arg)
{
    (void)arg;
    u32 before=n64c_fpu_get_control();
    u32 old=n64c_fpu_set_control(0x01000c03u);
    u32 now=n64c_fpu_get_control();
    return (void *)(uintptr_t)(before==2 && old==2 && now==0x01000c03u);
}

int main(void)
{
    fenv_t saved;
    CHECK(fegetenv(&saved)==0);
    CHECK(fesetenv(FE_DFL_ENV)==0);
    static const int modes[]={FE_TONEAREST,FE_TOWARDZERO,FE_UPWARD,FE_DOWNWARD};
    static const int flags[]={FE_INEXACT,FE_UNDERFLOW,FE_OVERFLOW,FE_DIVBYZERO,FE_INVALID};
    u32 old=0;
    /* Exhaust every supported control combination, including returned previous
     * values; compare against independent native fenv observations. */
    for (u32 flush=0;flush<2;flush++) for (u32 traps=0;traps<32;traps++) for (u32 mode=0;mode<4;mode++) {
        u32 wanted=(flush<<24)|(traps<<7)|mode;
        CHECK(n64c_fpu_set_control(wanted)==old);
        CHECK(n64c_fpu_get_control()==wanted);
        CHECK(fegetround()==modes[mode]);
        int enabled=0;
        for (unsigned i=0;i<5;i++) if (traps & (1u<<i)) enabled |= flags[i];
        CHECK(fegetexcept()==enabled);
        CHECK(!!(_mm_getcsr() & (1u<<15))==flush);
        CHECK(!!(_mm_getcsr() & (1u<<6))==flush);
        old=wanted;
    }
    CHECK(n64c_fpu_set_control(0)==old);
    for (unsigned mode=0;mode<4;mode++) {
        static const float expected[]={2,1,2,1};
        (void)n64c_fpu_set_control(mode);
        volatile float input=1.5f;
        volatile float rounded=nearbyintf(input);
        CHECK(rounded==expected[mode]);
        CHECK(n64c_fpu_get_control()==mode);
    }
    (void)n64c_fpu_set_control(0x01000800u);
    CHECK(n64c_fpu_set_control(n64c_fpu_get_control() | 0x400u)==0x01000800u);
    CHECK(n64c_fpu_get_control()==0x01000c00u);
    CHECK(fegetexcept()==(FE_INVALID|FE_DIVBYZERO));
    (void)n64c_fpu_set_control(0);
    for (unsigned bit=0;bit<32;bit++) if ((1u<<bit) & ~0x01000f83u)
        failure(bit,"boot-missing: system-FPU-unsupported-bits\n",0);
    failure(32,"boot-missing: system-FPU-status\n",0);
    failure(33,"boot-missing: system-FPU-flush-mismatch\n",0);
    failure(34,"boot-missing: system-FPU-rounding-mismatch\n",0);
    failure(35,"boot-missing: system-FPU-trap-mismatch\n",0);
    failure(36,"",SIGFPE);
    failure(37,"",SIGFPE);
    volatile float one=1.0f,zero=0.0f;
    volatile float infinity=one/zero;
    CHECK(isinf(infinity));
    CHECK(feclearexcept(FE_ALL_EXCEPT)==0);
    (void)n64c_fpu_set_control(0x01000000u);
    volatile float tiny=1.0e-40f,left=1.0e-30f,right=1.0e-10f;
    volatile float input=tiny*one,output=left*right;
    CHECK(input==0 && output==0);
    CHECK(feclearexcept(FE_ALL_EXCEPT)==0);
    (void)n64c_fpu_set_control(2);
    pthread_t thread;void *result;
    CHECK(pthread_create(&thread,NULL,thread_mode,NULL)==0);
    CHECK(pthread_join(thread,&result)==0 && (uintptr_t)result==1);
    CHECK(n64c_fpu_get_control()==2);
    CHECK(fesetenv(&saved)==0);
    printf("FPU checks=%u; control combinations=256\n",checks);
    return 0;
}
