/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Address facts: https://n64.readthedocs.io/#memory-map
 * Linux/i386 adapter: a protected register page lets the CPU execute one native
 * memory instruction. SIGTRAP reprotects it. Component calls run on a separate
 * TLS stack after sigreturn; a second signal restores the saved machine context.
 * No instruction decoder, guest CPU, ptrace or callbacks in signal context. */
#define _GNU_SOURCE
#include "n64c_memory.h"
#include "n64c_internal.h"
#include "n64c_mmio.h"
#include "n64c_diag.h"

#if defined(__linux__) && defined(__i386__)
#include <signal.h>
#include <errno.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <fcntl.h>
#include <ucontext.h>

enum { PAGE_BYTES=4096, MAX_MAPS=12, RESUME_SIGNAL=SIGUSR2, TLB_ENTRIES=32 };
static const u32 devices[]={0x04400000,0x04500000,0x04600000,0x04800000};
typedef struct { void *base; size_t bytes; } Mapping;
static Mapping maps[MAX_MAPS];
static unsigned map_count, handlers;
static u32 ram_size;
static int window_fd=-1;               /* the memfd behind every window, kept for TLB mappings */
typedef struct { void *base; size_t bytes; u32 physical; } TlbMapping;
static TlbMapping tlb[TLB_ENTRIES][2];
static struct sigaction prior[3];
static const int signals[]={SIGSEGV,SIGTRAP,RESUME_SIGNAL};
static _Thread_local struct {
    ucontext_t saved;
    void *page;
    u32 physical, offset, value;
    int stage, write, error;
    _Alignas(16) unsigned char stack[65536];
} transaction;

static _Noreturn void fault_stop(void)
{
    static const char message[]="n64compat: invalid MMIO fault\n";
    (void)write(STDERR_FILENO,message,sizeof message-1);
    _Exit(78);
}
static _Noreturn void dispatch(void);

static void redirect(ucontext_t *context)
{
    uintptr_t top=(uintptr_t)(transaction.stack+sizeof transaction.stack);
    top=(top & ~(uintptr_t)15)-sizeof(uintptr_t);
    *(uintptr_t *)top=0; /* dispatch never returns */
    context->uc_mcontext.gregs[REG_ESP]=(greg_t)top;
    context->uc_mcontext.gregs[REG_EIP]=(greg_t)(uintptr_t)dispatch;
    context->uc_mcontext.gregs[REG_EFL]&=~(greg_t)(0x100|0x400);
}
static int device_page(uintptr_t address,u32 *physical)
{
    if (address<0x80000000u || address>=0xC0000000u) return 0;
    *physical=(u32)address & 0x1FFFFFFFu;
    for (unsigned i=0;i<sizeof devices/sizeof devices[0];i++)
        if (*physical>=devices[i] && *physical<devices[i]+PAGE_BYTES) return 1;
    return 0;
}
extern char __executable_start[];
/* A guest KUSEG address the guest maps through the TLB (osMapTLB): below the host image, where the host itself
 * maps nothing, and above the lowest 64 KiB that Linux never maps (vm.mmap_min_addr), so a null-pointer access
 * stays a host fault. */
static int guest_virtual(uintptr_t address)
{
    return ram_size && address>=0x10000u && address<(uintptr_t)__executable_start;
}
/* A guest access to a virtual page no TLB entry maps when nothing services it (off a guest thread, or no
 * OS_EVENT_FAULT queue): the boot stops here by name instead of crashing. */
static _Noreturn void tlb_miss_stop(u32 address)
{
    char line[]="boot-missing: TLB-miss-00000000\n";
    static const char hex[]="0123456789abcdef";
    for (int i=0;i<8;i++) line[sizeof line-3-i]=hex[(address>>(4*i))&15u];
    (void)write(STDERR_FILENO,line,sizeof line-1);
    _Exit(78);
}
/* The CPU jumped to a guest address: a call through a code pointer the data holds as the original's raw value
 * (KSEG0 0x80xxxxxx or a KUSEG alias), which is no native function. Native code never runs from guest memory, so
 * the boot stops by name instead of the fault reaching the guest's own fault service. */
