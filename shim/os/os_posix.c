/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Linux backend of the host seam (os/n64c_host.h), for i386 and for 32-bit ARM, Android (bionic) included: fixed-address
 * memfd windows, SIGSEGV/SIGUSR2 fault handling on the ucontext, the fixed-address guest stacks and the stderr tee. The code
 * is the Linux part of n64c_memory.c, n64c_sched.c and boot/game_main.c, moved here unchanged in behaviour; the ARM and
 * Android parts are PORT-ANDROID-2's (an A32 word transfer is decoded in n64c_memory.c, its registers are read here). */
#define _GNU_SOURCE
#include "n64c_host.h"
#include "../n64c_memory.h"
#include "../n64c_diag.h"

#if defined(__linux__)
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <ucontext.h>
#include <unistd.h>
#ifdef __ANDROID__
#include <dlfcn.h>
#endif
#if defined(__ANDROID__) && __ANDROID_API__ < 30
/* bionic declares memfd_create from API 30; the system call is there since Linux 3.17. */
#ifndef MFD_CLOEXEC
#define MFD_CLOEXEC 0x0001U
#endif
static int memfd_create(const char *name,unsigned flags) { return (int)syscall(SYS_memfd_create,name,flags); }
#endif

#ifdef N64C_HOST_NATIVE
enum { RESUME_SIGNAL=SIGUSR2, SIGNAL_COUNT=2 };
static int window_fd=-1;
static unsigned handlers;
static struct sigaction prior[SIGNAL_COUNT];
static const int signals[SIGNAL_COUNT]={SIGSEGV,RESUME_SIGNAL};
_Static_assert(sizeof(ucontext_t)<=sizeof(N64cHostContext),"N64cHostContext holds a ucontext_t");
#ifdef __ANDROID__
/* The game is a shared library inside the app process (no __executable_start of its own): the guest KUSEG is every
 * address below KSEG0. */
#else
extern char __executable_start[];
#endif

size_t n64c_host_page_size(void) { return (size_t)sysconf(_SC_PAGESIZE); }
size_t n64c_host_granule(void) { return 4096; }
int n64c_host_store_create(size_t bytes)
{
    int fd=memfd_create("n64c-address-windows",MFD_CLOEXEC);
    if (fd<0) return 0;
    if (ftruncate(fd,(off_t)bytes)!=0) { close(fd);return 0; }
    window_fd=fd;
    return 1;
}
void n64c_host_store_close(void)
{
    if (window_fd>=0) { close(window_fd);window_fd=-1; }
}
static int native_protection(int protection)
{
    return protection==N64C_PROT_RW ? PROT_READ|PROT_WRITE : protection==N64C_PROT_READ ? PROT_READ : PROT_NONE;
}
/* The guest windows sit at fixed addresses the host allocator knows nothing about: the Android emulator's goldfish address
 * space (or any library loaded later) can be placed on one, and n64c_memory_open then refuses on its first launch. A load-time
 * constructor therefore holds every window the open maps as PROT_NONE anonymous memory; n64c_host_map takes a window over
 * from its hold, n64c_host_unmap gives it back. A window already taken stays unheld and the open refuses as before. The cart
 * windows are held at the open's own ROM limit (n64c_memory_open refuses more than 0x0FC00000). */
