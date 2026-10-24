/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Windows/i386 backend of the host seam (os/n64c_host.h), built with mingw-w64 (i686-w64-mingw32).
 *
 * Address windows: one pagefile-backed section (CreateFileMapping) holds RDRAM, the cartridge and the register
 * pages; every window is a MapViewOfFileEx at its fixed address, never over an existing allocation. Faults come
 * through a vectored exception handler. A fault on a register page is serviced like the Linux backend does it: the
 * handler copies the thread's CONTEXT, points the live one at the generic dispatch on a TLS stack, and the dispatch
 * ends in NtContinue of the saved copy. The x87/SSE state is saved with fxsave when the handler is entered and
 * restored before NtContinue, so a fault between two floating point instructions of the game loses nothing.
 *
 * Granule: Windows maps views at 64 KiB addresses and offsets, but the guest TLB maps 4 KiB pages. A TLB page whose
 * address, physical offset and size are all multiples of 64 KiB is a second view of the section (a true alias). Any
 * other page is kept coherent in software: the virtual page is a private page and the RDRAM frame behind it
 * (through both KSEG0 and KSEG1) is protected whenever the virtual page holds the current bytes, and the other way
 * round. A touch of the side that does not hold them faults, the handler copies the page across and swaps the
 * protections, and the instruction runs again. Exactly one side of a frame is accessible at a time. This needs no
 * instruction decoding, so it works for every access the game or the shim makes, REP MOVS included; only a system
 * call that writes into a frame (ReadFile into RDRAM) cannot be faulted in, which n64c_host_ram_prepare covers. */
#if defined(_WIN32) && defined(__i386__)
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include "n64c_host.h"
#include "../n64c_memory.h"
#include "../n64c_diag.h"
#include <fcntl.h>
#include <io.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

_Static_assert(sizeof(CONTEXT)<=sizeof(N64cHostContext),"N64cHostContext holds a CONTEXT");
extern IMAGE_DOS_HEADER __ImageBase;
enum { PAGE=4096, GRANULE=65536, FRAMES=2048 /* 8 MiB of RDRAM */, KSEG0=0x80000000u, KSEG1=0xA0000000u };

/* The guest's virtual pages (osMapTLB) live below the image: 0x00400000..0x01000000 holds the game's resource pager.
 * The process start claims that range in 64 KiB reservations before anything else allocates (n64c_host_process_start), and
 * a TLB mapping takes its chunks over. */
enum { CLAIM_LO=0x00400000, CLAIM_HI=0x00A00000, CLAIM_CHUNKS=(CLAIM_HI-CLAIM_LO)/GRANULE };
/* What the game uses: a 1 MB mapping at 0x00400000 and the pager's pages from 0x00800000. A start whose loader put its stack or
 * heap there is run again (n64c_host_process_start); the gap between is left to the system. */
static int wanted(uintptr_t chunk) { return chunk<0x00500000 || chunk>=0x00800000; }
static unsigned char claimed[CLAIM_CHUNKS];

static HANDLE section;
static PVOID veh_handle;
static CRITICAL_SECTION lock;
static int lock_ready;

size_t n64c_host_page_size(void)
{
    SYSTEM_INFO info;
    GetSystemInfo(&info);
    return info.dwPageSize;
}
size_t n64c_host_granule(void) { return GRANULE; }
uintptr_t n64c_host_image_start(void) { return (uintptr_t)&__ImageBase; }
int n64c_host_traced(void) { return IsDebuggerPresent(); }

