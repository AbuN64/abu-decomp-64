/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Run-on boot diagnostics: the walk's diagnostic boot (N64C_RUN_ON=1 in the environment, the instrumented walk build).
 * The fail-fast boot ends at its first failure, so every boot names one problem. Here each failure is recorded as one
 * line, in boot order, and the boot goes on past it wherever that has a defined meaning:
 *     boot-diag: <class> <subject> at=<n>/<total> after=<last first hit> in=<function><caller<... [details]
 * classes: missing (a call into an unlinked function; it returns 0), stop (a shim stop; the site drops the task,
 * reads 0 or leaves the bytes raw), code-pointer (a call through a raw guest code address; it returns 0), fault (a
 * CPU fault; the innermost instrumented function returns 0), layout (guest DMA or RNC bytes no byte-order row
 * covers; they stay big-endian) and hang (no new hit, failure or rendered frame for N64C_RUN_ON_HANG seconds; the spinning function
 * returns 0). A failure with no way on ends the run as before, after the list. The run ends with
 *     boot-diag: end <failures> failures (<per class>), census <n>/<total>
 * Nothing here ships: the acceptance gate (fl0) and the stream tile run fail-fast. Values after a failure are
 * whatever the original code does with a 0, so a later failure may be a consequence of an earlier one; the list is
 * in boot order so the first entries are the independent ones. */
#define _GNU_SOURCE
#include "trace.h"
#include "n64c_diag.h"
#include "n64c_memory.h"
#include "n64c_pi.h"
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <ucontext.h>
#include <unistd.h>

#define NOINST __attribute__((no_instrument_function))
enum { CLASS_MISSING, CLASS_STOP, CLASS_CODE_POINTER, CLASS_FAULT, CLASS_LAYOUT, CLASS_HANG, CLASSES };
static const char *const class_names[CLASSES] = {"missing", "stop", "code-pointer", "fault", "layout", "hang"};
enum { MAX_SEEN = 4096, FAULT_REPEATS = 5000, MAX_HANGS = 16, HANG_TRIES = 400 };
typedef struct {
    int cls;
    char subject[80];
    unsigned long count;
} Seen;
static Seen seen[MAX_SEEN];
static unsigned n_seen, per_class[CLASSES];
static volatile unsigned distinct;
static int active, ended;
static unsigned hang_seconds = 15, limit_seconds;
static pthread_mutex_t diag_lock = PTHREAD_MUTEX_INITIALIZER;
static N64cTraceStack *volatile hang_target;
static unsigned hangs;
static volatile int hang_broken;
/* The frame driver is the single producer; only the watchdog reads this count.
 * Use an atomic counter rather than sharing renderer or scheduler internals. */
static unsigned rendered_frames;
NOINST void n64c_diag_rendered_frame(void)
{
    __atomic_add_fetch(&rendered_frames,1,__ATOMIC_RELAXED);
}

