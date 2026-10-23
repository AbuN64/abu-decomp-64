/* SPDX-License-Identifier: GPL-3.0-or-later */
#define _GNU_SOURCE
#include "../n64c_memory.h"
#include "../n64c_pi.h"
#include "../n64c_cont.h"
#include "../n64c_audio.h"
#include "../n64c_internal.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
static int checks;
#define CHECK(value) do { checks++; if (!(value)) {     fprintf(stderr,"FAIL memory:%d: %s\n",__LINE__,#value); exit(1); } } while (0)
#if defined(__linux__) && defined(__i386__)
#include <signal.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>

static volatile u32 *const pi=(volatile u32 *)(uintptr_t)0xA4600000u;
static volatile u32 *const vi=(volatile u32 *)(uintptr_t)0xA4400000u;
static volatile u32 *const ai=(volatile u32 *)(uintptr_t)0xA4500000u;
static volatile u32 *const si=(volatile u32 *)(uintptr_t)0xA4800000u;
static OSMesgQueue events;
static OSMesg slots[8];
static unsigned samples,input_reads,order;
static u32 sample_rate;

static void input(int port,OSContPad *pad,void *user)
{
    (void)user;
    CHECK(port==0);
    input_reads++;
    pad->button=0x8000;
    pad->stick_x=-5;
    pad->stick_y=7;
}
static void audio(void *user,const int16_t *frames,size_t count,u32 rate)
{
    (void)user;
    CHECK(frames!=NULL);
    sigset_t mask;
    CHECK(sigprocmask(SIG_SETMASK,NULL,&mask)==0);
    CHECK(!sigismember(&mask,SIGSEGV) && !sigismember(&mask,SIGTRAP) &&
        !sigismember(&mask,SIGUSR2));
    samples+=(unsigned)count;
    sample_rate=rate;
}
static void expect_event(u32 expected)
{
    OSMesg message;
    CHECK(osRecvMesg(&events,&message,OS_MESG_NOBLOCK)==0);
    CHECK(message.data32==expected);
}
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
/* TLB miss service as a pager does it: the fault event names the stopped thread, its context the cause and
 * address; the pager maps the page and restarts the thread, whose load then completes. */