int n64c_host_store_create(size_t bytes)
{
    if (!lock_ready) { InitializeCriticalSection(&lock);lock_ready=1; }
    section=CreateFileMappingW(INVALID_HANDLE_VALUE,NULL,PAGE_READWRITE|SEC_COMMIT,0,(DWORD)bytes,NULL);
    return section!=NULL;
}
static void release_blocks(void);
void n64c_host_store_close(void)
{
    release_blocks();
    if (section) { CloseHandle(section);section=NULL; }
}
static DWORD native_protection(int protection)
{
    return protection==N64C_PROT_RW ? PAGE_READWRITE : protection==N64C_PROT_READ ? PAGE_READONLY : PAGE_NOACCESS;
}
void *n64c_host_map(uintptr_t address,size_t bytes,size_t offset,int protection)
{
    void *view=MapViewOfFileEx(section,FILE_MAP_WRITE,0,(DWORD)offset,bytes,(void *)address);
    if (!view) return NULL;
    if (view!=(void *)address) { UnmapViewOfFile(view);return NULL; }
    DWORD old;
    if (protection!=N64C_PROT_RW && !VirtualProtect(view,bytes,native_protection(protection),&old)) {
        UnmapViewOfFile(view);
        return NULL;
    }
    return view;
}
int n64c_host_protect(void *base,size_t bytes,int protection)
{
    DWORD old;
    return VirtualProtect(base,bytes,native_protection(protection),&old) ? 0 : -1;
}
int n64c_host_unmap(void *base,size_t bytes) { (void)bytes;return UnmapViewOfFile(base) ? 0 : -1; }

/* ---- software-coherent TLB pages ---- */
typedef struct DsmPage {
    uintptr_t v;               /* virtual page address */
    uint32_t frame;            /* RDRAM frame (physical >> 12) */
    struct DsmPage *next;      /* next virtual page of the same frame */
    struct DsmPage *hash_next; /* bucket chain */
} DsmPage;
typedef struct { DsmPage *pages; DsmPage *owner; } Frame; /* owner NULL: the RDRAM frame holds the bytes */
#define BUCKETS 4096
static Frame frames[FRAMES];
static DsmPage *buckets[BUCKETS];
static uintptr_t blocks[1024];  /* 64 KiB blocks reserved for virtual pages */
static unsigned block_count;
typedef struct { uintptr_t base; size_t bytes; int placeholder; } View;
static View views[64];          /* 64 KiB-aligned TLB pages mapped as true aliases */

/* The 1 MB mapping at 0x00400000 is the one the game makes at boot and a thread created by another part of the program (the
 * loader's workers, SDL, the driver) can take the range in the moment between giving a reservation back and mapping the
 * view. A placeholder (Windows 10 1803 and later) is replaced by the view in one call, so there is no such moment. */
enum { ZONE1=0x00400000, ZONE1_BYTES=0x00100000, MEM_RESERVE_PLACEHOLDER_=0x00040000, MEM_REPLACE_PLACEHOLDER_=0x00004000,
       MEM_PRESERVE_PLACEHOLDER_=0x2 };