NOINST static const char *name_of(const void *address, char *buffer, size_t size)
{
    unsigned offset = 0;
    const char *name = n64c_trace_symbol(address, &offset);
    if (!name)
        snprintf(buffer, size, "%p", address);
    else if (offset)
        snprintf(buffer, size, "%s+0x%x", name, offset);
    else
        snprintf(buffer, size, "%s", name);
    return buffer;
}
/* "in=f<g<h": the innermost instrumented frames of a stack, skipping a generated stop function named skip. */
NOINST static void frames_text(const N64cTraceStack *stack, const char *skip, char *out, size_t size)
{
    size_t used = (size_t)snprintf(out, size, "in=");
    int shown = 0;
    if (stack) {
        unsigned depth = stack->depth < N64C_TRACE_DEPTH ? stack->depth : N64C_TRACE_DEPTH;
        for (unsigned k = depth; k-- > 0 && shown < 4 && used < size;) {
            const char *name = n64c_trace_symbol(stack->frame[k].fn, NULL);
            if (!name || (skip && !strcmp(name, skip)))
                continue;
            used += (size_t)snprintf(out + used, size - used, "%s%s", shown ? "<" : "", name);
            shown++;
        }
    }
    if (!shown && used < size)
        snprintf(out + used, size - used, "host");
}
NOINST static void print_end(const char *why)
{
    if (ended)
        return;
    ended = 1;
    unsigned total = 0;
    const char *last = NULL;
    const unsigned hits = n64c_trace_position(&total, &last);
    fprintf(stderr, "boot-diag: end %u failures (", n_seen);
    for (int c = 0; c < CLASSES; c++)
        fprintf(stderr, "%s%u %s", c ? ", " : "", per_class[c], class_names[c]);
    fprintf(stderr, "), census %u/%u, last %s%s%s\n", hits, total, last ? last : "none", why ? ": " : "",
            why ? why : "");
    for (unsigned i = 0; i < n_seen; i++)
        if (seen[i].count > 1)
            fprintf(stderr, "boot-diag: repeat %s %s x%lu\n", class_names[seen[i].cls], seen[i].subject, seen[i].count);
    if (n64c_trace_summary)
        n64c_trace_summary();
    fflush(stderr);
}
NOINST static _Noreturn void give_up(const char *why)
{
    /* A thread stuck inside a record (a fault in a signal handler) must not keep the list from printing. */
    struct timespec due;
    clock_gettime(CLOCK_REALTIME, &due);
    due.tv_sec += 2;
    const int locked = pthread_mutex_timedlock(&diag_lock, &due) == 0;
    print_end(why);
    if (locked)
        pthread_mutex_unlock(&diag_lock);
    _Exit(78);
}
/* One failure. where: its context ("in=..."), or NULL for the calling thread's frames. Returns its repeat count
 * (1: the first time, printed). */
NOINST static unsigned long record_at(int cls, const char *subject, const char *skip, const char *details,
                                      const char *where)
{
    pthread_mutex_lock(&diag_lock);
    Seen *entry = NULL;
    for (unsigned i = 0; i < n_seen && !entry; i++)
        if (seen[i].cls == cls && !strncmp(seen[i].subject, subject, sizeof seen[i].subject - 1))
            entry = &seen[i];
    if (!entry && n_seen < MAX_SEEN) {
        entry = &seen[n_seen++];
        entry->cls = cls;
        snprintf(entry->subject, sizeof entry->subject, "%s", subject);
        per_class[cls]++;
        distinct = n_seen;
        unsigned total = 0;
        const char *last = NULL;
        const unsigned hits = n64c_trace_position(&total, &last);
        char own_where[512];
        if (!where) {
            frames_text(n64c_trace_stack(), skip, own_where, sizeof own_where);
            where = own_where;
        }
        fprintf(stderr, "boot-diag: %s %s at=%u/%u after=%s %s%s%s\n", class_names[cls], subject, hits, total,
                last ? last : "none", where, details && *details ? " " : "", details ? details : "");
        fflush(stderr);
    }
    unsigned long count = entry ? ++entry->count : 0;
    pthread_mutex_unlock(&diag_lock);
    if (!entry)
        give_up("too many distinct failures");
    return count;
}
NOINST static unsigned long record(int cls, const char *subject, const char *skip, const char *details)
{
    return record_at(cls, subject, skip, details, NULL);
}
NOINST int n64c_diag_missing(const char *symbol)
{
    if (!active)
        return 0;
    record(CLASS_MISSING, symbol, symbol, NULL);
    return 1;
}
NOINST int n64c_diag_stop(const char *subject, int can_continue)
{
    if (!active)
        return 0;
    record(CLASS_STOP, subject, NULL, can_continue ? NULL : "end=no-way-on");
    if (!can_continue) {
        pthread_mutex_lock(&diag_lock);
        print_end(subject);
        pthread_mutex_unlock(&diag_lock);
    }
    return can_continue;
}
NOINST void n64c_diag_layout(const char *what, u32 rom, u32 bytes, const void *ram)
{
    if (!active)
        return;
    /* A resource streamed in chunks (one DMA per 8 KiB) is one failure: a chunk that continues the last uncovered
     * range counts as a repeat of it, and the end line gives the run's extent. */
    static char run_subject[64];
    static u32 run_end;
    char subject[64], details[96];
    if (!(rom == run_end && run_subject[0]))
        snprintf(run_subject, sizeof run_subject, "%s-rom-0x%08x", what, rom);
    snprintf(subject, sizeof subject, "%s", run_subject);
    run_end = rom + bytes;
    snprintf(details, sizeof details, "bytes=0x%x ram=0x%08x", bytes, n64c_memory_physical(ram));
    record(CLASS_LAYOUT, subject, NULL, details);
}