static OSMesgQueue faults;
static OSMesg fault_slots[2];
static OSThread faulting;
static u32 fault_value;
static unsigned fault_done;
static void faulter(void *argument)
{
    (void)argument;
    fault_value=*(volatile u32 *)(uintptr_t)0x00A00004u;
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
    CHECK((cause&0x7C)==8 && address==0x00A00004u); /* TLBL: a load miss */
    *(u32 *)n64c_memory_ram(0x3004,4)=0xFEEDBEEFu;
    osMapTLB(5,0,(void *)(uintptr_t)0x00A00000u,0x3000u,0xFFFFFFFFu,-1);
    osStartThread(thread);
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
static void child_failure(int kind,int expected)
{
    pid_t child=fork();
    CHECK(child>=0);
    if (child==0) {
        if (kind==0) (void)pi[200]; /* unknown register */
        else if (kind==2) osUnmapTLB(32);
        else if (kind==3) (void)*(volatile u32 *)(uintptr_t)0x00800000u; /* unmapped guest virtual page */
        else if (kind==4) { volatile u32 *volatile host=(volatile u32 *)(uintptr_t)0x10u; (void)*host; } /* host null page */
        else if (kind==5) ((void (*)(void))(uintptr_t)0x00239FBCu)(); /* call through a raw KUSEG code word */
        else if (kind==6) ((void (*)(void))(uintptr_t)0x80001000u)(); /* call through a raw KSEG0 code word */
        else *(volatile u32 *)(uintptr_t)0xB0000000u=0; /* immutable ROM */
        _Exit(99);
    }
    int status;
    CHECK(waitpid(child,&status,0)==child);
    if (expected==78) CHECK(WIFEXITED(status) && WEXITSTATUS(status)==78);
    else CHECK(WIFSIGNALED(status) && WTERMSIG(status)==expected);
}
int main(void)
{
    struct rlimit no_core={0,0};
    CHECK(setrlimit(RLIMIT_CORE,&no_core)==0);
    const char *path="n64c_memory_test.rom";
    u8 image[4096];
    for (unsigned i=0;i<sizeof image;i++) image[i]=(u8)i;
    FILE *file=fopen(path,"wb");
    CHECK(file && fwrite(image,1,sizeof image,file)==sizeof image && fclose(file)==0);
    N64cConfig console={.rom_path=path};
    N64cMemoryConfig config={.rdram_bytes=0x800000,.rom_bytes=sizeof image,
        .video_clock_hz=48681812};
    n64c_init(&console);
    CHECK(n64c_pi_register(0x20,0x30,N64C_RES_SWAP32,NULL)==0);
    CHECK(!n64c_memory_open(NULL));
    N64cMemoryConfig invalid=config;
    invalid.rdram_bytes=0x1000;
    CHECK(!n64c_memory_open(&invalid));
    CHECK(signal(SIGUSR2,SIG_IGN)!=SIG_ERR);
    CHECK(!n64c_memory_open(&config));
    CHECK(signal(SIGUSR2,SIG_DFL)!=SIG_ERR);
    void *blocker=mmap((void *)(uintptr_t)0xA0000000u,4096,PROT_READ|PROT_WRITE,
        MAP_PRIVATE|MAP_ANONYMOUS|MAP_FIXED_NOREPLACE,-1,0);
    CHECK(blocker==(void *)(uintptr_t)0xA0000000u);
    *(u32 *)blocker=0x11223344;
    CHECK(!n64c_memory_open(&config) && *(u32 *)blocker==0x11223344);
    CHECK(munmap(blocker,4096)==0);
    CHECK(n64c_memory_open(&config));
    CHECK(!n64c_memory_open(&config)); /* no replacement of live state */
    u32 *ram=n64c_memory_ram(0x100,32);
    CHECK(ram && n64c_memory_ram(0x7FFFFF,2)==NULL);
    ram[0]=0x31415926;
    CHECK(*(u32 *)(uintptr_t)0xA0000100u==0x31415926);
    CHECK(n64c_memory_physical((void *)(uintptr_t)0xA0000100u)==0x100);
    CHECK(osVirtualToPhysical(ram)==0x100);
    /* a TLB entry aliases RDRAM: 1 MB even page at 0x00400000 onto physical 0x100000, odd page invalid */
    osMapTLB(0,0x1FE000,(void *)(uintptr_t)0x00400000u,0x80100000u,0xFFFFFFFFu,7);
    CHECK(n64c_probe_tlb((void *)(uintptr_t)0x00400123u)==0x100123u);
    CHECK(osVirtualToPhysical((void *)(uintptr_t)0x00400123u)==0x100123u);
    CHECK(n64c_probe_tlb((void *)(uintptr_t)0x00500000u)==UINT32_MAX);
    u32 *tlb_ram=n64c_memory_ram(0x100000,4);
    tlb_ram[0]=0x600DF00D;
    CHECK(*(volatile u32 *)(uintptr_t)0x00400000u==0x600DF00D);
    *(volatile u32 *)(uintptr_t)0x00400004u=0x12345678;
    CHECK(tlb_ram[1]==0x12345678);
    /* An indexed unmap releases both pages and leaves other entries intact. */
    osMapTLB(3,0,(void *)(uintptr_t)0x00600000u,0x100000u,0x101000u,-1);
    *(volatile u32 *)(uintptr_t)0x00601000u=0xAABBCCDD;
    CHECK(*(u32 *)n64c_memory_ram(0x101000,4)==0xAABBCCDD);
    CHECK(n64c_probe_tlb((void *)(uintptr_t)0x00600123u)==0x100123u);
    CHECK(n64c_probe_tlb((void *)(uintptr_t)0x00601123u)==0x101123u);
    CHECK(osVirtualToPhysical((void *)(uintptr_t)0x00601123u)==0x101123u);
    osUnmapTLB(3);
    CHECK(n64c_probe_tlb((void *)(uintptr_t)0x00600000u)==UINT32_MAX);
    CHECK(n64c_probe_tlb((void *)(uintptr_t)0x00601000u)==UINT32_MAX);
    unsigned char residency;
    errno=0;
    CHECK(mincore((void *)(uintptr_t)0x00600000u,4096,&residency)==-1 && errno==ENOMEM);
    errno=0;
    CHECK(mincore((void *)(uintptr_t)0x00601000u,4096,&residency)==-1 && errno==ENOMEM);
    CHECK(*(volatile u32 *)(uintptr_t)0x00400000u==0x600DF00D);
    osUnmapTLB(3); /* empty entry is harmless */
    osMapTLB(3,0,(void *)(uintptr_t)0x00600000u,0x100000u,0x101000u,-1);
    CHECK(*(volatile u32 *)(uintptr_t)0x00601000u==0xAABBCCDD);
    osMapTLB(3,0,(void *)(uintptr_t)0x00602000u,0x101000u,0xFFFFFFFFu,-1);
    errno=0;
    CHECK(mincore((void *)(uintptr_t)0x00600000u,4096,&residency)==-1 && errno==ENOMEM);
    CHECK(*(volatile u32 *)(uintptr_t)0x00602000u==0xAABBCCDD);
    CHECK(n64c_probe_tlb((void *)(uintptr_t)0x00600000u)==UINT32_MAX);
    CHECK(n64c_probe_tlb((void *)(uintptr_t)0x00602004u)==0x101004u);
    osUnmapTLBAll();
    errno=0;
    CHECK(mincore((void *)(uintptr_t)0x00602000u,4096,&residency)==-1 && errno==ENOMEM);
    CHECK(n64c_probe_tlb((void *)(uintptr_t)0x00400000u)==UINT32_MAX);
    CHECK(n64c_probe_tlb((void *)(uintptr_t)0x00602000u)==UINT32_MAX);
    child_failure(2,78);
    osMapTLB(0,0x1FE000,(void *)(uintptr_t)0x00400000u,0x80100000u,0xFFFFFFFFu,7); /* remap after unmap */
    CHECK(*(volatile u32 *)(uintptr_t)0x00400004u==0x12345678);
    osUnmapTLBAll();
    CHECK(*(const u32 *)(uintptr_t)0xB0000020u==0x20212223);
    CHECK(!memcmp((void *)(uintptr_t)0x90000000u,(void *)(uintptr_t)0xB0000000u,4096));
    memcpy(ram,(void *)(uintptr_t)0xB0000020u,16);
    CHECK(ram[0]==0x20212223);
    errno=61;
    volatile double seed=7.25;
    volatile unsigned iterations=8;
    double held=seed;
    for (unsigned i=0;i<iterations;i++) {
        held=held*1.5+0.25;
        CHECK(pi[4]==0);
    }
    CHECK(held==198.1240234375 && errno==61);

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

    n64c_set_audio_sink(audio,NULL);
    message.data32=12;
    osSetEventMesg(OS_EVENT_AI,&events,message);
    ai[0]=0x100;
    ai[4]=1520;
    ai[1]=16;
    /* The submitted buffer plays on the virtual clock: AI_STATUS reads DMA busy (one of two FIFO entries held). */
    CHECK(samples==4 && sample_rate==48681812u/1521u && ai[1]==0 && ai[3]==AI_STATUS_DMA_BUSY);
    n64c_clock_advance_to(n64c_clock_now()+100000); /* the AI interrupt follows the buffer's playing time */
    expect_event(12);
    n64c_cont_set_input(input,NULL);
    message.data32=13;
    osSetEventMesg(OS_EVENT_SI,&events,message);
    u8 *packet=n64c_memory_ram(0x300,64);
    memset(packet,0,64);
    packet[0]=1; packet[1]=3; packet[2]=0; packet[6]=0xFE; packet[63]=1;
    si[0]=0x300;
    si[4]=0x1FC007C0;
    expect_event(13);
    si[1]=0x1FC007C0;
    expect_event(13);
    CHECK(packet[3]==5 && packet[4]==0 && packet[5]==2 && packet[63]==0 && input_reads==0);
    memset(packet,0,64);
    packet[0]=1; packet[1]=4; packet[2]=1; packet[7]=0xFE; packet[63]=1;
    si[4]=0x1FC007C0;
    expect_event(13);
    si[1]=0x1FC007C0;
    expect_event(13);
    CHECK(packet[3]==0x80 && packet[4]==0 && packet[5]==0xFB && packet[6]==7 && input_reads==1);
    memset(packet,0,64);
    packet[1]=1; packet[2]=3; packet[3]=0; packet[7]=0xFE; packet[63]=1;
    si[4]=0x1FC007C0;
    expect_event(13);
    si[1]=0x1FC007C0;
    expect_event(13);
    CHECK(packet[2]==0x83 && si[6]==0);
    {   /* the register-page decoder: lengths, direction, register, immediate */
        unsigned length,reg,write; u32 imm;
        const unsigned char load[]={0x8B,0x15,0x78,0x56,0x34,0x12};      /* mov edx,[abs32] */
        const unsigned char store[]={0x89,0x4B,0x10};                    /* mov [ebx+16],ecx */
        const unsigned char sib[]={0x8B,0x44,0x91,0x08};                 /* mov eax,[ecx+edx*4+8] */
        const unsigned char immed[]={0xC7,0x00,0x44,0x33,0x22,0x11};     /* mov dword [eax],0x11223344 */
        const unsigned char moffs[]={0xA3,0,0,0,0};
        const unsigned char bad[]={0x8A,0x00};
        const unsigned char alu[]={0x01,0x03};
        CHECK(n64c_memory_decode_mov(load,&length,&reg,&write,&imm) && length==6 && reg==2 && !write);
        CHECK(n64c_memory_decode_mov(store,&length,&reg,&write,&imm) && length==3 && reg==1 && write);
        CHECK(n64c_memory_decode_mov(sib,&length,&reg,&write,&imm) && length==4 && reg==0 && !write);
        CHECK(n64c_memory_decode_mov(immed,&length,&reg,&write,&imm) && length==6 && write && imm==0x11223344u);
        CHECK(n64c_memory_decode_mov(moffs,&length,&reg,&write,&imm) && length==5 && write);
        CHECK(!n64c_memory_decode_mov(bad,&length,&reg,&write,&imm) && !n64c_memory_decode_mov(alu,&length,&reg,&write,&imm));
    }
    CHECK(!n64c_memory_tracer_in_status("Name:\tx\nTracerPid:\t0\nUid:\t1\n"));
    CHECK(n64c_memory_tracer_in_status("Name:\tx\nTracerPid:\t4242\nUid:\t1\n"));
    CHECK(!n64c_memory_tracer_in_status("Name:\tx\nPid:\t7\n"));
    child_failure(0,78);
    child_failure(1,SIGSEGV);
    child_failure(3,78); /* a TLB miss is a named boot stop */
    child_failure(4,SIGSEGV); /* a host fault stays a crash */
    child_failure(5,78); /* a call into guest memory is a named boot stop (code-pointer-<address>) */
    child_failure(6,78);
    n64c_shutdown();
    CHECK(n64c_memory_ram(0,1)==NULL);
    struct sigaction action;
    CHECK(sigaction(SIGSEGV,NULL,&action)==0 && action.sa_handler==SIG_DFL);
    n64c_init(&console);
    CHECK(n64c_memory_open(&config));
    CHECK(*(u32 *)n64c_memory_ram(0x100,4)==0);
    n64c_shutdown();
    CHECK(remove(path)==0);
    printf("native memory: %d checks; aliases, typed PI, signal context, PI/VI/AI/SI, lifecycle\n",checks);
    return 0;
}
#else
int main(void)
{
    N64cMemoryConfig config={0x800000,4096,48681812};
    CHECK(!n64c_memory_open(&config));
    puts("native memory: unsupported host refuses");
    return 0;
}
#endif