typedef PVOID (WINAPI *VirtualAlloc2Fn)(HANDLE,PVOID,SIZE_T,ULONG,ULONG,void *,ULONG);
typedef PVOID (WINAPI *MapViewOfFile3Fn)(HANDLE,HANDLE,PVOID,ULONG64,SIZE_T,ULONG,ULONG,void *,ULONG);
typedef BOOL (WINAPI *UnmapViewOfFile2Fn)(HANDLE,PVOID,ULONG);
static VirtualAlloc2Fn virtual_alloc2;
static MapViewOfFile3Fn map_view_of_file3;
static UnmapViewOfFile2Fn unmap_view_of_file2;
static int zone1_placeholder; /* the range is one placeholder, not yet mapped */
static int placeholders_available(void)
{
    if (!virtual_alloc2) {
        HMODULE kernel=GetModuleHandleA("kernelbase.dll");
        if (kernel) {
            virtual_alloc2=(VirtualAlloc2Fn)(void *)GetProcAddress(kernel,"VirtualAlloc2");
            map_view_of_file3=(MapViewOfFile3Fn)(void *)GetProcAddress(kernel,"MapViewOfFile3");
            unmap_view_of_file2=(UnmapViewOfFile2Fn)(void *)GetProcAddress(kernel,"UnmapViewOfFile2");
        }
    }
    return virtual_alloc2 && map_view_of_file3 && unmap_view_of_file2;
}
/* Give the claimed chunks of [at, at+bytes) back (hold=0) or take them again (hold=1). */
static void claim_chunks(uintptr_t at,size_t bytes,int hold)
{
    for (uintptr_t chunk=at&~(uintptr_t)(GRANULE-1);chunk<at+bytes;chunk+=GRANULE) {
        if (chunk<CLAIM_LO || chunk>=CLAIM_HI) continue;
        unsigned char *flag=&claimed[(chunk-CLAIM_LO)/GRANULE];
        if (!hold && *flag) { VirtualFree((void *)chunk,0,MEM_RELEASE);*flag=0; }
        else if (hold && !*flag && VirtualAlloc((void *)chunk,GRANULE,MEM_RESERVE,PAGE_NOACCESS)==(void *)chunk) *flag=1;
    }
}
static DsmPage *find_page(uintptr_t v)
{
    for (DsmPage *p=buckets[(v>>12)%BUCKETS];p;p=p->hash_next)
        if (p->v==v) return p;
    return NULL;
}
static int ensure_block(uintptr_t v)
{
    const uintptr_t block=v&~(uintptr_t)(GRANULE-1);
    if (block>=CLAIM_LO && block<CLAIM_HI && claimed[(block-CLAIM_LO)/GRANULE]) return 1;
    for (unsigned i=0;i<block_count;i++)
        if (blocks[i]==block) return 1;
    if (block_count>=sizeof blocks/sizeof blocks[0]) return 0;
    void *got=VirtualAlloc((void *)block,GRANULE,MEM_RESERVE,PAGE_NOACCESS);
    if (got!=(void *)block) {
        if (got) VirtualFree(got,0,MEM_RELEASE);
        return 0;
    }
    blocks[block_count++]=block;
    return 1;
}
static void release_blocks(void)
{
    while (block_count) VirtualFree((void *)blocks[--block_count],0,MEM_RELEASE);
}
static void protect_frame(uint32_t frame,DWORD protection)
{
    DWORD old;
    VirtualProtect((void *)(uintptr_t)(KSEG0+frame*PAGE),PAGE,protection,&old);
    VirtualProtect((void *)(uintptr_t)(KSEG1+frame*PAGE),PAGE,protection,&old);
}
static void protect_page(DsmPage *page,DWORD protection)
{
    DWORD old;
    VirtualProtect((void *)page->v,PAGE,protection,&old);
}
/* The frame's current bytes move to `to` (NULL: the RDRAM frame) and that side becomes the only accessible one. */
static void transfer(uint32_t frame,DsmPage *to)
{
    Frame *f=&frames[frame];
    void *ram=(void *)(uintptr_t)(KSEG0+frame*PAGE);
    if (f->owner==to) return;
    if (to) {
        if (f->owner) {
            protect_page(to,PAGE_READWRITE);
            memcpy((void *)to->v,(void *)f->owner->v,PAGE);
            protect_page(f->owner,PAGE_NOACCESS);
        } else {
            protect_page(to,PAGE_READWRITE);
            memcpy((void *)to->v,ram,PAGE);
            protect_frame(frame,PAGE_NOACCESS);
        }
    } else {
        protect_frame(frame,PAGE_READWRITE);
        memcpy(ram,(void *)f->owner->v,PAGE);
        protect_page(f->owner,PAGE_NOACCESS);
    }
    f->owner=to;
}
static int dsm_map(uintptr_t v,uint32_t physical)
{
    const uint32_t frame=physical>>12;
    if (frame>=FRAMES || find_page(v) || !ensure_block(v)) return -1;
    if (!VirtualAlloc((void *)v,PAGE,MEM_COMMIT,PAGE_NOACCESS)) return -1;
    DsmPage *page=calloc(1,sizeof *page);
    if (!page) { VirtualFree((void *)v,PAGE,MEM_DECOMMIT);return -1; }
    page->v=v;
    page->frame=frame;
    page->next=frames[frame].pages;
    frames[frame].pages=page;
    page->hash_next=buckets[(v>>12)%BUCKETS];
    buckets[(v>>12)%BUCKETS]=page;
    return 0;
}
static void dsm_unmap(DsmPage *page)
{
    Frame *f=&frames[page->frame];
    if (f->owner==page) transfer(page->frame,NULL);
    for (DsmPage **p=&f->pages;*p;p=&(*p)->next)
        if (*p==page) { *p=page->next;break; }
    for (DsmPage **p=&buckets[(page->v>>12)%BUCKETS];*p;p=&(*p)->hash_next)
        if (*p==page) { *p=page->hash_next;break; }
    VirtualFree((void *)page->v,PAGE,MEM_DECOMMIT);
    free(page);
}
/* A fault inside a software-coherent frame: 1 when the swap was done and the instruction can run again. */
static int dsm_fault(uintptr_t address)
{
    int handled=0;
    if (!lock_ready) return 0;
    EnterCriticalSection(&lock);
    DsmPage *page=find_page(address&~(uintptr_t)(PAGE-1));
    if (page) {
        if (frames[page->frame].owner!=page) { transfer(page->frame,page);handled=1; }
    } else {
        uintptr_t physical=0;
        if (address>=KSEG0 && address<KSEG0+FRAMES*PAGE) physical=address-KSEG0;
        else if (address>=KSEG1 && address<KSEG1+FRAMES*PAGE) physical=address-KSEG1;
        else goto out;
        const uint32_t frame=(uint32_t)(physical>>12);
        if (frames[frame].pages && frames[frame].owner) { transfer(frame,NULL);handled=1; }
    }
out:
    LeaveCriticalSection(&lock);
    return handled;
}
/* Make [pointer, pointer+bytes) of guest memory (RDRAM through KSEG0/KSEG1, or a virtual page the guest mapped)
 * accessible to the caller: a system call cannot fault a protected frame in. A store runs the normal fault path. */