#if defined(__i386__) && defined(__linux__)
/* The callee-saved registers a function's prologue pushed, as frame-pointer offsets (0: not saved there):
 * [endbr32] push %ebp; mov %esp,%ebp; then any of push %ebx/%esi/%edi. Returns the prologue's length, 0 when the
 * prologue is another shape. */
NOINST static unsigned saved_slots(const unsigned char *start, int slot[3])
{
    const unsigned char *code = start;
    slot[0] = slot[1] = slot[2] = 0;
    if (code[0] == 0xF3 && code[1] == 0x0F && code[2] == 0x1E && code[3] == 0xFB)
        code += 4;
    if (code[0] != 0x55 || !((code[1] == 0x89 && code[2] == 0xE5) || (code[1] == 0x8B && code[2] == 0xEC)))
        return 0;
    code += 3;
    for (int offset = -4;; offset -= 4, code++) {
        if (*code == 0x53 && !slot[0])
            slot[0] = offset;
        else if (*code == 0x56 && !slot[1])
            slot[1] = offset;
        else if (*code == 0x57 && !slot[2])
            slot[2] = offset;
        else
            return (unsigned)(code - start);
    }
}
extern void __cyg_profile_func_enter(void *fn, void *call_site);
/* An original function: built with the census, so it calls the entry hook within its first instructions. */
NOINST static int instrumented(const unsigned char *fn)
{
    for (unsigned i = 0; i < 64; i++)
        if (fn[i] == 0xE8) {
            int32_t rel;
            memcpy(&rel, fn + i + 1, sizeof rel);
            if ((uintptr_t)(fn + i + 5) + (uintptr_t)(intptr_t)rel == (uintptr_t)__cyg_profile_func_enter)
                return 1;
        }
    return 0;
}
static const int saved_regs[3] = {REG_EBX, REG_ESI, REG_EDI};
/* Return 0 from the original function the PC is in. Its frame pointer is live in ebp; its caller's callee-saved
 * registers are in its save slots, or still in the registers where it saved none. Drops its frame records (the
 * entry hook's; its exit hook may already have run, the compiler moves it before trapping arithmetic). */
NOINST static void *unwind_here(ucontext_t *uc)
{
    greg_t *g = uc->uc_mcontext.gregs;
    unsigned offset = 0, length;
    const unsigned char *pc = (const unsigned char *)(uintptr_t)g[REG_EIP];
    int slot[3];
    if (!n64c_trace_symbol(pc, &offset))
        return NULL;
    const unsigned char *fn = pc - offset;
    const uintptr_t fp = (uintptr_t)g[REG_EBP], sp = (uintptr_t)g[REG_ESP];
    if (!instrumented(fn) || !(length = saved_slots(fn, slot)) || offset <= length || fp < sp ||
        fp - sp > (16u << 20) || !n64c_trace_symbol(((void **)fp)[1], NULL))
        return NULL;
    for (int r = 0; r < 3; r++)
        if (slot[r])
            g[saved_regs[r]] = *(greg_t *)(fp + (intptr_t)slot[r]);
    g[REG_EIP] = (greg_t)(uintptr_t)((void **)fp)[1];
    g[REG_EBP] = *(greg_t *)fp;
    g[REG_ESP] = (greg_t)(fp + 8);
    g[REG_EAX] = g[REG_EDX] = g[REG_ECX] = 0;
    g[REG_EFL] &= ~(greg_t)(0x100 | 0x400);
    N64cTraceStack *stack = n64c_trace_stack();
    while (stack && stack->depth && stack->depth <= N64C_TRACE_DEPTH &&
           (uintptr_t)stack->frame[stack->depth - 1].frame <= fp)
        stack->depth--;
    return (void *)fn;
}
/* Rewrite context so that an original function returns 0 to its caller: the one the PC is in, else (a fault in the
 * shim or libc) the innermost one whose frame record proves out. A record says where its caller's ebx/esi/edi are
 * (its save slots, or the values at entry), so the caller resumes with exactly its own registers; frames inside it
 * are abandoned. Returns the function, or NULL. */
