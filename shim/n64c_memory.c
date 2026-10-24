/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Address facts: https://n64.readthedocs.io/#memory-map
 * Native adapter (Linux/i386 and Windows/i386, through the host seam os/n64c_host.h): a protected register page lets
 * the CPU execute one native access to a protected register page faults; the handler decodes that one native mov, and the component call
 * runs on a separate TLS stack after the fault handler returned, with the result written into the saved context. A resume
 * restores it past the instruction. No single-step (so the same under a debugger), guest CPU, ptrace or callbacks
 * in signal context. The decoder knows only the 32-bit mov forms; any other access stops by name. */
#include "n64c_memory.h"
#include "n64c_internal.h"
#include "n64c_mmio.h"
#include "n64c_diag.h"
#include "os/n64c_host.h"

/* The ARM word of an A32 single data transfer (LDR/STR, word): what the CPU executed when it faulted on a register
 * page. Pure, so the decoder runs in the host test. Refused (0): other encodings (halfword, byte, doubleword,
 * multiple, VFP, Thumb), condition 1111, a base or transfer register that is the PC. */
int n64c_memory_decode_arm(u32 insn,N64cArmTransfer *out)
{
    if (((insn>>26)&3u)!=1u || (insn>>28)==0xFu) return 0;       /* load/store word or byte, not the NV space */
    const unsigned I=(insn>>25)&1u,P=(insn>>24)&1u,U=(insn>>23)&1u,B=(insn>>22)&1u,W=(insn>>21)&1u,L=(insn>>20)&1u;
    const unsigned rn=(insn>>16)&15u,rt=(insn>>12)&15u;
    if (B || rn==15u || rt==15u) return 0;
    out->load=(int)L;
    out->rt=rt;
    out->rn=rn;
    out->up=(int)U;
    out->pre=(int)P;
    out->writeback=(int)(!P || W);
    out->register_offset=(int)I;
    out->immediate=insn&0xFFFu;
    out->rm=insn&15u;
    out->shift_type=(insn>>5)&3u;
    out->shift_amount=(insn>>7)&31u;
    if (I && (out->rm==15u || (insn&0x10u))) return 0;            /* register-shifted register forms do not exist here */
    if (!P && W) return 0;                                       /* LDRT/STRT: unprivileged transfer */
    if (out->writeback && (rn==rt)) return 0;                    /* unpredictable */
    return 1;
}
/* The offset the instruction added to (or subtracted from) the base: an immediate, or Rm shifted by an immediate. */
u32 n64c_memory_arm_offset(const N64cArmTransfer *t,const u32 regs[16])
{
    if (!t->register_offset) return t->immediate;
    u32 v=regs[t->rm];
    const unsigned n=t->shift_amount;
    switch (t->shift_type) {
    case 0: return v<<n;                                          /* LSL */
    case 1: return n ? v>>n : 0;                                  /* LSR #32 is 0 */
    case 2: return n ? (u32)((int32_t)v>>n) : (u32)((int32_t)v>>31); /* ASR #32 is the sign */
    default: return n ? (v>>n)|(v<<(32-n)) : v;                   /* ROR (RRX with carry is never emitted for MMIO) */
    }
}
#ifdef N64C_HOST_NATIVE
#include <errno.h>
#include <signal.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

enum { PAGE_BYTES=4096, MAX_MAPS=12, TLB_ENTRIES=32 };
static const u32 devices[]={0x04400000,0x04500000,0x04600000,0x04800000};
typedef struct { void *base; size_t bytes; } Mapping;
static Mapping maps[MAX_MAPS];
static unsigned map_count;
static u32 ram_size;
static int store_open;                 /* the backing store behind every window, kept for TLB mappings */
typedef struct { void *base; size_t bytes; u32 physical; } TlbMapping;
static TlbMapping tlb[TLB_ENTRIES][2];
static _Thread_local struct {
    N64cHostContext saved;
    u32 physical, value;
    unsigned length, reg;
    int stage, write, error;
    unsigned char stack[65536]; /* its top is aligned by hand (redirect): thread-local storage promises no more than 4 */
} transaction;

static _Noreturn void fault_stop_at(int line)
{
    static const char message[]="n64compat: invalid MMIO fault\n";
#ifdef _WIN32
    char where[48];
    const int n=snprintf(where,sizeof where,"n64compat: fault stop at n64c_memory.c:%d\n",line);
    if (n>0) (void)write(STDERR_FILENO,where,(size_t)n);
#else
    (void)line;
#endif
    (void)write(STDERR_FILENO,message,sizeof message-1);
    _Exit(78);
}
#define fault_stop() fault_stop_at(__LINE__)
static _Noreturn void dispatch(void);