void n64c_host_ram_prepare(void *pointer,size_t bytes)
{
    const uintptr_t at=(uintptr_t)pointer;
    if (!bytes || !lock_ready) return;
    for (uintptr_t page=at&~(uintptr_t)(PAGE-1);page<at+bytes;page+=PAGE) {
        const int ram=(page>=KSEG0 && page<KSEG0+FRAMES*PAGE) || (page>=KSEG1 && page<KSEG1+FRAMES*PAGE);
        EnterCriticalSection(&lock);
        const int virt=find_page(page)!=NULL; /* a coherent virtual page; the 64 KiB aliases are always accessible */
        LeaveCriticalSection(&lock);
        if (!ram && !virt) continue;
        volatile unsigned char *byte=(volatile unsigned char *)page;
        *byte=*byte;
    }
}

int n64c_host_tlb_map(void *vaddr,size_t bytes,uint32_t physical)
{
    const uintptr_t v=(uintptr_t)vaddr;
    int rc=0;
    if (!lock_ready) return -1;
    EnterCriticalSection(&lock);
    if (!(v%GRANULE) && !(physical%GRANULE) && !(bytes%GRANULE)) {
        unsigned slot=0;
        while (slot<sizeof views/sizeof views[0] && views[slot].base) slot++;
        int placeholder=zone1_placeholder && v==ZONE1 && bytes==ZONE1_BYTES;
        void *view=NULL;
        if (slot<sizeof views/sizeof views[0]) {
            if (placeholder) view=map_view_of_file3(section,GetCurrentProcess(),(void *)v,physical,bytes,
                                                    MEM_REPLACE_PLACEHOLDER_,PAGE_READWRITE,NULL,0);
            else { claim_chunks(v,bytes,0);view=n64c_host_map(v,bytes,physical,N64C_PROT_RW); }
        }
        if (view && placeholder) zone1_placeholder=0;
        if (view) views[slot]=(View){v,bytes,placeholder};
        else { fprintf(stderr,"n64c: map %p +%x failed, error %lu\n",(void *)v,(unsigned)bytes,(unsigned long)GetLastError());
            for (uintptr_t at=v;at<v+bytes;) { /* what is in the way */
                MEMORY_BASIC_INFORMATION m;
                if (!VirtualQuery((void *)at,&m,sizeof m)) break;
                if (m.State!=MEM_FREE) fprintf(stderr,"n64c:   %p +%lx state %lx type %lx (allocated at %p)\n",m.BaseAddress,(unsigned long)m.RegionSize,
                    (unsigned long)m.State,(unsigned long)m.Type,m.AllocationBase);
                at=(uintptr_t)m.BaseAddress+m.RegionSize;
            }
            claim_chunks(v,bytes,1);rc=-1; }
    } else {
        size_t mapped=0;
        for (;mapped<bytes;mapped+=PAGE)
            if (dsm_map(v+mapped,physical+(uint32_t)mapped)) { rc=-1;break; }
        if (rc)
            for (size_t d=0;d<mapped;d+=PAGE) {
                DsmPage *page=find_page(v+d);
                if (page) dsm_unmap(page);
            }
    }
    LeaveCriticalSection(&lock);
    return rc;
}
int n64c_host_tlb_unmap(void *vaddr,size_t bytes)
{
    const uintptr_t v=(uintptr_t)vaddr;
    if (!lock_ready) return -1;
    EnterCriticalSection(&lock);
    for (unsigned i=0;i<sizeof views/sizeof views[0];i++)
        if (views[i].base==v) {
            if (views[i].placeholder) {
                if (unmap_view_of_file2(GetCurrentProcess(),(void *)v,MEM_PRESERVE_PLACEHOLDER_)) zone1_placeholder=1;
            } else {
                UnmapViewOfFile((void *)v);
                claim_chunks(v,views[i].bytes,1);
            }
            views[i]=(View){0};
            LeaveCriticalSection(&lock);
            return 0;
        }
    for (size_t done=0;done<bytes;done+=PAGE) {
        DsmPage *page=find_page(v+done);
        if (page) dsm_unmap(page);
    }
    LeaveCriticalSection(&lock);
    return 0;
}