NOINST static void *unwind(ucontext_t *uc)
{
    void *here = unwind_here(uc);
    if (here)
        return here;
    N64cTraceStack *stack = n64c_trace_stack();
    if (!stack || !stack->depth || stack->depth > N64C_TRACE_DEPTH)
        return NULL;
    greg_t *g = uc->uc_mcontext.gregs;
    const uintptr_t sp = (uintptr_t)g[REG_ESP];
    for (unsigned k = stack->depth; k-- > 0;) {
        const N64cTraceFrame *f = &stack->frame[k];
        const uintptr_t fp = (uintptr_t)f->frame;
        int slot[3];
        if (!fp || fp < sp || fp - sp > (16u << 20) || ((void **)fp)[1] != f->site ||
            !n64c_trace_symbol(f->site, NULL) || !saved_slots(f->fn, slot))
            continue;
        for (int r = 0; r < 3; r++)
            g[saved_regs[r]] = slot[r] ? *(greg_t *)(fp + (intptr_t)slot[r]) : (greg_t)f->regs[r];
        g[REG_EIP] = (greg_t)(uintptr_t)f->site;
        g[REG_EBP] = *(greg_t *)fp;
        g[REG_ESP] = (greg_t)(fp + 8);
        g[REG_EAX] = g[REG_EDX] = g[REG_ECX] = 0;
        g[REG_EFL] &= ~(greg_t)(0x100 | 0x400);
        stack->depth = k;
        return f->fn;
    }
    return NULL;
}
static const char *signal_name(int signal_number)
{
    switch (signal_number) {
    case SIGSEGV: return "SIGSEGV";
    case SIGBUS: return "SIGBUS";
    case SIGFPE: return "SIGFPE";
    case SIGILL: return "SIGILL";
    default: return "signal";
    }
}
/* Where a faulting address and the registers came from: the cartridge offset of DMA'd bytes they point into. */
NOINST static void provenance(const ucontext_t *uc, const void *address, char *out, size_t size)
{
    static const struct { int reg; const char *name; } regs[] = {
        {REG_EAX, "eax"}, {REG_EBX, "ebx"}, {REG_ECX, "ecx"}, {REG_EDX, "edx"}, {REG_ESI, "esi"}, {REG_EDI, "edi"}};
    u32 dev;
    size_t used = 0;
    if (address && n64c_pi_origin(address, &dev))
        used += (size_t)snprintf(out + used, size - used, " addr-rom=0x%08x", dev);
    for (unsigned i = 0; i < sizeof regs / sizeof regs[0] && used < size; i++) {
        const void *value = (const void *)(uintptr_t)uc->uc_mcontext.gregs[regs[i].reg];
        if (n64c_pi_origin(value, &dev))
            used += (size_t)snprintf(out + used, size - used, " %s-rom=0x%08x", regs[i].name, dev);
    }
}
NOINST int n64c_diag_fault(int signal_number, void *info_opaque, void *context)
{
    if (!active)
        return 0;
    const siginfo_t *info = info_opaque;
    ucontext_t *uc = context;
    const void *pc = (const void *)(uintptr_t)uc->uc_mcontext.gregs[REG_EIP];
    const void *address = info ? info->si_addr : NULL;
    char at[128], subject[160], details[256], back[160], where[512];
    snprintf(subject, sizeof subject, "%s@%s", signal_name(signal_number), name_of(pc, at, sizeof at));
    /* The access words match the shim's own fault line (n64c_memory.c "n64c-fault: TLB-miss-<addr> load|store at
     * host pc <pc>"), so one grep reads both. */
    const char *access = signal_number != SIGSEGV ? "op" : uc->uc_mcontext.gregs[REG_ERR] & 2 ? "store" : "load";
    size_t used = (size_t)snprintf(details, sizeof details, "%s-%08lx %s at host pc %#lx",
                                   info ? "addr" : "TLB-miss", (unsigned long)(uintptr_t)address, access,
                                   (unsigned long)(uintptr_t)pc);
    provenance(uc, address, details + used, sizeof details - used);
    frames_text(n64c_trace_stack(), NULL, where, sizeof where); /* before the unwind drops the frames */
    const void *fn = unwind(uc);
    used = strlen(details);
    snprintf(details + used, sizeof details - used, " return=%s",
             fn ? name_of(fn, back, sizeof back) : "none");
    if (record_at(CLASS_FAULT, subject, NULL, details, where) > FAULT_REPEATS)
        give_up("one fault repeats without end");
    if (!fn) {
        pthread_mutex_lock(&diag_lock);
        print_end("a fault with no instrumented frame to return from");
        pthread_mutex_unlock(&diag_lock);
    }
    return fn != NULL;
}
NOINST int n64c_diag_code_pointer(u32 address, void *context)
{
    if (!active)
        return 0;
    ucontext_t *uc = context;
    greg_t *g = uc->uc_mcontext.gregs;
    void *const *sp = (void *const *)(uintptr_t)g[REG_ESP];
    char subject[32], details[96], back[96];
    snprintf(subject, sizeof subject, "0x%08x", address);
    /* The fetch faulted at the target: the call's return address is on top of the stack. */
    void *ret = *sp;
    const int ok = n64c_trace_symbol(ret, NULL) != NULL;
    snprintf(details, sizeof details, "return=%s", ok ? name_of(ret, back, sizeof back) : "none");
    if (record(CLASS_CODE_POINTER, subject, NULL, details) > FAULT_REPEATS)
        give_up("one code pointer repeats without end");
    if (!ok) {
        pthread_mutex_lock(&diag_lock);
        print_end("a code pointer call with no return address");
        pthread_mutex_unlock(&diag_lock);
        return 0;
    }
    g[REG_EIP] = (greg_t)(uintptr_t)ret;
    g[REG_ESP] += 4;
    g[REG_EAX] = g[REG_EDX] = 0;
    return 1;
}
static struct sigaction prior_fault[3];
static const int fault_signals[3] = {SIGBUS, SIGFPE, SIGILL};
NOINST static void fault_handler(int signal_number, siginfo_t *info, void *context)
{
    if (n64c_diag_fault(signal_number, info, context))
        return;
    for (int i = 0; i < 3; i++)
        if (fault_signals[i] == signal_number)
            sigaction(signal_number, &prior_fault[i], NULL); /* returning re-runs the fault under the old action */
}
NOINST static void abort_handler(int signal_number)
{
    (void)signal_number;
    record(CLASS_STOP, "SIGABRT", NULL, "end=no-way-on");
    give_up("SIGABRT");
}
/* The hang breaker runs on the spinning thread; it acts only while that thread is in the body of an original
 * function (not inside the shim, which may hold the scheduler lock), else the watchdog tries again. */