enum { HOLD_COUNT=2*(2+4) };
static struct { uintptr_t base;size_t bytes; } hold[HOLD_COUNT];
static int held(uintptr_t address,size_t bytes)
{
    for (unsigned i=0;i<HOLD_COUNT;i++)
        if (hold[i].bytes && address>=hold[i].base && bytes<=hold[i].bytes-(address-hold[i].base)) return 1;
    return 0;
}
static void hold_window(uintptr_t base,size_t bytes)
{
    for (unsigned i=0;i<HOLD_COUNT;i++) {
        if (hold[i].bytes) continue;
        void *got=mmap((void *)base,bytes,PROT_NONE,MAP_PRIVATE|MAP_ANONYMOUS|MAP_NORESERVE|MAP_FIXED_NOREPLACE,-1,0);
        if (got==MAP_FAILED) return;
        if (got!=(void *)base) { munmap(got,bytes);return; }
        hold[i].base=base;
        hold[i].bytes=bytes;
        return;
    }
}
__attribute__((constructor)) static void hold_windows(void)
{
    static const uintptr_t devices[]={0x04400000,0x04500000,0x04600000,0x04800000};
    for (unsigned k=0;k<2;k++) {
        uintptr_t segment=k ? 0xA0000000u : 0x80000000u;
        hold_window(segment,0x800000u);                  /* RDRAM: 8 MiB with the expansion pak */
        hold_window(segment+0x10000000u,0x0FC00000u);    /* cart */
        for (unsigned i=0;i<4;i++) hold_window(segment+devices[i],4096);
    }
}
void *n64c_host_map(uintptr_t address,size_t bytes,size_t offset,int protection)
{
    void *target=(void *)address;
    int take=held(address,bytes);
    void *result=mmap(target,bytes,native_protection(protection),MAP_SHARED|(take ? MAP_FIXED : MAP_FIXED_NOREPLACE),window_fd,(off_t)offset);
    if (result==MAP_FAILED) return NULL;
    if (result!=target) { munmap(result,bytes);return NULL; }
    return result;
}
int n64c_host_protect(void *base,size_t bytes,int protection)
{
    return mprotect(base,bytes,native_protection(protection));
}
int n64c_host_unmap(void *base,size_t bytes)
{
    if (held((uintptr_t)base,bytes))
        return mmap(base,bytes,PROT_NONE,MAP_PRIVATE|MAP_ANONYMOUS|MAP_NORESERVE|MAP_FIXED,-1,0)==MAP_FAILED ? -1 : 0;
    return munmap(base,bytes);
}
int n64c_host_tlb_map(void *vaddr,size_t bytes,uint32_t physical)
{
    return n64c_host_map((uintptr_t)vaddr,bytes,physical,N64C_PROT_RW)==vaddr ? 0 : -1;
}
int n64c_host_tlb_unmap(void *vaddr,size_t bytes) { return munmap(vaddr,bytes); }
#ifdef __ANDROID__
uintptr_t n64c_host_image_start(void) { return (uintptr_t)0x80000000u; }
#else
uintptr_t n64c_host_image_start(void) { return (uintptr_t)__executable_start; }
#endif
void n64c_host_ram_prepare(void *pointer,size_t bytes) { (void)pointer;(void)bytes; }

/* A debugger holds the process: /proc/self/status names a tracer. */
int n64c_host_traced(void)
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