/* The loader of the process creates its heap (and the system its mapped tables) in the lowest free address space, which is
 * where the game wants its virtual pages. So the program starts itself again suspended, reserves those ranges in the new
 * process before its loader has run, and resumes it; the new process finds the reservations and takes them over. */
static int adopt_claims(void)
{
    int all=1;
    MEMORY_BASIC_INFORMATION whole;
    if (VirtualQuery((void *)ZONE1,&whole,sizeof whole) && whole.State==MEM_RESERVE && whole.Type==MEM_PRIVATE &&
        (uintptr_t)whole.AllocationBase==ZONE1 && whole.RegionSize==ZONE1_BYTES && placeholders_available())
        zone1_placeholder=1;
    for (unsigned i=0;i<CLAIM_CHUNKS;i++) {
        const uintptr_t at=CLAIM_LO+i*GRANULE;
        if (!wanted(at)) continue;
        if (zone1_placeholder && at>=ZONE1 && at<ZONE1+ZONE1_BYTES) continue;
        MEMORY_BASIC_INFORMATION m;
        if (VirtualQuery((void *)at,&m,sizeof m) && m.State==MEM_RESERVE && m.Type==MEM_PRIVATE &&
            (uintptr_t)m.AllocationBase==at && m.RegionSize==GRANULE) claimed[i]=1;
        else all=0;
    }
    return all;
}
/* No second process (it could not be created): reserve what is free in this one. */
static int claim_guest_space(void)
{
    int all=1;
    for (unsigned i=0;i<CLAIM_CHUNKS;i++) {
        void *at=(void *)(uintptr_t)(CLAIM_LO+i*GRANULE);
        if (!wanted((uintptr_t)at)) continue;
        void *got=VirtualAlloc(at,GRANULE,MEM_RESERVE,PAGE_NOACCESS);
        if (got==at) claimed[i]=1;
        else { all=0;if (got) VirtualFree(got,0,MEM_RELEASE); }
    }
    return all;
}
int n64c_host_process_start(void)
{
    /* A crash ends with the fault record and the log, not with a system dialog nobody is there to close. */
    SetErrorMode(SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX|SEM_NOOPENFILEERRORBOX);
    if (getenv("N64C_LAUNCHED")) {
        const int adopted=adopt_claims();
        if (!adopted)
            fprintf(stderr,"n64c: the guest address ranges were not all reserved at start; osMapTLB may fail\n");
        return -1;
    }
    SetEnvironmentVariableA("N64C_LAUNCHED","1");
    STARTUPINFOW startup;
    PROCESS_INFORMATION child;
    memset(&startup,0,sizeof startup);
    startup.cb=sizeof startup;
    wchar_t *line=_wcsdup(GetCommandLineW());
    DWORD code=1;
    if (!line || !CreateProcessW(NULL,line,NULL,NULL,TRUE,CREATE_SUSPENDED,NULL,NULL,&startup,&child)) {
        fprintf(stderr,"n64c: cannot start the program suspended (error %lu); reserving in this process\n",
                (unsigned long)GetLastError());
        free(line);
        SetEnvironmentVariableA("N64C_LAUNCHED",NULL);
        if (!claim_guest_space())
            fprintf(stderr,"n64c: the guest address ranges are not free; osMapTLB may fail\n");
        return -1;
    }
    free(line);
    const int placeholder=placeholders_available() &&
        virtual_alloc2(child.hProcess,(void *)ZONE1,ZONE1_BYTES,MEM_RESERVE|MEM_RESERVE_PLACEHOLDER_,PAGE_NOACCESS,NULL,0);
    for (unsigned i=0;i<CLAIM_CHUNKS;i++) {
        const uintptr_t at=CLAIM_LO+i*GRANULE;
        if (placeholder && at>=ZONE1 && at<ZONE1+ZONE1_BYTES) continue;
        if (wanted(at)) (void)VirtualAllocEx(child.hProcess,(void *)at,GRANULE,MEM_RESERVE,PAGE_NOACCESS);
    }
    ResumeThread(child.hThread);
    WaitForSingleObject(child.hProcess,INFINITE);
    GetExitCodeProcess(child.hProcess,&code);
    CloseHandle(child.hThread);
    CloseHandle(child.hProcess);
    return (int)(code & 0x7fffffffu); /* a crash status (0xC...) is not "carry on": it ends this process too */
}

