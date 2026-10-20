/* SPDX-License-Identifier: GPL-3.0-or-later */
#define _GNU_SOURCE
#include "../n64c_internal.h"
#include "../n64c_system.h"
#include <fenv.h>
#include <stdio.h>
#include <stdlib.h>
#include <xmmintrin.h>
static unsigned checks;
#define CHECK(x) do { checks++;if (!(x)) { fprintf(stderr,"FAIL:%d:%s\n",__LINE__,#x);exit(1); } } while (0)
/* Scheduler and native floating-point environment are real; unrelated ROM and
 * physical-address services are mocked. */
s32 osPiReadIo(u32 address,u32 *value) { (void)address;(void)value;abort(); }
u32 n64c_memory_physical(const void *p) { return (u32)(uintptr_t)p; }
static OSThread first,second,nested;
static OSMesgQueue queue;
static OSMesg messages[1];
static unsigned seen;
static void child(void *arg)
{
    (void)arg;
    CHECK(n64c_fpu_get_control()==0x01000800u);
    CHECK(!fetestexcept(FE_ALL_EXCEPT));
    seen|=1;
}
static void high(void *arg)
{
    (void)arg;
    CHECK(n64c_fpu_get_control()==0x01000800u);
    CHECK(n64c_fpu_set_control(0x01000402u)==0x01000800u);
    CHECK(feraiseexcept(FE_INEXACT)==0);
    osCreateThread(&nested,3,child,NULL,NULL,30);
    osStartThread(&nested); /* child preempts; inherits neither mode nor flags */
    CHECK(seen==1);
    CHECK(fetestexcept(FE_INEXACT)==FE_INEXACT);
    CHECK(feclearexcept(FE_ALL_EXCEPT)==0);
    CHECK(n64c_fpu_get_control()==0x01000402u);
    OSMesg message;
    CHECK(osRecvMesg(&queue,&message,OS_MESG_BLOCK)==0 && message.data32==7);
    CHECK(n64c_fpu_get_control()==0x01000402u); /* mode survives a blocked yield */
    seen|=2;
}
static void low(void *arg)
{
    (void)arg;
    CHECK(n64c_fpu_get_control()==0x01000800u);
    CHECK(n64c_fpu_set_control(1)==0x01000800u);
    OSMesg message;message.data32=7;
    CHECK(osSendMesg(&queue,message,OS_MESG_BLOCK)==0);
    CHECK(seen==3);
    CHECK(n64c_fpu_get_control()==1);
    seen|=4;
}
int main(void)
{
    fenv_t host;CHECK(fegetenv(&host)==0);CHECK(fesetenv(FE_DFL_ENV)==0);
    (void)n64c_fpu_set_control(0x01000403u);
    CHECK(feraiseexcept(FE_INEXACT)==0);
    unsigned host_mxcsr=_mm_getcsr();
    n64c_sched_reset();
    osCreateMesgQueue(&queue,messages,1);
    osCreateThread(&first,1,high,NULL,NULL,20);
    osCreateThread(&second,2,low,NULL,NULL,10);
    osStartThread(&first);osStartThread(&second);
    n64c_sched_run_idle();
    CHECK(seen==7 && n64c_sched_alive()==0);
    CHECK(_mm_getcsr()==host_mxcsr);
    CHECK(fegetround()==FE_DOWNWARD && fegetexcept()==FE_DIVBYZERO);
    CHECK(fetestexcept(FE_INEXACT)==FE_INEXACT);
    n64c_sched_reset();
    CHECK(feclearexcept(FE_ALL_EXCEPT)==0);
    CHECK(n64c_fpu_get_control()==0x01000403u);
    CHECK(fesetenv(&host)==0);
    printf("thread FPU checks=%u; three real guest threads\n",checks);
    return 0;
}