static void redirect(void *context)
{
    uintptr_t top=(uintptr_t)(transaction.stack+sizeof transaction.stack);
    top=(top & ~(uintptr_t)15)-sizeof(uintptr_t);
    *(uintptr_t *)top=0; /* dispatch never returns */
    n64c_host_redirect(context,top,(void (*)(void))dispatch);
}
static int device_page(uintptr_t address,u32 *physical)
{
    if (address<0x80000000u || address>=0xC0000000u) return 0;
    *physical=(u32)address & 0x1FFFFFFFu;
    for (unsigned i=0;i<sizeof devices/sizeof devices[0];i++)
        if (*physical>=devices[i] && *physical<devices[i]+PAGE_BYTES) return 1;
    return 0;
}
/* A guest KUSEG address the guest maps through the TLB (osMapTLB): below the host image, where the host itself
 * maps nothing, and above the lowest 64 KiB that the host never maps (Linux vm.mmap_min_addr, the Windows null
 * region), so a null-pointer access stays a host fault. */
static int guest_virtual(uintptr_t address)
{
    return ram_size && address>=0x10000u && address<n64c_host_image_start();
}
/* Every named stop ends the same way: under a debugger through the stop function it breaks on, so the stop is
 * held with its stacks; otherwise the line is written and the process exits 78. `line` is "boot-missing: S\n". */
static _Noreturn void named_stop(const char *line,size_t length)
{
    if (N64C_LINKED(n64c_boot_missing) && n64c_host_traced()) {
        char subject[64];
        size_t start=sizeof "boot-missing: "-1,n=length-start-1;
        if (n>=sizeof subject) n=sizeof subject-1;
        memcpy(subject,line+start,n);
        subject[n]=0;
        n64c_boot_missing(subject); /* prints the line itself; the process ends there */
    }
    (void)write(STDERR_FILENO,line,length);
    _Exit(78);
}
/* A guest access to a virtual page no TLB entry maps when nothing services it (off a guest thread, or no
 * OS_EVENT_FAULT queue): the boot stops here by name instead of crashing. */
static _Noreturn void tlb_miss_stop(u32 address)
{
    char line[]="boot-missing: TLB-miss-00000000\n";
    static const char hex[]="0123456789abcdef";
    for (int i=0;i<8;i++) line[sizeof line-3-i]=hex[(address>>(4*i))&15u];
    named_stop(line,sizeof line-1);
}
/* The CPU jumped to a guest address: a call through a code pointer the data holds as the original's raw value
 * (KSEG0 0x80xxxxxx or a KUSEG alias), which is no native function. Native code never runs from guest memory, so
 * the boot stops by name instead of the fault reaching the guest's own fault service. */
static _Noreturn void code_pointer_stop(u32 address)
{
    char line[]="boot-missing: code-pointer-00000000\n";
    static const char hex[]="0123456789abcdef";
    for (int i=0;i<8;i++) line[sizeof line-3-i]=hex[(address>>(4*i))&15u];
    named_stop(line,sizeof line-1);
}
static int guest_code_fetch(const N64cHostFault *fault)
{
    if (fault->pc!=fault->address || !ram_size) return 0;
    return guest_virtual(fault->address) || (fault->address>=0x80000000u && fault->address<0xC0000000u);
}
/* Decode the one native mov that faulted on a register page: 8B/89 mov r32,r/m32 and r/m32,r32, C7 /0 mov
 * r/m32,imm32, A1/A3 mov eax,moffs32 and moffs32,eax. The address is the fault's, so only the length, the direction
 * and the register are read; anything else (prefixes, byte or 16-bit access, read-modify-write) is refused. */