/* ---- machine context ---- */
uintptr_t n64c_host_pc(const void *ctx) { return ((const CONTEXT *)ctx)->Eip; }
void n64c_host_set_pc(void *ctx,uintptr_t pc) { ((CONTEXT *)ctx)->Eip=(DWORD)pc; }
static DWORD *reg_slot(CONTEXT *c,unsigned reg)
{
    switch (reg) {
    case 0: return &c->Eax;
    case 1: return &c->Ecx;
    case 2: return &c->Edx;
    case 3: return &c->Ebx;
    case 4: return &c->Esp;
    case 5: return &c->Ebp;
    case 6: return &c->Esi;
    default: return &c->Edi;
    }
}
uint32_t n64c_host_reg(const void *ctx,unsigned reg) { return *reg_slot((CONTEXT *)ctx,reg); }
void n64c_host_set_reg(void *ctx,unsigned reg,uint32_t value) { *reg_slot(ctx,reg)=value; }
void n64c_host_ctx_copy(void *dst,const void *src) { memcpy(dst,src,sizeof(CONTEXT)); }
void n64c_host_redirect(void *ctx,uintptr_t stack_top,void (*fn)(void))
{
    CONTEXT *context=ctx;
    context->Esp=(DWORD)stack_top;
    context->Eip=(DWORD)(uintptr_t)fn;
    context->EFlags&=~(DWORD)0x400;
}
void n64c_host_describe(const char *what,const N64cHostFault *fault,int caller) { (void)what;(void)fault;(void)caller; }
int n64c_host_diag_tlb_miss(uintptr_t address,void *saved_context)
{
    (void)address;(void)saved_context;
    return 0; /* run-on is a Linux diagnostic build */
}