/* Words at a host address, 1 when all were readable (a bad frame pointer is no second fault). */
static int read_words_at(uintptr_t at,u32 *words,unsigned count)
{
    struct iovec local={words,count*sizeof *words},remote={(void *)at,count*sizeof *words};
    return process_vm_readv(getpid(),&local,1,&remote,1,0)==(ssize_t)(count*sizeof *words);
}
#if defined(__arm__)
/* The ARM registers r0..r15 of a signal context are one run of words (sigcontext arm_r0..arm_pc). */
#define ARM_REGS(c) (&(c)->uc_mcontext.arm_r0)
uintptr_t n64c_host_pc(const void *ctx) { return (uintptr_t)((const ucontext_t *)ctx)->uc_mcontext.arm_pc; }
void n64c_host_set_pc(void *ctx,uintptr_t pc) { ((ucontext_t *)ctx)->uc_mcontext.arm_pc=(unsigned long)pc; }
uint32_t n64c_host_reg(const void *ctx,unsigned reg) { return (uint32_t)ARM_REGS((const ucontext_t *)ctx)[reg]; }
void n64c_host_set_reg(void *ctx,unsigned reg,uint32_t value) { ARM_REGS((ucontext_t *)ctx)[reg]=value; }
static uintptr_t context_sp(const ucontext_t *c) { return (uintptr_t)c->uc_mcontext.arm_sp; }
/* The faulting access was a write: ARM records it in the fault status (bit 11, WnR) of the context. */
static int context_write(const ucontext_t *c) { return (c->uc_mcontext.error_code&(1ul<<11))!=0; }
void n64c_host_ctx_copy(void *dst,const void *src) { memcpy(dst,src,sizeof(ucontext_t)); }
void n64c_host_redirect(void *ctx,uintptr_t stack_top,void (*fn)(void))
{
    ucontext_t *context=ctx;
    context->uc_mcontext.arm_sp=(unsigned long)(stack_top&~(uintptr_t)7);
    context->uc_mcontext.arm_lr=0;
    const uintptr_t entry=(uintptr_t)fn;
    context->uc_mcontext.arm_pc=(unsigned long)(entry&~(uintptr_t)1);
    if (entry&1u) context->uc_mcontext.arm_cpsr|=(1ul<<5); else context->uc_mcontext.arm_cpsr&=~(1ul<<5); /* Thumb state */
}
#else
static const int gpr[8]={REG_EAX,REG_ECX,REG_EDX,REG_EBX,REG_ESP,REG_EBP,REG_ESI,REG_EDI};
uintptr_t n64c_host_pc(const void *ctx) { return (uintptr_t)((const ucontext_t *)ctx)->uc_mcontext.gregs[REG_EIP]; }
void n64c_host_set_pc(void *ctx,uintptr_t pc) { ((ucontext_t *)ctx)->uc_mcontext.gregs[REG_EIP]=(greg_t)pc; }
uint32_t n64c_host_reg(const void *ctx,unsigned reg) { return (uint32_t)((const ucontext_t *)ctx)->uc_mcontext.gregs[gpr[reg]]; }
void n64c_host_set_reg(void *ctx,unsigned reg,uint32_t value) { ((ucontext_t *)ctx)->uc_mcontext.gregs[gpr[reg]]=(greg_t)value; }
static int context_write(const ucontext_t *c) { return (c->uc_mcontext.gregs[REG_ERR]&2)!=0; }
void n64c_host_ctx_copy(void *dst,const void *src) { memcpy(dst,src,sizeof(ucontext_t)); }
void n64c_host_redirect(void *ctx,uintptr_t stack_top,void (*fn)(void))
{
    ucontext_t *context=ctx;
    context->uc_mcontext.gregs[REG_ESP]=(greg_t)stack_top;
    context->uc_mcontext.gregs[REG_EIP]=(greg_t)(uintptr_t)fn;
    context->uc_mcontext.gregs[REG_EFL]&=~(greg_t)0x400;
}
#endif
#ifdef __ANDROID__
void n64c_host_describe(const char *what,const N64cHostFault *fault,int caller)
{
    uintptr_t pc=fault->pc;
    if (caller) {
#if defined(__arm__)
        pc=(uintptr_t)((const ucontext_t *)fault->context)->uc_mcontext.arm_lr;
#else
        pc=0;
        if (!read_words_at((uintptr_t)((const ucontext_t *)fault->context)->uc_mcontext.gregs[REG_ESP],(u32 *)&pc,1)) return;
#endif
    }
    Dl_info where;
    if (dladdr((void *)pc,&where) && where.dli_fbase)
        fprintf(stderr,"n64c-fault: %s %#lx (%s+%#lx, %s)\n",what,(unsigned long)pc,
                where.dli_sname ? where.dli_sname : "?",
                (unsigned long)(pc-(uintptr_t)(where.dli_saddr ? where.dli_saddr : where.dli_fbase)),
                where.dli_fname ? where.dli_fname : "?");
    else
        fprintf(stderr,"n64c-fault: %s %#lx\n",what,(unsigned long)pc);
    fflush(stderr);
}
#else
void n64c_host_describe(const char *what,const N64cHostFault *fault,int caller) { (void)what;(void)fault;(void)caller; }
#endif
_Noreturn void n64c_host_resume(void)
{
    static const char message[]="n64compat: invalid MMIO fault\n";
    sigset_t resume;
    sigemptyset(&resume);
    sigaddset(&resume,RESUME_SIGNAL);
    if (sigprocmask(SIG_UNBLOCK,&resume,NULL)==0)
        (void)raise(RESUME_SIGNAL); /* signal return resumes the original native instruction */
    (void)!write(STDERR_FILENO,message,sizeof message-1);
    _Exit(78);
}
int n64c_host_diag_tlb_miss(uintptr_t address,void *saved_context)
{
    siginfo_t miss;
    memset(&miss,0,sizeof miss);
    miss.si_signo=SIGSEGV;
    miss.si_addr=(void *)address;
    return n64c_diag_fault && n64c_diag_fault(SIGSEGV,&miss,saved_context);
}

