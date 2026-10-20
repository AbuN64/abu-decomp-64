/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Behavioral expectations from the interrupt API and the native global-mask contract. */
#include "../n64c_internal.h"
#include <stdio.h>
#include <stdlib.h>

static unsigned checks;
#define CHECK(x) do { checks++; if (!(x)) { fprintf(stderr,"interrupts:%d: %s\n",__LINE__,#x); exit(1); } } while (0)
static OSMesgQueue q;
static OSMesg slots[8];
static OSThread high, low;
static int order;
static OSThread compare_thread;
static OSTime compare_seen[2];
static void receive(u32 value)
{
    OSMesg m;
    CHECK(osRecvMesg(&q,&m,OS_MESG_NOBLOCK)==0 && m.data32==value);
}
static void empty(void) { CHECK(osRecvMesg(&q,NULL,OS_MESG_NOBLOCK)==-1); }
static void high_entry(void *arg)
{
    (void)arg;
    CHECK(n64c_running_thread()==&high);
    OSMesg m;
    CHECK(osRecvMesg(&q,&m,OS_MESG_BLOCK)==0 && m.data32==77);
    CHECK(n64c_running_thread()==&high && order==1);
    order=2;
}
static void low_entry(void *arg)
{
    (void)arg;
    CHECK(n64c_running_thread()==&low);
    OSIntMask old=osSetIntMask(OS_IM_NONE);
    n64c_event_post(OS_EVENT_PI);
    CHECK(order==0);
    order=1;
    CHECK(osSetIntMask(old)==OS_IM_NONE);
    CHECK(order==2 && n64c_running_thread()==&low);
    order=3;
}
static void compare_entry(void *arg)
{
    (void)arg;
    OSMesg m;
    CHECK(osRecvMesg(&q,&m,OS_MESG_BLOCK)==0 && m.data32==90);
    compare_seen[0]=osGetTime();
    __osSetCompare(osGetCount()+3);
    CHECK(osRecvMesg(&q,&m,OS_MESG_BLOCK)==0 && m.data32==90);
    compare_seen[1]=osGetTime();
}
int main(void)
{
    N64cConfig cfg={0};
    n64c_init(&cfg);
    osCreateViManager(OS_PRIORITY_VIMGR);
    osCreateMesgQueue(&q,slots,8);
    CHECK(n64c_running_thread()==NULL);
    const u32 events[]={OS_EVENT_SP,OS_EVENT_SI,OS_EVENT_AI,OS_EVENT_VI,OS_EVENT_PI,OS_EVENT_DP,
        OS_EVENT_COUNTER,OS_EVENT_SW1,OS_EVENT_SW2,OS_EVENT_CART,OS_EVENT_PRENMI};
    const OSIntMask masks[]={OS_IM_SP,OS_IM_SI,OS_IM_AI,OS_IM_VI,OS_IM_PI,OS_IM_DP,
        OS_IM_COUNTER,OS_IM_SW1,OS_IM_SW2,OS_IM_CART,OS_IM_PRENMI};
    for (unsigned i=0;i<sizeof events/sizeof events[0];i++) {
        osSetEventMesg(events[i],&q,(OSMesg){.data32=100+i});
        CHECK(osSetIntMask(OS_IM_NONE)==OS_IM_ALL);
        n64c_event_post(events[i]);
        n64c_event_post(events[i]); /* one latched interrupt, not two queued messages */
        empty();
        CHECK(osSendMesg(&q,(OSMesg){.data32=9},OS_MESG_NOBLOCK)==0);
        receive(9); /* ordinary software messages are not interrupts */
        CHECK(osSetIntMask(masks[i])==OS_IM_NONE);
        receive(100+i);
        empty();
        CHECK(osSetIntMask(OS_IM_ALL)==masks[i]);
        empty();
        osSetEventMesg(events[i],NULL,(OSMesg){0});
    }
    osSetEventMesg(OS_EVENT_PI,&q,(OSMesg){.data32=55});
    osSetIntMask(OS_IM_PI & ~0x400u); /* MI source enabled, CPU RCP bridge disabled */
    n64c_event_post(OS_EVENT_PI);
    empty();
    osSetIntMask(OS_IM_PI & ~1u); /* CPU global interrupt enable disabled */
    empty();
    osSetIntMask(OS_IM_PI);
    receive(55);
    u32 outer=__osDisableInt(), inner=__osDisableInt();
    CHECK(outer==1 && inner==0);
    n64c_event_post(OS_EVENT_PI);
    __osRestoreInt(inner);
    empty();
    __osRestoreInt(outer);
    receive(55);
    CHECK(osSetIntMask(OS_IM_ALL)==OS_IM_PI);
    for (int i=0;i<8;i++) CHECK(osSendMesg(&q,(OSMesg){.data32=8},OS_MESG_NOBLOCK)==0);
    n64c_event_post(OS_EVENT_PI); /* a full event queue drops the nonblocking message */
    for (int i=0;i<8;i++) receive(8);
    osSetIntMask(OS_IM_ALL);
    empty();

    /* Masked VI still advances hardware retraces; notification is deferred and coalesced. */
    osViSetEvent(&q,(OSMesg){.data32=66},1);
    osSetIntMask(OS_IM_NONE);
    n64c_vi_retrace(); n64c_vi_retrace();
    osCreateViManager(OS_PRIORITY_VIMGR); /* creation cannot reset pending VI state */
    CHECK(osViGetRetraceCount()==2);
    empty();
    osSetIntMask(OS_IM_VI);
    receive(66); empty();
    osViSetEvent(NULL,(OSMesg){0},1);

    /* Due timers survive masking and service at the current monotonic clock. */
    OSTimer once={0}, repeat={0};
    osSetTimer(&once,10,0,&q,(OSMesg){.data32=10});
    osSetTimer(&repeat,12,12,&q,(OSMesg){.data32=12});
    n64c_clock_advance_to(25);
    CHECK(osGetTime()==25); empty();
    osCreateViManager(OS_PRIORITY_VIMGR); /* nor lose due timers or clock state */
    osSetIntMask(OS_IM_COUNTER);
    receive(10); receive(12); receive(12); empty();
    CHECK(osGetTime()==25 && osStopTimer(&repeat)==0);

    /* Programmed Compare uses Count equality, replacement and wrapping. */
    osSetEventMesg(OS_EVENT_COUNTER,&q,(OSMesg){.data32=90});
    __osSetCompare(30);
    n64c_clock_advance_to(29); empty();
    n64c_clock_advance_to(30); receive(90); empty();
    n64c_clock_advance_to(40); empty();
    __osSetCompare(45); __osSetCompare(47);
    n64c_clock_advance_to(46); empty();
    n64c_clock_advance_to(47); receive(90); empty();
    osSetIntMask(OS_IM_NONE);
    __osSetCompare(50);
    n64c_clock_advance_to(55); empty();
    __osSetCompare(60); /* writing Compare acknowledges the masked old event */
    osSetIntMask(OS_IM_COUNTER); empty();
    n64c_clock_advance_to(60); receive(90); empty();
    osSetIntMask(OS_IM_NONE);
    osSetTimer(&once,2,0,&q,(OSMesg){.data32=62});
    __osSetCompare(61);
    n64c_clock_advance_to(65); empty();
    __osSetCompare(70); /* acknowledge Compare without losing a due host timer */
    osSetIntMask(OS_IM_COUNTER);
    receive(62); receive(90); empty();
    n64c_clock_advance_to(70); receive(90); empty();
    n64c_clock_advance_to(0xfffffffeULL); empty();
    __osSetCompare(1);
    n64c_clock_advance_to(0x100000000ULL); empty();
    CHECK(osGetCount()==0);
    n64c_clock_advance_to(0x100000001ULL); receive(90); empty();
    CHECK(osGetCount()==1);
    __osSetCompare(osGetCount());
    n64c_clock_advance_to(osGetTime()); receive(90); empty();
    OSTime start=osGetTime();
    osCreateThread(&compare_thread,3,compare_entry,NULL,NULL,30);
    osStartThread(&compare_thread);
    n64c_run_idle();
    __osSetCompare(osGetCount()+2);
    n64c_clock_advance_to(start+10);
    CHECK(compare_seen[0]==start+2 && compare_seen[1]==start+5);
    empty();
    osSetEventMesg(OS_EVENT_COUNTER,NULL,(OSMesg){0});

    osSetIntMask(OS_IM_ALL);
    osSetEventMesg(OS_EVENT_PI,&q,(OSMesg){.data32=77});
    osCreateThread(&high,1,high_entry,NULL,NULL,20);
    osCreateThread(&low,2,low_entry,NULL,NULL,10);
    osStartThread(&high); osStartThread(&low);
    n64c_run_idle();
    CHECK(order==3 && n64c_running_thread()==NULL);
    osSetIntMask(OS_IM_NONE);
    n64c_event_post(OS_EVENT_PI);
    __osSetCompare(osGetCount()+1);
    n64c_shutdown();
    n64c_init(&cfg);
    osCreateMesgQueue(&q,slots,8);
    osSetEventMesg(OS_EVENT_PI,&q,(OSMesg){.data32=88});
    CHECK(osSetIntMask(OS_IM_ALL)==OS_IM_ALL);
    empty(); /* shutdown discarded pending events */
    osSetEventMesg(OS_EVENT_COUNTER,&q,(OSMesg){.data32=90});
    n64c_clock_advance_to(2); empty(); /* shutdown discarded the Compare deadline */
    n64c_event_post(OS_EVENT_PI); receive(88);
    n64c_shutdown();
    printf("interrupts: %u checks passed\n",checks);
    return 0;
}
