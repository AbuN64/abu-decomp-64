/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The host OS seam of the shim: everything the native address windows, the guest stacks and the process-level boot
 * code need from the operating system, behind one small header. os/os_posix.c is the Linux/i386 backend (the code
 * that was in n64c_memory.c and n64c_sched.c, moved verbatim); os/os_win32.c is the Windows/i386 backend (mingw-w64).
 * The generic modules (n64c_memory.c, n64c_sched.c, boot/) hold no OS call: a platform fact is a function here.
 *
 * Address windows: the generic side asks for fixed-address views of one backing store (map, protect, unmap) and
 * receives faults as an N64cHostFault; the backend decodes nothing but the OS's own fault record. Machine contexts are
 * opaque to the generic side and read or written only through the accessors below (x86-32 register numbers in
 * encoding order: eax ecx edx ebx esp ebp esi edi; ARM: r0..r15). */
#ifndef N64C_HOST_H
#define N64C_HOST_H
#include <stddef.h>
#include <stdint.h>

/* The native address windows exist on Linux/i386 and Windows/i386; every other host refuses (n64c_memory.h). */
#if (defined(__linux__) && (defined(__i386__) || defined(__arm__))) || (defined(_WIN32) && defined(__i386__))
#define N64C_HOST_NATIVE 1
#endif

/* Storage for a saved machine context (a ucontext_t or a CONTEXT); the backend asserts it is large enough. */
typedef struct N64cHostContext { unsigned char opaque[1024]; } N64cHostContext; /* no alignment promise: thread-local on Windows */

enum { N64C_PROT_NONE = 0, N64C_PROT_READ = 1, N64C_PROT_RW = 3 };

/* A CPU fault the OS reported on this thread. context and info are the OS's own records, valid for the call only
 * (info is NULL where the OS has none). */
typedef struct {
    uintptr_t address;  /* the faulting data address, or the target of a bad jump */
    uintptr_t pc;       /* the faulting instruction */
    int write;          /* the access was a store */
    void *context;
    void *info;
} N64cHostFault;

/* ---- generic side, called by the backends (n64c_memory.c) ---- */
/* 1: the fault was one of the module's (device register, TLB miss, code pointer): the context was rewritten and the
 * thread resumes there. 0: not the module's; the backend chains to the OS default. */
int n64c_memory_on_fault(const N64cHostFault *fault);
/* The resume of a serviced fault: copies the saved context into the live one. 1 when one was pending. */
int n64c_memory_on_resume(void *context);

#ifdef N64C_HOST_NATIVE
/* ---- address windows (n64c_memory.c) ---- */
size_t n64c_host_page_size(void);   /* the CPU page size; the module needs 4096 */
size_t n64c_host_granule(void);     /* map alignment of address, offset and length: 4096 on Linux, 65536 on Windows */
int n64c_host_store_create(size_t bytes);  /* the backing store every window maps (memfd; a pagefile section) */
void n64c_host_store_close(void);
/* A view of the store at exactly address (never replacing an existing mapping): the address or NULL when it is taken
 * or cannot be mapped. address, offset and bytes are multiples of the granule. */
void *n64c_host_map(uintptr_t address, size_t bytes, size_t offset, int protection);
int n64c_host_protect(void *base, size_t bytes, int protection);   /* 0 on success */
int n64c_host_unmap(void *base, size_t bytes);                     /* 0 on success */
/* A guest TLB page: bytes (a multiple of the 4096-byte page) of RDRAM at the physical offset appear at vaddr, which
 * is not a multiple of the granule on Windows. Both return 0 on success. The pages alias RDRAM: a store through
 * either is seen through the other. */
int n64c_host_tlb_map(void *vaddr, size_t bytes, uint32_t physical);
int n64c_host_tlb_unmap(void *vaddr, size_t bytes);
/* Make [pointer, pointer+bytes) of RDRAM accessible to a system call that is about to write it (ReadFile cannot fault
 * a protected frame in); a no-op where RDRAM is never protected. */
void n64c_host_ram_prepare(void *pointer, size_t bytes);
uintptr_t n64c_host_image_start(void);   /* lowest address of the running program: guest virtual pages lie below it */
int n64c_host_traced(void);              /* a debugger is attached */
/* Faults: install handlers for the process (all or nothing, 1 when installed), remove them again (safe when none).
 * Install refuses when another handler already owns the fault. */
int n64c_host_fault_install(void);
void n64c_host_fault_uninstall(void);
/* Name a host code address on stderr (Android: library, function, offset): how a stop says which original function took it.
 * caller=1 names the call that jumped (the return address) instead of the faulting pc. A no-op where there is no dladdr. */
void n64c_host_describe(const char *what, const N64cHostFault *fault, int caller);
/* Machine context accessors; ctx is the OS context of a fault or an N64cHostContext holding a copy. */
uintptr_t n64c_host_pc(const void *ctx);
void n64c_host_set_pc(void *ctx, uintptr_t pc);
uint32_t n64c_host_reg(const void *ctx, unsigned reg);
void n64c_host_set_reg(void *ctx, unsigned reg, uint32_t value);
void n64c_host_ctx_copy(void *dst, const void *src);
/* Point ctx at fn on the given stack top (the return slot already holds 0): the next instruction run is fn's first. */
void n64c_host_redirect(void *ctx, uintptr_t stack_top, void (*fn)(void));
/* Continue the saved context of a serviced fault (never returns); the backend calls n64c_memory_on_resume. */
_Noreturn void n64c_host_resume(void);
/* A TLB miss in run-on: the diagnostic fault hook gets the saved context. 1 when it resumed the boot. */
int n64c_host_diag_tlb_miss(uintptr_t address, void *saved_context);
#endif

/* ---- clock (boot/frame.c) ---- */
struct timespec;
/* Sleep until the CLOCK_MONOTONIC time due (returns at once when it has passed). */
void n64c_host_sleep_until(const struct timespec *due);

/* ---- process start (boot/game_main.c, first statement of main) ---- */
/* -1: carry on in this process. Otherwise the program already ran in another process (Windows starts itself again when
 * the loader put its stack or heap in the range the guest maps pages into) and main returns this status at once. */
int n64c_host_process_start(void);

/* Run body(argc, argv) and return its status: Windows runs it on a thread with a large stack, because the program's own main
 * thread has the small stack that keeps it out of the guest's address range (see os/os_win32.c); Linux calls it. */
int n64c_host_run(int (*body)(int, char **), int argc, char **argv);

/* ---- guest threads (n64c_sched.c) ---- */
/* The fixed-address stack of a guest thread slot (STACK bytes, guard below), or NULL where the OS places thread stacks
 * itself (Windows): the host pointers the game stores in guest RAM then vary per run. */
void *n64c_host_guest_stack(int slot, size_t bytes, size_t slot_bytes, size_t guard_bytes);

/* ---- boot (boot/game_main.c) ---- */
/* stderr also goes to path: the OS copies the stream to both so nothing is lost when the process dies. */
void n64c_host_tee_stderr(const char *path);
#endif