NOINST static void hang_handler(int signal_number, siginfo_t *info, void *context)
{
    (void)signal_number;
    (void)info;
    if (n64c_trace_stack() == hang_target && !hang_broken && unwind_here(context))
        hang_broken = 1;
}
static int install(void)
{
    struct sigaction action;
    memset(&action, 0, sizeof action);
    action.sa_sigaction = fault_handler;
    action.sa_flags = SA_SIGINFO;
    sigemptyset(&action.sa_mask);
    for (int i = 0; i < 3; i++)
        if (sigaction(fault_signals[i], &action, &prior_fault[i]) != 0)
            return 0;
    action.sa_sigaction = hang_handler;
    action.sa_flags = SA_SIGINFO | SA_RESTART;
    if (sigaction(SIGURG, &action, NULL) != 0)
        return 0;
    signal(SIGABRT, abort_handler);
    return 1;
}
NOINST static void break_hang(void)
{
    N64cTraceStack *stack = n64c_trace_last_stack();
    char where[512], subject[96];
    frames_text(stack, NULL, where, sizeof where);
    snprintf(subject, sizeof subject, "%.80s", where + 3);
    char *cut = strchr(subject, '<');
    if (cut)
        *cut = 0;
    char thread[32];
    snprintf(thread, sizeof thread, "depth=%u", stack ? stack->depth : 0);
    record_at(CLASS_HANG, subject, NULL, thread, where);
    if (!stack || ++hangs > MAX_HANGS)
        give_up(stack ? "hangs keep coming" : "a hang with no instrumented thread");
    hang_broken = 0;
    hang_target = stack;
    for (int i = 0; i < HANG_TRIES && !hang_broken; i++) {
        pthread_kill((pthread_t)stack->thread, SIGURG);
        struct timespec pause = {0, 5000000};
        nanosleep(&pause, NULL);
    }
    hang_target = NULL;
    if (!hang_broken)
        give_up("a hang outside original code (the spinning thread never ran an original function body)");
}
#else
NOINST int n64c_diag_fault(int signal_number, void *info, void *context)
{
    (void)signal_number; (void)info; (void)context;
    return 0;
}
NOINST int n64c_diag_code_pointer(u32 address, void *context)
{
    (void)address; (void)context;
    return 0;
}
static int install(void) { return 1; }
static void break_hang(void) { give_up("a hang (no breaker on this host)"); }
#endif
/* No new hit, failure or completed rendered frame for hang_seconds is a hang.
 * The independent time limit still bounds a healthy repeating frame loop. */