/* ---- faults ---- */
/* The x87/SSE state at the fault, kept per thread until the context is continued. */
static _Thread_local unsigned char fx_raw[512+16];
static unsigned char *fx_area(void) { return (unsigned char *)(((uintptr_t)fx_raw+15)&~(uintptr_t)15); }

typedef LONG (NTAPI *NtContinueFn)(PCONTEXT,BOOLEAN);
static NtContinueFn nt_continue;

_Noreturn void n64c_host_resume(void)
{
    CONTEXT context;
    if (!n64c_memory_on_resume(&context) || !nt_continue) {
        static const char message[]="n64compat: resume without a serviced fault\n";
        (void)!_write(2,message,sizeof message-1);
        _exit(78);
    }
    __asm__ volatile("fxrstor %0"::"m"(*fx_area()):"memory");
    nt_continue(&context,FALSE);
    _exit(78);
}

/* Words at a host address, 1 when all were readable. */
static int read_words(uintptr_t at,DWORD *words,unsigned count)
{
    SIZE_T got=0;
    return ReadProcessMemory(GetCurrentProcess(),(const void *)at,words,count*sizeof *words,&got) &&
        got==count*sizeof *words;
}
/* The Windows fault record: info is an EXCEPTION_RECORD, context a CONTEXT (same fields as the Linux record). */
int n64c_memory_fault_json(char *out,size_t cap,int signo,const void *given,const void *opaque,int tid)
{
    const EXCEPTION_RECORD *info=given;
    const CONTEXT *c=opaque;
    size_t used=0;
    DWORD words[8];
#define PUT(...) do { int n_=snprintf(out+used,cap-used,__VA_ARGS__); if (n_<0 || (size_t)n_>=cap-used) return 0; used+=(size_t)n_; } while (0)
    const unsigned long long kind=info->NumberParameters>0 ? info->ExceptionInformation[0] : 0;
    PUT("{\"signal\":%d,\"code\":%lu,\"si_addr\":\"0x%08x\",\"access\":\"%s\",\"tid\":%d,\"eip\":\"0x%08x\","
        "\"eflags\":\"0x%08x\",\"regs\":{",signo,(unsigned long)info->ExceptionCode,
        info->NumberParameters>1 ? (unsigned)info->ExceptionInformation[1] : 0u,
        kind==8 ? "exec" : kind==1 ? "write" : "read",tid,(unsigned)c->Eip,(unsigned)c->EFlags);
    PUT("\"eax\":\"0x%08x\",\"ecx\":\"0x%08x\",\"edx\":\"0x%08x\",\"ebx\":\"0x%08x\",\"esp\":\"0x%08x\","
        "\"ebp\":\"0x%08x\",\"esi\":\"0x%08x\",\"edi\":\"0x%08x\"",(unsigned)c->Eax,(unsigned)c->Ecx,(unsigned)c->Edx,
        (unsigned)c->Ebx,(unsigned)c->Esp,(unsigned)c->Ebp,(unsigned)c->Esi,(unsigned)c->Edi);
    PUT("},\"stack\":[");
    const unsigned stack_words=read_words(c->Esp,words,8) ? 8 : 0;
    for (unsigned i=0;i<stack_words;i++) PUT("%s\"0x%08x\"",i ? "," : "",(unsigned)words[i]);
    PUT("],\"args\":[");
    if (read_words(c->Ebp+8,words,4))
        for (unsigned i=0;i<4;i++) PUT("%s\"0x%08x\"",i ? "," : "",(unsigned)words[i]);
    PUT("],\"frames\":[");
    uintptr_t frame=c->Ebp;
    for (unsigned depth=0;depth<8 && read_words(frame,words,2);depth++) {
        PUT("%s\"0x%08x\"",depth ? "," : "",(unsigned)words[1]);
        if (words[0]<=frame) break;
        frame=words[0];
    }
    PUT("]}\n");
#undef PUT
    return (int)used;
}
static void fault_record(const EXCEPTION_POINTERS *ep)
{
    const char *path=getenv("N64C_FAULT_FRAME");
    char text[2048];
    const int length=path ? n64c_memory_fault_json(text,sizeof text,11,ep->ExceptionRecord,ep->ContextRecord,(int)GetCurrentThreadId()) : 0;
    HANDLE file=length>0 ? CreateFileA(path,GENERIC_WRITE,0,NULL,CREATE_ALWAYS,FILE_ATTRIBUTE_NORMAL,NULL) : INVALID_HANDLE_VALUE;
    if (file==INVALID_HANDLE_VALUE) return;
    DWORD written;
    WriteFile(file,text,(DWORD)length,&written,NULL);
    CloseHandle(file);
}
/* The handler proper. The FPU state was saved by the entry thunk below before any floating point code could run. */
static LONG veh_main(EXCEPTION_POINTERS *ep)
{
    const EXCEPTION_RECORD *record=ep->ExceptionRecord;
    if (record->ExceptionCode!=EXCEPTION_ACCESS_VIOLATION || record->NumberParameters<2) {
        /* Other errors (a floating point trap, an illegal instruction) end the process: leave the fault record first. */
        if ((record->ExceptionCode>>30)==3u && record->ExceptionCode!=0xC000013Au) fault_record(ep);
        return EXCEPTION_CONTINUE_SEARCH;
    }
    const ULONG_PTR kind=record->ExceptionInformation[0];
    const uintptr_t address=(uintptr_t)record->ExceptionInformation[1];
    if (dsm_fault(address)) return EXCEPTION_CONTINUE_EXECUTION;
    const N64cHostFault fault={address,ep->ContextRecord->Eip,kind==1,ep->ContextRecord,(void *)record};
    if (n64c_memory_on_fault(&fault)) return EXCEPTION_CONTINUE_EXECUTION;
    fault_record(ep);
    return EXCEPTION_CONTINUE_SEARCH;
}
/* Entry: fxsave first (a normal function may use xmm registers before its first statement), then the work. A fault
 * fully handled here (a swap of a coherent page) resumes at the same instruction, so the saved state goes back now;
 * a redirected fault restores it when its dispatch continues the saved context. */
static LONG CALLBACK veh(EXCEPTION_POINTERS *ep) __attribute__((target("general-regs-only")));
static LONG CALLBACK veh(EXCEPTION_POINTERS *ep)
{
    unsigned char *area=fx_area();
    __asm__ volatile("fxsave %0":"=m"(*area)::"memory");
    const DWORD at=ep->ContextRecord->Eip;
    LONG result=veh_main(ep);
    if (result==EXCEPTION_CONTINUE_EXECUTION && ep->ContextRecord->Eip==at)
        __asm__ volatile("fxrstor %0"::"m"(*area):"memory");
    return result;
}
int n64c_host_fault_install(void)
{
    if (!nt_continue) nt_continue=(NtContinueFn)(void *)GetProcAddress(GetModuleHandleA("ntdll.dll"),"NtContinue");
    if (!nt_continue || veh_handle) return nt_continue && veh_handle ? 1 : 0;
    veh_handle=AddVectoredExceptionHandler(1,veh);
    return veh_handle!=NULL;
}
void n64c_host_fault_uninstall(void)
{
    if (veh_handle) { RemoveVectoredExceptionHandler(veh_handle);veh_handle=NULL; }
}

#endif /* _WIN32 && __i386__ */