static _Noreturn void code_pointer_stop(u32 address)
{
    char line[]="boot-missing: code-pointer-00000000\n";
    static const char hex[]="0123456789abcdef";
    for (int i=0;i<8;i++) line[sizeof line-3-i]=hex[(address>>(4*i))&15u];
    (void)write(STDERR_FILENO,line,sizeof line-1);
    _Exit(78);
}
static int guest_code_fetch(const ucontext_t *context,uintptr_t address)
{
    if ((uintptr_t)context->uc_mcontext.gregs[REG_EIP]!=address || !ram_size) return 0;
    return guest_virtual(address) || (address>=0x80000000u && address<0xC0000000u);
}
/* A debugger holds the process (the tile runs under gdb). It stops on every signal the handlers below take
 * on purpose, so the supervisor passes SIGSEGV, SIGUSR2 and SIGTRAP through; a genuine host fault then dies in
 * the default action with no stop. Traced runs name that fault at the stop function instead. */
int n64c_memory_tracer_in_status(const char *text)
{
    static const char key[]="TracerPid:";
    for (const char *line=text;line && *line;) {
        if (strncmp(line,key,sizeof key-1)==0) {
            line+=sizeof key-1;
            while (*line==' ' || *line=='\t') line++;
            return *line>='1' && *line<='9';
        }
        line=strchr(line,'\n');
        if (line) line++;
    }
    return 0;
}
static int host_traced(void)
{
    char text[2048];
    int fd=open("/proc/self/status",O_RDONLY);
    if (fd<0) return 0;
    ssize_t got=read(fd,text,sizeof text-1);
    (void)close(fd);
    if (got<=0) return 0;
    text[got]=0;
    return n64c_memory_tracer_in_status(text);
}
static void signal_handler(int signal_number,siginfo_t *info,void *opaque)
{
    ucontext_t *context=opaque;
    if (signal_number==SIGSEGV && guest_code_fetch(context,(uintptr_t)info->si_addr)) {
        if (n64c_diag_code_pointer && n64c_diag_code_pointer((u32)(uintptr_t)info->si_addr,context)) return;
        code_pointer_stop((u32)(uintptr_t)info->si_addr);
    }
    if (signal_number==RESUME_SIGNAL && transaction.stage==3) {
        *context=transaction.saved;
        transaction.stage=0;
        errno=transaction.error;
        return;
    }
    if (signal_number==RESUME_SIGNAL && transaction.stage==2) {
        *context=transaction.saved;
        transaction.stage=1;
        errno=transaction.error;
        return;
    }
    if (signal_number==SIGTRAP && transaction.stage==1 && info->si_code==TRAP_TRACE) {
        transaction.value=*(volatile u32 *)((u8 *)transaction.page+transaction.offset);
        if (mprotect(transaction.page,PAGE_BYTES,PROT_NONE)!=0) fault_stop();
        context->uc_mcontext.gregs[REG_EFL]&=~(greg_t)0x100;
        if (transaction.write) {
            transaction.saved=*context;
            transaction.stage=4;
            redirect(context);
        } else {
            transaction.stage=0;
        }
        return;
    }
    u32 physical;
    if (signal_number==SIGSEGV && transaction.stage==0 &&
        device_page((uintptr_t)info->si_addr,&physical)) {
        if ((physical & 3u) || (context->uc_mcontext.gregs[REG_EFL]&0x100))
            fault_stop();
        transaction.error=errno;
        transaction.physical=physical;
        transaction.offset=physical & (PAGE_BYTES-1u);
        transaction.page=(void *)((uintptr_t)info->si_addr & ~(uintptr_t)(PAGE_BYTES-1));
        transaction.write=(context->uc_mcontext.gregs[REG_ERR]&2)!=0;
        transaction.saved=*context;
        transaction.stage=5;
        redirect(context);
        return;
    }
    if (signal_number==SIGSEGV && transaction.stage==0 && guest_virtual((uintptr_t)info->si_addr)) {
        if (!n64c_sched_on_guest() || (context->uc_mcontext.gregs[REG_EFL]&0x100)) {
            if (!(context->uc_mcontext.gregs[REG_EFL]&0x100) && n64c_diag_fault &&
                n64c_diag_fault(signal_number,info,context)) return;
            tlb_miss_stop((u32)(uintptr_t)info->si_addr);
        }
        transaction.error=errno;
        transaction.physical=(u32)(uintptr_t)info->si_addr; /* the guest virtual address (badvaddr) */
        transaction.write=(context->uc_mcontext.gregs[REG_ERR]&2)!=0;
        transaction.saved=*context;
        transaction.stage=6;
        redirect(context);
        return;
    }
    /* Run-on (the walk's diagnostic boot) records a host fault and returns from the faulting original function. */
    if (signal_number==SIGSEGV && transaction.stage==0 && !(context->uc_mcontext.gregs[REG_EFL]&0x100) &&
        n64c_diag_fault && n64c_diag_fault(signal_number,info,context)) return;
    /* Preserve real faults outside the hardware windows. We install only over
     * default handlers; returning retries the fault under the default action. */
    if (signal_number==SIGSEGV && n64c_boot_missing && host_traced()) {
        char subject[32];
        snprintf(subject,sizeof subject,"host-fault-%08x",(unsigned)(uintptr_t)info->si_addr);
        n64c_boot_missing(subject); /* the debugger's stop function; the process ends there */
    }
    for (unsigned i=0;i<3;i++)
        if (signals[i]==signal_number) {
            (void)sigaction(signal_number,&prior[i],NULL);
            return;
        }
    fault_stop();
}
static _Noreturn void dispatch(void)
{
    int ok;
    if (transaction.stage==5) {
        u32 value;
        ok=n64c_mmio_read(transaction.physical,&value);
        if (ok && mprotect(transaction.page,PAGE_BYTES,PROT_READ|PROT_WRITE)==0) {
            *(volatile u32 *)((u8 *)transaction.page+transaction.offset)=value;
            transaction.saved.uc_mcontext.gregs[REG_EFL]|=0x100;
            transaction.stage=2;
        } else ok=0;
    } else if (transaction.stage==4) {
        ok=n64c_mmio_write(transaction.physical,transaction.value);
        if (ok) transaction.stage=3;
    } else if (transaction.stage==6) {
        /* TLB miss: MIPS exception code TLBL (2) for a load, TLBS (3) for a store, in Cause bits 2..6. The guest's
         * fault service maps the page (osMapTLB) and restarts the thread; the access then runs again. Every miss
         * the guest gets is logged with the host instruction that took it, so a fault the guest service cannot
         * resolve (a wild pointer it answers with its crash path) names its code in the boot log. */
        fprintf(stderr,"n64c-fault: TLB-miss-%08x %s at host pc %#lx\n",(unsigned)transaction.physical,
                transaction.write ? "store" : "load",(unsigned long)transaction.saved.uc_mcontext.gregs[REG_EIP]);
        if (!n64c_sched_fault((transaction.write ? 3u : 2u)<<2,transaction.physical)) {
            /* Run-on: the saved context returns from the faulting original function instead. */
            siginfo_t miss;
            memset(&miss,0,sizeof miss);
            miss.si_signo=SIGSEGV;
            miss.si_addr=(void *)(uintptr_t)transaction.physical;
            if (!n64c_diag_fault || !n64c_diag_fault(SIGSEGV,&miss,&transaction.saved))
                tlb_miss_stop(transaction.physical);
        }
        transaction.stage=3;
        ok=1;
    } else ok=0;
    if (!ok) {
        char subject[32];
        snprintf(subject,sizeof subject,"MMIO-%08x",transaction.physical);
        if (!n64c_diag_stop || !n64c_diag_stop(subject,transaction.stage==4 || transaction.stage==5)) {
            fprintf(stderr,"boot-missing: %s\n",subject);
            fflush(stderr);
            _Exit(78);
        }
        /* Run-on: a register read the model refuses reads 0, a refused write is dropped. */
        if (transaction.stage==5 && mprotect(transaction.page,PAGE_BYTES,PROT_READ|PROT_WRITE)==0) {
            *(volatile u32 *)((u8 *)transaction.page+transaction.offset)=0;
            transaction.saved.uc_mcontext.gregs[REG_EFL]|=0x100;
            transaction.stage=2;
        } else if (transaction.stage==4) {
            transaction.stage=3;
        } else fault_stop();
    }
    sigset_t resume;
    sigemptyset(&resume);
    sigaddset(&resume,RESUME_SIGNAL);
    if (sigprocmask(SIG_UNBLOCK,&resume,NULL)!=0 || raise(RESUME_SIGNAL)!=0)
        fault_stop();
    fault_stop(); /* signal return resumes the original native instruction */
}
static int map_at(u32 address,size_t bytes,int protection,int fd,off_t offset)
{
    if (map_count>=MAX_MAPS) return 0;
    void *target=(void *)(uintptr_t)address;
    void *result=mmap(target,bytes,protection,MAP_SHARED|MAP_FIXED_NOREPLACE,fd,offset);
    if (result==MAP_FAILED) return 0;
    if (result!=target) { munmap(result,bytes); return 0; }
    maps[map_count++]=(Mapping){result,bytes};
    return 1;
}
static int map_pair(u32 physical,size_t bytes,int protection,int fd,off_t offset)
{
    return map_at(0x80000000u+physical,bytes,protection,fd,offset) &&
           map_at(0xA0000000u+physical,bytes,protection,fd,offset);
}
void osUnmapTLB(s32 index)
{
    if (index<0 || index>=TLB_ENTRIES) {
        fprintf(stderr,"n64c: osUnmapTLB index %d outside the native TLB model\n",(int)index);
        fault_stop();
    }
    for (unsigned j=0;j<2;j++) {
        TlbMapping *page=&tlb[index][j];
        if (page->base && munmap(page->base,page->bytes)!=0) fault_stop();
        *page=(TlbMapping){0};
    }
}
void osUnmapTLBAll(void)
{
    for (s32 i=0;i<TLB_ENTRIES;i++) osUnmapTLB(i);
}
void osMapTLB(s32 index,u32 pm,void *vaddr,u32 evenpaddr,u32 oddpaddr,s32 asid)
{
    (void)asid;
    const size_t page=(((size_t)pm|0x1FFFu)+1u)/2u;
    if (index<0 || index>=TLB_ENTRIES || window_fd<0 || (uintptr_t)vaddr%(2*page)) {
        fprintf(stderr,"n64c: osMapTLB index %d vaddr %p outside the native TLB model\n",(int)index,vaddr);
        fault_stop();
    }
    osUnmapTLB(index);
    const u32 pages[2]={evenpaddr,oddpaddr};
    for (unsigned j=0;j<2;j++) {
        if (pages[j]==0xFFFFFFFFu) continue;
        const u32 physical=pages[j]&0x1FFFFFFFu;
        void *target=(char *)vaddr+j*page;
        void *result=physical%PAGE_BYTES || physical>ram_size || page>ram_size-physical ? MAP_FAILED :
            mmap(target,page,PROT_READ|PROT_WRITE,MAP_SHARED|MAP_FIXED_NOREPLACE,window_fd,(off_t)physical);
        if (result!=target) {
            fprintf(stderr,"n64c: osMapTLB page 0x%08x -> %p not mappable\n",physical,target);
            fault_stop();
        }
        tlb[index][j]=(TlbMapping){result,page,physical};
    }
}
void n64c_memory_close(void)
{
    osUnmapTLBAll();
    if (window_fd>=0) { close(window_fd);window_fd=-1; }
    while (handlers) {
        --handlers;
        (void)sigaction(signals[handlers],&prior[handlers],NULL);
    }
    while (map_count) {
        --map_count;
        munmap(maps[map_count].base,maps[map_count].bytes);
    }
    ram_size=0;
    transaction.stage=0;
}
int n64c_memory_open(const N64cMemoryConfig *config)
{
    if (!config || map_count || (config->rdram_bytes!=0x400000u &&
        config->rdram_bytes!=0x800000u) || !config->rom_bytes ||
        config->rom_bytes>0x0FC00000u || !config->video_clock_hz ||
        sysconf(_SC_PAGESIZE)!=PAGE_BYTES) return 0;
    for (unsigned i=0;i<3;i++) {
        if (sigaction(signals[i],NULL,&prior[i])!=0 || prior[i].sa_handler!=SIG_DFL)
            return 0;
    }
    size_t cart_bytes=((size_t)config->rom_bytes+PAGE_BYTES-1u)&~(size_t)(PAGE_BYTES-1u);
    int fd=memfd_create("n64c-address-windows",MFD_CLOEXEC);
    if (fd<0) return 0;
    size_t total=(size_t)config->rdram_bytes+cart_bytes+sizeof devices/sizeof devices[0]*PAGE_BYTES;
    int ok=ftruncate(fd,(off_t)total)==0 &&
        map_pair(0,config->rdram_bytes,PROT_READ|PROT_WRITE,fd,0) &&
        map_pair(0x10000000u,cart_bytes,PROT_READ|PROT_WRITE,fd,(off_t)config->rdram_bytes);
    if (ok) {
        ok=n64c_pi_read(0,(void *)(uintptr_t)0x90000000u,config->rom_bytes)==0;
        ok=ok && mprotect((void *)(uintptr_t)0x90000000u,cart_bytes,PROT_READ)==0 &&
            mprotect((void *)(uintptr_t)0xB0000000u,cart_bytes,PROT_READ)==0;
    }
    for (unsigned i=0;ok && i<sizeof devices/sizeof devices[0];i++)
        ok=map_pair(devices[i],PAGE_BYTES,PROT_NONE,fd,
            (off_t)(config->rdram_bytes+cart_bytes+i*PAGE_BYTES));
    if (ok) window_fd=fd; else close(fd);
    if (ok) {
        struct sigaction action;
        memset(&action,0,sizeof action);
        action.sa_sigaction=signal_handler;
        action.sa_flags=SA_SIGINFO;
        sigemptyset(&action.sa_mask);
        for (unsigned i=0;ok && i<3;i++) {
            ok=sigaction(signals[i],&action,NULL)==0;
            if (ok) handlers++;
        }
    }
    if (!ok) { n64c_memory_close(); return 0; }
    ram_size=config->rdram_bytes;
    n64c_mmio_reset(config->video_clock_hz);
    return 1;
}
void *n64c_memory_ram(u32 physical,u32 bytes)
{
    if (physical>ram_size || bytes>ram_size-physical || !ram_size) return NULL;
    return (void *)(uintptr_t)(0x80000000u+physical);
}
u32 n64c_probe_tlb(const void *pointer)
{
    uintptr_t value=(uintptr_t)pointer;
    for (unsigned i=0;i<TLB_ENTRIES;i++)
        for (unsigned j=0;j<2;j++) {
            const TlbMapping *page=&tlb[i][j];
            uintptr_t base=(uintptr_t)page->base;
            if (page->base && value>=base && value-base<page->bytes)
                return page->physical+(u32)(value-base);
        }
    return UINT32_MAX;
}
u32 n64c_memory_physical(const void *pointer)
{
    uintptr_t value=(uintptr_t)pointer;
    if ((value>=0x80000000u && value<0x80000000u+ram_size) ||
        (value>=0xA0000000u && value<0xA0000000u+ram_size)) return (u32)value&0x1FFFFFFFu;
    u32 physical=n64c_probe_tlb(pointer);
    return physical!=UINT32_MAX ? physical : (u32)value;
}
#else
int n64c_memory_open(const N64cMemoryConfig *config) { (void)config; return 0; }
void n64c_memory_close(void) {}
void *n64c_memory_ram(u32 physical,u32 bytes) { (void)physical; (void)bytes; return NULL; }
u32 n64c_memory_physical(const void *pointer) { return (u32)(uintptr_t)pointer; }
u32 n64c_probe_tlb(const void *pointer) { (void)pointer; return UINT32_MAX; }
void osUnmapTLB(s32 index) { (void)index; }
void osUnmapTLBAll(void) {}
void osMapTLB(s32 index,u32 pm,void *vaddr,u32 evenpaddr,u32 oddpaddr,s32 asid)
{
    (void)index;(void)pm;(void)vaddr;(void)evenpaddr;(void)oddpaddr;(void)asid;
}
#endif