NOINST static void *watchdog(void *unused)
{
    (void)unused;
    struct timespec start, now;
    clock_gettime(CLOCK_MONOTONIC, &start);
    unsigned last_hits = 0, last_distinct = 0, last_frames = 0;
    time_t still = start.tv_sec;
    for (;;) {
        struct timespec second = {1, 0};
        nanosleep(&second, NULL);
        clock_gettime(CLOCK_MONOTONIC, &now);
        if (limit_seconds && now.tv_sec - start.tv_sec >= (time_t)limit_seconds)
            give_up("time limit");
        const unsigned hits = n64c_trace_position(NULL, NULL), d = distinct;
        const unsigned frames = __atomic_load_n(&rendered_frames,__ATOMIC_RELAXED);
        if (hits != last_hits || d != last_distinct || frames != last_frames) {
            last_hits = hits;
            last_distinct = d;
            last_frames = frames;
            still = now.tv_sec;
        } else if (now.tv_sec - still >= (time_t)hang_seconds) {
            break_hang();
            clock_gettime(CLOCK_MONOTONIC, &now);
            still = now.tv_sec;
        }
    }
    return NULL;
}
NOINST static unsigned seconds(const char *name, unsigned fallback)
{
    const char *text = getenv(name);
    char *end;
    unsigned long value = text && *text ? strtoul(text, &end, 10) : fallback;
    return text && *text && *end ? fallback : (unsigned)value;
}
NOINST int n64c_runon_init(void)
{
    const char *on = getenv("N64C_RUN_ON");
    if (!on || strcmp(on, "1") != 0)
        return 0;
    hang_seconds = seconds("N64C_RUN_ON_HANG", 15);
    limit_seconds = seconds("N64C_RUN_ON_LIMIT", 0);
    if (!install()) {
        fprintf(stderr, "boot-diag: run-on unavailable (signal handlers)\n");
        return 0;
    }
    pthread_t thread;
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
    if (hang_seconds && pthread_create(&thread, &attr, watchdog, NULL) != 0)
        hang_seconds = 0;
    pthread_attr_destroy(&attr);
    active = 1;
    fprintf(stderr, "boot-diag: run-on: failures are recorded in boot order and the boot goes on (hang %u s, limit %u s)\n",
            hang_seconds, limit_seconds);
    fflush(stderr);
    return 1;
}
NOINST int n64c_runon_end(int rc)
{
    if (!active)
        return rc;
    pthread_mutex_lock(&diag_lock);
    const unsigned failures = n_seen;
    print_end(NULL);
    pthread_mutex_unlock(&diag_lock);
    return failures ? 78 : rc;
}
