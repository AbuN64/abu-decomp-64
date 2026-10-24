/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The Windows/i386 backend of the address windows (os/os_win32.c): the same windows, register pages, TLB pages and
 * fault service as tests/test_memory.c checks on Linux, without the fork-based failure cases. */
#ifdef _WIN32
#include <windows.h>
#endif
#include "../n64c_memory.h"
#include "../n64c_pi.h"
#include "../n64c_cont.h"
#include "../n64c_audio.h"
#include "../n64c_internal.h"
#include "../os/n64c_host.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#if defined(_WIN32) && defined(__i386__)
static int checks;
#define CHECK(value) do { checks++; if (!(value)) { fprintf(stderr,"FAIL memory_win:%d: %s\n",__LINE__,#value); exit(1); } } while (0)
static volatile u32 *const pi=(volatile u32 *)(uintptr_t)0xA4600000u;
static volatile u32 *const vi=(volatile u32 *)(uintptr_t)0xA4400000u;
/* What the game links (boot/missing.c, boot/frame.c) and the shim names without a weak reference on Windows. */
long long n64c_boot_missing(const char *symbol)
{
    fprintf(stderr,"boot-missing: %s\n",symbol);
    _Exit(78);
}
void n64c_boot_run_ended(void) {}
static OSMesgQueue events;
static OSMesg slots[8];
static unsigned order;
static void reader(void *argument)
{
    (void)argument;
    OSMesg message;
    CHECK(osRecvMesg(&events,&message,OS_MESG_BLOCK)==0 && message.data32==11);
    CHECK(order==0);
    CHECK(*(u32 *)n64c_memory_ram(0x200,4)==0x20212223u);
    CHECK(pi[3]==15); /* register state is published before its completion event */
    vi[1]=0x800;
    order=1;
}
static void writer(void *argument)
{
    (void)argument;
    errno=73;
    pi[0]=0x200;
    pi[1]=0x10000020u;
    pi[3]=15;
    CHECK(order==1 && errno==73);
    order=2;
}
static OSMesgQueue faults;
static OSMesg fault_slots[2];
static OSThread faulting;
static u32 fault_value;
static unsigned fault_done;
static void faulter(void *argument)
{
    (void)argument;
    if (GetEnabledXStateFeatures() & 4) {
        unsigned char before[32],after[32];
        for (unsigned i=0;i<sizeof before;i++) before[i]=(unsigned char)(i*7+3);
        /* One instruction faults with both halves of YMM0 live. The pager must
         * resume the load and retain all 256 bits, not just the FXSAVE half. */
        __asm__ volatile("vmovdqu %2, %%ymm0\n\t"
                         "movl 0x00900004, %%eax\n\t"
                         "vmovdqu %%ymm0, %0\n\t"
                         "vzeroupper"
                         : "=m"(after), "=a"(fault_value) : "m"(before) : "xmm0", "memory");
        CHECK(!memcmp(before,after,sizeof before));
    } else fault_value=*(volatile u32 *)(uintptr_t)0x00900004u;
    fault_done=1;
}
static void pager(void *argument)
{
    (void)argument;
    OSMesg message;
    CHECK(osRecvMesg(&faults,&message,OS_MESG_BLOCK)==0 && message.data32==21);
    OSThread *thread=osGetCurrFaultedThread();
    u32 cause,address;
    memcpy(&cause,thread->context+0x100,sizeof cause);
    memcpy(&address,thread->context+0x104,sizeof address);
    CHECK(thread==&faulting && thread->state==OS_STATE_STOPPED && (thread->flags&2) && !fault_done);
    CHECK((cause&0x7C)==8 && address==0x00900004u); /* TLBL: a load miss */
    *(u32 *)n64c_memory_ram(0x3004,4)=0xFEEDBEEFu;
    osMapTLB(5,0,(void *)(uintptr_t)0x00900000u,0x3000u,0xFFFFFFFFu,-1);
    osStartThread(thread);
}
int main(void)
{
    
    const int elsewhere=n64c_host_process_start(); /* the test runs in a second process that has the guest range reserved */
    if (elsewhere>=0) return elsewhere;
    const char *path="n64c_memory_win_test.rom";
    u8 image[4096];
    for (unsigned i=0;i<sizeof image;i++) image[i]=(u8)i;
    FILE *file=fopen(path,"wb");
    CHECK(file && fwrite(image,1,sizeof image,file)==sizeof image && fclose(file)==0);
    N64cConfig console={.rom_path=path};
    N64cMemoryConfig config={.rdram_bytes=0x800000,.rom_bytes=sizeof image,.video_clock_hz=48681812};
    n64c_init(&console);
    CHECK(n64c_pi_register(0x20,0x30,N64C_RES_SWAP32,NULL)==0);
    CHECK(!n64c_memory_open(NULL));
    N64cMemoryConfig invalid=config;
    invalid.rdram_bytes=0x1000;
    CHECK(!n64c_memory_open(&invalid));
    /* an address already in use is never replaced */
    void *blocker=VirtualAlloc((void *)(uintptr_t)0xA0000000u,4096,MEM_RESERVE|MEM_COMMIT,PAGE_READWRITE);
    CHECK(blocker==(void *)(uintptr_t)0xA0000000u);
    *(u32 *)blocker=0x11223344;
    CHECK(!n64c_memory_open(&config) && *(u32 *)blocker==0x11223344);
    CHECK(VirtualFree(blocker,0,MEM_RELEASE));
    CHECK(n64c_memory_open(&config));
    CHECK(!n64c_memory_open(&config)); /* no replacement of live state */
    u32 *ram=n64c_memory_ram(0x100,32);
    CHECK(ram && n64c_memory_ram(0x7FFFFF,2)==NULL);
    ram[0]=0x31415926;
    CHECK(*(u32 *)(uintptr_t)0xA0000100u==0x31415926);
    CHECK(n64c_memory_physical((void *)(uintptr_t)0xA0000100u)==0x100);
    CHECK(osVirtualToPhysical(ram)==0x100);
    /* a 64 KiB aligned TLB entry is a second view of RDRAM: 1 MB even page at 0x00400000 onto physical 0x100000 */
    osMapTLB(0,0x1FE000,(void *)(uintptr_t)0x00400000u,0x80100000u,0xFFFFFFFFu,7);
    CHECK(n64c_probe_tlb((void *)(uintptr_t)0x00400123u)==0x100123u);
    CHECK(n64c_probe_tlb((void *)(uintptr_t)0x00500000u)==UINT32_MAX);
    u32 *tlb_ram=n64c_memory_ram(0x100000,4);
    tlb_ram[0]=0x600DF00D;
    CHECK(*(volatile u32 *)(uintptr_t)0x00400000u==0x600DF00D);
    *(volatile u32 *)(uintptr_t)0x00400004u=0x12345678;
    CHECK(tlb_ram[1]==0x12345678);
    osUnmapTLBAll();
    CHECK(n64c_probe_tlb((void *)(uintptr_t)0x00400000u)==UINT32_MAX);
    /* 4 KiB TLB pages the host cannot alias are kept coherent: a store through either side is seen through the other */
    osMapTLB(3,0,(void *)(uintptr_t)0x00800000u,0x100000u,0x101000u,-1);
    CHECK(n64c_probe_tlb((void *)(uintptr_t)0x00800123u)==0x100123u);
    CHECK(n64c_probe_tlb((void *)(uintptr_t)0x00801123u)==0x101123u);
    CHECK(osVirtualToPhysical((void *)(uintptr_t)0x00801123u)==0x101123u);
    CHECK(*(volatile u32 *)(uintptr_t)0x00800000u==0x600DF00D);          /* RDRAM bytes seen through the virtual page */
    *(volatile u32 *)(uintptr_t)0x00801000u=0xAABBCCDD;                  /* a store through the virtual page ... */
    CHECK(*(u32 *)n64c_memory_ram(0x101000,4)==0xAABBCCDD);              /* ... is in RDRAM */
    *(u32 *)n64c_memory_ram(0x101004,4)=0x01020304;                      /* a store to RDRAM ... */
    CHECK(*(volatile u32 *)(uintptr_t)0x00801004u==0x01020304);          /* ... is in the virtual page */
    CHECK(*(volatile u32 *)(uintptr_t)0x00801000u==0xAABBCCDD);          /* and the earlier bytes survived both swaps */
    for (unsigned i=0;i<64;i++) {                                         /* ping-pong */
        *(volatile u32 *)(uintptr_t)(0x00800010u+4*i)=i;
        CHECK(*(u32 *)n64c_memory_ram(0x100010+4*i,4)==i);
        *(u32 *)n64c_memory_ram(0x100010+4*i,4)=i+1000;
        CHECK(*(volatile u32 *)(uintptr_t)(0x00800010u+4*i)==i+1000);
    }
    /* a file read into either side (a system call) reaches the other */
    CHECK(n64c_pi_read(0x20,(void *)(uintptr_t)0x00800100u,16)==0);
    CHECK(*(u32 *)n64c_memory_ram(0x100100,4)==*(volatile u32 *)(uintptr_t)0x00800100u);
    CHECK(n64c_pi_read(0x20,n64c_memory_ram(0x100200,16),16)==0);
    CHECK(*(volatile u32 *)(uintptr_t)0x00800200u==*(u32 *)n64c_memory_ram(0x100200,4));
    /* two virtual pages of one frame, and an unmap that writes the virtual side back */
    osMapTLB(4,0,(void *)(uintptr_t)0x00810000u,0x100000u,0xFFFFFFFFu,-1);
    *(volatile u32 *)(uintptr_t)0x00810020u=0xCAFEF00Du;
    CHECK(*(volatile u32 *)(uintptr_t)0x00800020u==0xCAFEF00Du);
    *(volatile u32 *)(uintptr_t)0x00800024u=0x0BADCAFEu;
    CHECK(*(volatile u32 *)(uintptr_t)0x00810024u==0x0BADCAFEu);
    *(volatile u32 *)(uintptr_t)0x00810028u=0x77665544u;
    osUnmapTLB(4);
    CHECK(*(u32 *)n64c_memory_ram(0x100028,4)==0x77665544u);
    CHECK(*(volatile u32 *)(uintptr_t)0x00800028u==0x77665544u);
    osUnmapTLB(3);
    CHECK(n64c_probe_tlb((void *)(uintptr_t)0x00800000u)==UINT32_MAX);
    CHECK(*(u32 *)n64c_memory_ram(0x101000,4)==0xAABBCCDD && *(u32 *)n64c_memory_ram(0x100020,4)==0xCAFEF00Du);
    osUnmapTLB(3); /* empty entry is harmless */
    osMapTLB(3,0,(void *)(uintptr_t)0x00802000u,0x101000u,0xFFFFFFFFu,-1);
    CHECK(*(volatile u32 *)(uintptr_t)0x00802000u==0xAABBCCDD);
    osUnmapTLBAll();
    CHECK(*(const u32 *)(uintptr_t)0xB0000020u==0x20212223);
    CHECK(!memcmp((void *)(uintptr_t)0x90000000u,(void *)(uintptr_t)0xB0000000u,4096));
    memcpy(ram,(void *)(uintptr_t)0xB0000020u,16);
    CHECK(ram[0]==0x20212223);
    /* the floating point state of the interrupted code survives a register fault (x87 and SSE) */
    errno=61;
    volatile double seed=7.25;
    volatile long double wide=1.5L;
    volatile unsigned iterations=8;
    double held=seed;
    long double deep=wide;
    for (unsigned i=0;i<iterations;i++) {
        held=held*1.5+0.25;
        deep=deep*3.0L+1.0L;
        CHECK(pi[4]==0);
    }
    fprintf(stderr,"fp: held=%.10f deep=%.4Lf errno=%d\n",held,deep,errno);
    CHECK(held==198.1240234375 && deep==13121.5L && errno==61);

    osCreateMesgQueue(&events,slots,8);
    OSMesg message;
    message.data32=11;
    osSetEventMesg(OS_EVENT_PI,&events,message);
    OSThread high,low;
    osCreateThread(&high,1,reader,NULL,NULL,20);
    osCreateThread(&low,2,writer,NULL,NULL,10);
    osStartThread(&high);
    osStartThread(&low);
    n64c_run_idle();
    CHECK(order==2 && n64c_thread_count()==0);
    CHECK(osGetCurrFaultedThread()==NULL);
    /* TLB miss service as a pager does it */
    osCreateMesgQueue(&faults,fault_slots,2);
    message.data32=21;
    osSetEventMesg(OS_EVENT_FAULT,&faults,message);
    OSThread service;
    osCreateThread(&faulting,3,faulter,NULL,NULL,10);
    osCreateThread(&service,4,pager,NULL,NULL,30);
    osStartThread(&service);
    osStartThread(&faulting);
    n64c_run_idle();
    CHECK(fault_done && fault_value==0xFEEDBEEFu && osGetCurrFaultedThread()==&faulting);
    osUnmapTLB(5);
    osSetEventMesg(OS_EVENT_FAULT,NULL,message);
    vi[0]=3;
    CHECK(osViGetNextFramebuffer()==n64c_memory_ram(0x800,1));
    CHECK(n64c_frame()==0 && osViGetCurrentFramebuffer()==n64c_memory_ram(0x800,1));
    CHECK(vi[1]==0x800);
    n64c_memory_close();
    CHECK(n64c_memory_ram(0,4)==NULL);
    CHECK(n64c_memory_open(&config)); /* closed state reopens */
    n64c_memory_close();
    remove(path);
    printf("memory_win: %d checks ok\n",checks);
    return 0;
}
#else
int main(void) { printf("memory_win: not a Windows/i386 build, skipped\n"); return 0; }
#endif