int n64c_memory_decode_mov(const unsigned char *code,unsigned *length,unsigned *reg,unsigned *write,u32 *immediate)
{
    unsigned n=1;
    const unsigned char op=code[0];
    *immediate=0;
    if (op==0xA1 || op==0xA3) {
        *length=5; *reg=0; *write=op==0xA3;
        return 1;
    }
    if (op!=0x8B && op!=0x89 && op!=0xC7) return 0;
    const unsigned char modrm=code[n++];
    const unsigned mod=modrm>>6,rm=modrm&7;
    if (mod==3) return 0;
    if (rm==4) {
        const unsigned char sib=code[n++];
        if (mod==0 && (sib&7)==5) n+=4;
    } else if (mod==0 && rm==5) n+=4;
    if (mod==1) n+=1;
    else if (mod==2) n+=4;
    *reg=(modrm>>3)&7;
    *write=op!=0x8B;
    if (op==0xC7) {
        if (*reg!=0) return 0;
        memcpy(immediate,code+n,4);
        n+=4;
    }
    *length=n;
    return 1;
}
#if defined(__arm__)
/* The decoded ARM transfer of the faulting instruction, kept for mmio_finish. */
static _Thread_local N64cArmTransfer arm_transfer;
static int decode_mov(const N64cHostFault *fault,unsigned *length,unsigned *reg,unsigned *write,u32 *value)
{
    N64cArmTransfer t;
    if (!n64c_memory_decode_arm(*(const u32 *)fault->pc,&t)) return 0; /* the decoded instruction says whether it stores */
    arm_transfer=t;
    *length=4;
    *reg=t.rt;
    *write=(unsigned)!t.load;
    if (*write) *value=n64c_host_reg(fault->context,t.rt);
    return 1;
}
#else
static int decode_mov(const N64cHostFault *fault,unsigned *length,unsigned *reg,unsigned *write,u32 *value)
{
    const unsigned char *code=(const unsigned char *)fault->pc;
    u32 immediate;
    if (!n64c_memory_decode_mov(code,length,reg,write,&immediate) || (int)*write!=fault->write) return 0;
    if (*write) *value=code[0]==0xC7 ? immediate : n64c_host_reg(fault->context,*reg);
    return 1;
}
#endif
static _Noreturn void mmio_decode_stop(const N64cHostFault *fault)
{
    const unsigned char *code=(const unsigned char *)fault->pc;
    static const char hex[]="0123456789abcdef";
    char line[]="boot-missing: MMIO-instruction 000000000000\n";
    for (int i=0;i<6;i++) {
        line[sizeof line-14+2*i]=hex[code[i]>>4];
        line[sizeof line-13+2*i]=hex[code[i]&15];
    }
    named_stop(line,sizeof line-1);
}
/* The component call is done: a read lands in the register, and the saved context moves past the instruction. */
static void mmio_finish(u32 value)
{
#if defined(__arm__)
    const N64cArmTransfer *t=&arm_transfer;
    if (t->writeback) { /* pre-indexed with writeback: the base becomes the address; post-indexed: the offset is added */
        u32 snapshot[16];
        for (unsigned i=0;i<16;i++) snapshot[i]=n64c_host_reg(&transaction.saved,i);
        const u32 offset=n64c_memory_arm_offset(t,snapshot);
        const u32 base=snapshot[t->rn];
        n64c_host_set_reg(&transaction.saved,t->rn,t->up ? base+offset : base-offset);
    }
    if (!transaction.write) n64c_host_set_reg(&transaction.saved,t->rt,value);
#else
    if (!transaction.write) {
        const unsigned char *code=(const unsigned char *)n64c_host_pc(&transaction.saved);
        const unsigned reg=code[0]==0xA1 ? 0 : transaction.reg;
        n64c_host_set_reg(&transaction.saved,reg,value);
    }
#endif
    n64c_host_set_pc(&transaction.saved,n64c_host_pc(&transaction.saved)+transaction.length);
}
/* A debugger holds the process (the tile runs under gdb). It stops on every signal the handlers below take
 * on purpose, so the supervisor passes SIGSEGV and SIGUSR2 through; a genuine host fault then dies in
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
/* The module's part of a fault: the OS backend calls this first and chains to the OS default when it returns 0. */
int n64c_memory_on_fault(const N64cHostFault *fault)
{
    void *context=fault->context;
    if (guest_code_fetch(fault)) {
        n64c_host_describe("code-pointer call from",fault,1);
        if (n64c_diag_code_pointer && n64c_diag_code_pointer((u32)fault->address,context)) return 1;
        code_pointer_stop((u32)fault->address);
    }
    u32 physical;
    if (transaction.stage==0 && device_page(fault->address,&physical)) {
        unsigned length,reg,write;
        u32 value=0;
        if ((physical & 3u) || !decode_mov(fault,&length,&reg,&write,&value)) mmio_decode_stop(fault);
        transaction.error=errno;
        transaction.physical=physical;
        transaction.write=(int)write;
        transaction.length=length;
        transaction.reg=reg;
        transaction.value=value;
        n64c_host_ctx_copy(&transaction.saved,context);
        transaction.stage=5;
        redirect(context);
        return 1;
    }
    if (transaction.stage==0 && guest_virtual(fault->address)) {
        if (!n64c_sched_on_guest()) {
            if (n64c_diag_fault &&
                n64c_diag_fault(SIGSEGV,fault->info,context)) return 1;
            n64c_host_describe("host thread TLB-miss at",fault,0); /* a host thread, not a guest thread */
            tlb_miss_stop((u32)fault->address);
        }
        transaction.error=errno;
        transaction.physical=(u32)fault->address; /* the guest virtual address (badvaddr) */
        transaction.write=fault->write;
        n64c_host_ctx_copy(&transaction.saved,context);
        transaction.stage=6;
        redirect(context);
        return 1;
    }
    /* Run-on (the walk's diagnostic boot) records a host fault and returns from the faulting original function. */
    if (transaction.stage==0 && n64c_diag_fault && n64c_diag_fault(SIGSEGV,fault->info,context)) return 1;
    return 0;
}
int n64c_memory_on_resume(void *context)
{
    if (transaction.stage!=3) return 0;
    n64c_host_ctx_copy(context,&transaction.saved);
    transaction.stage=0;
    errno=transaction.error;
    return 1;
}
static _Noreturn void dispatch(void)
{
    int ok;
    if (transaction.stage==5) {
        u32 value=transaction.value;
        ok=transaction.write ? n64c_mmio_write(transaction.physical,value) : n64c_mmio_read(transaction.physical,&value);
        if (ok) {
            mmio_finish(value);
            transaction.stage=3;
        }
    } else if (transaction.stage==6) {
        /* TLB miss: MIPS exception code TLBL (2) for a load, TLBS (3) for a store, in Cause bits 2..6. The guest's
         * fault service maps the page (osMapTLB) and restarts the thread; the access then runs again. Every miss
         * the guest gets is logged with the host instruction that took it, so a fault the guest service cannot
         * resolve (a wild pointer it answers with its crash path) names its code in the boot log. */
        fprintf(stderr,"n64c-fault: TLB-miss-%08x %s at host pc %#lx\n",(unsigned)transaction.physical,
                transaction.write ? "store" : "load",(unsigned long)n64c_host_pc(&transaction.saved));
        if (!n64c_sched_fault((transaction.write ? 3u : 2u)<<2,transaction.physical)) {
            /* Run-on: the saved context returns from the faulting original function instead. */
            if (!n64c_host_diag_tlb_miss(transaction.physical,&transaction.saved))
                tlb_miss_stop(transaction.physical);
        }
        transaction.stage=3;
        ok=1;
    } else ok=0;
    if (!ok) {
        char subject[32];
        snprintf(subject,sizeof subject,"MMIO-%08x",transaction.physical);
        if (!n64c_diag_stop || !n64c_diag_stop(subject,transaction.stage==5)) {
            fprintf(stderr,"boot-missing: %s\n",subject);
            fflush(stderr);
            _Exit(78);
        }
        /* Run-on: a register read the model refuses reads 0, a refused write is dropped. */
        if (transaction.stage==5) {
            mmio_finish(0);
            transaction.stage=3;
        } else fault_stop();
    }
    n64c_host_resume(); /* the resume restores the saved context: the original native instruction is past */
}
static int map_at(u32 address,size_t bytes,int protection,size_t offset)
{
    if (map_count>=MAX_MAPS) return 0;
    void *result=n64c_host_map((uintptr_t)address,bytes,offset,protection);
    if (!result) return 0;
    maps[map_count++]=(Mapping){result,bytes};
    return 1;
}
static int map_pair(u32 physical,size_t bytes,int protection,size_t offset)
{
    return map_at(0x80000000u+physical,bytes,protection,offset) &&
           map_at(0xA0000000u+physical,bytes,protection,offset);
}
static int tlb_trace(void)
{
    static int on=-1;
    if (on<0) { const char *text=getenv("N64C_TLB_TRACE"); on=text && *text && *text!='0'; }
    return on;
}
void osUnmapTLB(s32 index)
{
    if (tlb_trace()) fprintf(stderr,"n64c-tlb: unmap %d\n",(int)index);
    if (index<0 || index>=TLB_ENTRIES) {
        fprintf(stderr,"n64c: osUnmapTLB index %d outside the native TLB model\n",(int)index);
        fault_stop();
    }
    for (unsigned j=0;j<2;j++) {
        TlbMapping *page=&tlb[index][j];
        if (page->base && n64c_host_tlb_unmap(page->base,page->bytes)!=0) fault_stop();
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
    if (tlb_trace()) fprintf(stderr,"n64c-tlb: map %d pm %#x v %p even %#x odd %#x\n",(int)index,(unsigned)pm,vaddr,(unsigned)evenpaddr,(unsigned)oddpaddr);
    const size_t page=(((size_t)pm|0x1FFFu)+1u)/2u;
    if (index<0 || index>=TLB_ENTRIES || !store_open || (uintptr_t)vaddr%(2*page)) {
        fprintf(stderr,"n64c: osMapTLB index %d vaddr %p outside the native TLB model\n",(int)index,vaddr);
        fault_stop();
    }
    osUnmapTLB(index);
    const u32 pages[2]={evenpaddr,oddpaddr};
    for (unsigned j=0;j<2;j++) {
        if (pages[j]==0xFFFFFFFFu) continue;
        const u32 physical=pages[j]&0x1FFFFFFFu;
        void *target=(char *)vaddr+j*page;
        int failed=physical%PAGE_BYTES || physical>ram_size || page>ram_size-physical ||
            n64c_host_tlb_map(target,page,physical)!=0;
        if (failed) {
            fprintf(stderr,"n64c: osMapTLB page 0x%08x -> %p not mappable\n",physical,target);
            fault_stop();
        }
        tlb[index][j]=(TlbMapping){target,page,physical};
    }
}
void n64c_memory_close(void)
{
    osUnmapTLBAll();
    if (store_open) { n64c_host_store_close();store_open=0; }
    n64c_host_fault_uninstall();
    while (map_count) {
        --map_count;
        n64c_host_unmap(maps[map_count].base,maps[map_count].bytes);
    }
    ram_size=0;
    transaction.stage=0;
}
int n64c_memory_open(const N64cMemoryConfig *config)
{
    if (!config || map_count || (config->rdram_bytes!=0x400000u &&
        config->rdram_bytes!=0x800000u) || !config->rom_bytes ||
        config->rom_bytes>0x0FC00000u || !config->video_clock_hz ||
        n64c_host_page_size()!=PAGE_BYTES) return 0;
    const size_t granule=n64c_host_granule();
    size_t cart_bytes=((size_t)config->rom_bytes+granule-1u)&~(granule-1u);
    size_t total=(size_t)config->rdram_bytes+cart_bytes+sizeof devices/sizeof devices[0]*granule;
    if (!n64c_host_fault_install()) return 0;
    if (!n64c_host_store_create(total)) { n64c_host_fault_uninstall();return 0; }
    store_open=1;
    int ok=map_pair(0,config->rdram_bytes,N64C_PROT_RW,0) &&
        map_pair(0x10000000u,cart_bytes,N64C_PROT_RW,(size_t)config->rdram_bytes);
    if (ok) {
        ok=n64c_pi_read(0,(void *)(uintptr_t)0x90000000u,config->rom_bytes)==0;
        ok=ok && n64c_host_protect((void *)(uintptr_t)0x90000000u,cart_bytes,N64C_PROT_READ)==0 &&
            n64c_host_protect((void *)(uintptr_t)0xB0000000u,cart_bytes,N64C_PROT_READ)==0;
    }
    for (unsigned i=0;ok && i<sizeof devices/sizeof devices[0];i++)
        ok=map_pair(devices[i],granule,N64C_PROT_NONE,(size_t)config->rdram_bytes+cart_bytes+i*granule);
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
u32 n64c_memory_ram_size(void) { return ram_size; }
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
u32 n64c_memory_ram_size(void) { return 0; }
u32 n64c_memory_physical(const void *pointer) { return (u32)(uintptr_t)pointer; }
u32 n64c_probe_tlb(const void *pointer) { (void)pointer; return UINT32_MAX; }
void osUnmapTLB(s32 index) { (void)index; }
void osUnmapTLBAll(void) {}
void osMapTLB(s32 index,u32 pm,void *vaddr,u32 evenpaddr,u32 oddpaddr,s32 asid)
{
    (void)index;(void)pm;(void)vaddr;(void)evenpaddr;(void)oddpaddr;(void)asid;
}
#endif