#if defined(__arm__)
int n64c_memory_fault_json(char *out,size_t cap,int signo,const void *given,const void *opaque,int tid)
{
    const siginfo_t *info=given;
    const ucontext_t *context=opaque;
    const unsigned long *r=&context->uc_mcontext.arm_r0;
    size_t used=0;
    u32 words[8];
#define PUT(...) do { int n_=snprintf(out+used,cap-used,__VA_ARGS__); if (n_<0 || (size_t)n_>=cap-used) return 0; used+=(size_t)n_; } while (0)
    PUT("{\"signal\":%d,\"code\":%d,\"si_addr\":\"0x%08x\",\"access\":\"%s\",\"tid\":%d,\"pc\":\"0x%08x\",\"regs\":{",
        signo,info->si_code,(unsigned)(uintptr_t)info->si_addr,context_write(context) ? "write" : "read",tid,
        (unsigned)context->uc_mcontext.arm_pc);
    for (unsigned i=0;i<15;i++) PUT("%s\"r%u\":\"0x%08x\"",i ? "," : "",i,(unsigned)r[i]);
    PUT("},\"stack\":[");
    const unsigned stack_words=read_words_at(context_sp(context),words,8) ? 8 : 0;
    for (unsigned i=0;i<stack_words;i++) PUT("%s\"0x%08x\"",i ? "," : "",words[i]);
    PUT("]}\n");
#undef PUT
    return (int)used;
}
#else
int n64c_memory_fault_json(char *out,size_t cap,int signo,const void *given,const void *opaque,int tid)
{
    const siginfo_t *info=given;
    const greg_t *g=((const ucontext_t *)opaque)->uc_mcontext.gregs;
    static const struct { const char *name; int reg; } regs[]={{"eax",REG_EAX},{"ecx",REG_ECX},{"edx",REG_EDX},
        {"ebx",REG_EBX},{"esp",REG_ESP},{"ebp",REG_EBP},{"esi",REG_ESI},{"edi",REG_EDI}};
    const unsigned err=(unsigned)g[REG_ERR];
    size_t used=0;
    u32 words[8];
#define PUT(...) do { int n_=snprintf(out+used,cap-used,__VA_ARGS__); if (n_<0 || (size_t)n_>=cap-used) return 0; used+=(size_t)n_; } while (0)
    PUT("{\"signal\":%d,\"code\":%d,\"si_addr\":\"0x%08x\",\"access\":\"%s\",\"tid\":%d,\"eip\":\"0x%08x\","
        "\"eflags\":\"0x%08x\",\"regs\":{",signo,info->si_code,(unsigned)(uintptr_t)info->si_addr,
        (err&16) ? "exec" : (err&2) ? "write" : "read",tid,(unsigned)g[REG_EIP],(unsigned)g[REG_EFL]);
    for (unsigned i=0;i<sizeof regs/sizeof *regs;i++) PUT("%s\"%s\":\"0x%08x\"",i ? "," : "",regs[i].name,(unsigned)g[regs[i].reg]);
    PUT("},\"stack\":[");
    const unsigned stack_words=read_words_at((uintptr_t)g[REG_ESP],words,8) ? 8 : 0;
    for (unsigned i=0;i<stack_words;i++) PUT("%s\"0x%08x\"",i ? "," : "",words[i]);
    PUT("],\"args\":[");
    if (read_words_at((uintptr_t)g[REG_EBP]+8,words,4))
        for (unsigned i=0;i<4;i++) PUT("%s\"0x%08x\"",i ? "," : "",words[i]);
    PUT("],\"frames\":[");
    uintptr_t frame=(uintptr_t)g[REG_EBP];
    for (unsigned depth=0;depth<8 && read_words_at(frame,words,2);depth++) {
        PUT("%s\"0x%08x\"",depth ? "," : "",words[1]);
        if (words[0]<=frame) break;
        frame=words[0];
    }
    PUT("]}\n");
#undef PUT
    return (int)used;
}
#endif
/* Where a host fault is recorded for the supervisor: N64C_FAULT_FRAME names the file (the run folder's). */
static void fault_record(const siginfo_t *info,const ucontext_t *context)
{
    const char *path=getenv("N64C_FAULT_FRAME");
    char text[2048];
    const int length=path ? n64c_memory_fault_json(text,sizeof text,SIGSEGV,info,context,(int)syscall(SYS_gettid)) : 0;
    const int fd=length>0 ? open(path,O_WRONLY|O_CREAT|O_TRUNC,0644) : -1;
    if (fd<0) return;
    (void)write(fd,text,(size_t)length);
    (void)close(fd);
}
static _Noreturn void fault_stop(void)
{
    static const char message[]="n64compat: invalid MMIO fault\n";
    (void)write(STDERR_FILENO,message,sizeof message-1);
    _Exit(78);
}
static void signal_handler(int signal_number,siginfo_t *info,void *opaque)
{
    ucontext_t *context=opaque;
    if (signal_number==RESUME_SIGNAL) {
        if (n64c_memory_on_resume(context)) return;
    } else {
        const N64cHostFault fault={(uintptr_t)info->si_addr,n64c_host_pc(context),context_write(context),context,info};
        if (n64c_memory_on_fault(&fault)) return;
        fault_record(info,context);
        /* Preserve real faults outside the hardware windows. We install only over
         * default handlers; returning retries the fault under the default action. */
        if (n64c_boot_missing && n64c_host_traced()) {
            char subject[32];
            snprintf(subject,sizeof subject,"host-fault-%08x",(unsigned)(uintptr_t)info->si_addr);
            n64c_boot_missing(subject); /* the debugger's stop function; the process ends there */
        }
    }
    for (unsigned i=0;i<SIGNAL_COUNT;i++)
        if (signals[i]==signal_number) {
            (void)sigaction(signal_number,&prior[i],NULL);
            return;
        }
    fault_stop();
}
int n64c_host_fault_install(void)
{
    for (unsigned i=0;i<SIGNAL_COUNT;i++) {
#ifdef __ANDROID__
        /* bionic installs its crash handler (debuggerd) on SIGSEGV in every process and the runtime chains behind it: the
         * previous action is that handler, kept to be restored when a fault is not ours (a genuine crash then gets its
         * tombstone). The resume signal must still be unclaimed. */
        if (sigaction(signals[i],NULL,&prior[i])!=0 || (signals[i]!=SIGSEGV && prior[i].sa_handler!=SIG_DFL))
            return 0;
#else
        if (sigaction(signals[i],NULL,&prior[i])!=0 || prior[i].sa_handler!=SIG_DFL)
            return 0;
#endif
    }
    struct sigaction action;
    memset(&action,0,sizeof action);
    action.sa_sigaction=signal_handler;
    action.sa_flags=SA_SIGINFO;
    sigemptyset(&action.sa_mask);
    int ok=1;
    for (unsigned i=0;ok && i<SIGNAL_COUNT;i++) {
        ok=sigaction(signals[i],&action,NULL)==0;
        if (ok) handlers++;
    }
    if (!ok) n64c_host_fault_uninstall();
    return ok;
}
void n64c_host_fault_uninstall(void)
{
    while (handlers) {
        --handlers;
        (void)sigaction(signals[handlers],&prior[handlers],NULL);
    }
}
#endif /* N64C_HOST_NATIVE */
#endif /* __linux__ */
