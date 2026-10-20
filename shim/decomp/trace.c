/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Boot hit census: the -finstrument-functions entry hook of the original units. */
#define _GNU_SOURCE
#include "trace.h"
#include <elf.h>
#include <link.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define NOINST __attribute__((no_instrument_function))
typedef struct {
    void *fn;
    unsigned index; /* entry index + 1; 0 marks a free slot */
    uintptr_t end; /* end of the standalone function, for real call-frame sampling */
    unsigned unframed;
} Slot;
static Slot *slots;
static unsigned slot_mask;
static unsigned char *resolved; /* per entry: 1 once a body was found */
static const char **found;      /* per entry: the name that resolved */
static unsigned char *hit;
static unsigned hits, eligible, shim_bound, unresolved, expected_noted, last_order;
static const char *last_name;
static volatile int armed;
static int summarised;
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static const char *events[8];
static unsigned event_count;
static __thread N64cTraceStack *own;   /* this thread's live instrumented frames */
static N64cTraceStack *stacks, *last_stack;
/* The running image's own symbol table (.symtab of /proc/self/exe): a name -> address lookup that needs no
 * dynamic exports, so --gc-sections keeps working. Functions the link dropped are simply absent. */
static const unsigned char *image;
static size_t image_size;
static const ElfW(Sym) *syms;
static unsigned sym_count;
static const char *strings;
NOINST static void symbols_open(void)
{
    int fd = open("/proc/self/exe", O_RDONLY);
    struct stat st;
    if (fd < 0 || fstat(fd, &st) || st.st_size < (off_t)sizeof(ElfW(Ehdr)))
        return;
    image = mmap(NULL, (size_t)st.st_size, PROT_READ, MAP_PRIVATE, fd, 0);
    close(fd);
    if (image == MAP_FAILED) {
        image = NULL;
        return;
    }
    image_size = (size_t)st.st_size;
    const ElfW(Ehdr) *eh = (const ElfW(Ehdr) *)image;
    if (memcmp(eh->e_ident, ELFMAG, SELFMAG) || eh->e_ident[EI_CLASS] != (sizeof(void *) == 4 ? ELFCLASS32 : ELFCLASS64) ||
        eh->e_shoff + (size_t)eh->e_shnum * sizeof(ElfW(Shdr)) > image_size)
        return;
    const ElfW(Shdr) *sh = (const ElfW(Shdr) *)(image + eh->e_shoff);
    for (unsigned i = 0; i < eh->e_shnum; i++) {
        if (sh[i].sh_type != SHT_SYMTAB || sh[i].sh_link >= eh->e_shnum)
            continue;
        const ElfW(Shdr) *str = &sh[sh[i].sh_link];
        if (sh[i].sh_offset + sh[i].sh_size > image_size || str->sh_offset + str->sh_size > image_size)
            return;
        syms = (const ElfW(Sym) *)(image + sh[i].sh_offset);
        sym_count = sh[i].sh_size / sizeof(ElfW(Sym));
        strings = (const char *)(image + str->sh_offset);
        return;
    }
}
NOINST static void *symbol_address(const char *name, uintptr_t *end)
{
    for (unsigned i = 0; i < sym_count; i++)
        if (ELF32_ST_TYPE(syms[i].st_info) == STT_FUNC && syms[i].st_value && strings[syms[i].st_name] == name[0] &&
            !strcmp(strings + syms[i].st_name, name)) {
            *end = syms[i].st_value + syms[i].st_size;
            return (void *)(uintptr_t)syms[i].st_value;
        }
    return NULL;
}
NOINST static unsigned slot_of(const void *fn)
{
    return ((unsigned)(uintptr_t)fn * 2654435761u) >> 7;
}
NOINST static const char *entry_name(unsigned i)
{
    return found[i] ? found[i] : n64c_trace_entries[i].names[0];
}
/* The first entry no body has reached yet; entries with no instrumented body cannot be reached. */
NOINST static int expected_entry(void)
{
    for (unsigned i = 0; i < n64c_trace_count; i++)
        if (resolved[i] && !hit[i])
            return (int)i;
    return -1;
}

/* ---- Lockstep (N64C_LOCKSTEP=1, diagnostic flavour): the port half of the oracle comparison (lockstep.py). ----
 * At the first hit of every watched trace function the RDRAM image is hashed per 4 KiB page; pages whose hash
 * changed since the previous point are listed in lockstep.jsonl and their bytes stored once per hash in pages.bin
 * (16-byte header: u32 page, u32 0, u64 hash; then the page). The port keeps RDRAM in its own (native) byte order;
 * lockstep.py owns every equivalence between that and the oracle's canonical big-endian bytes. The first
 * LOCKSTEP_CALLS calls of each trace function also record 8 argument words from the frame and, through a return
 * trampoline, eax/edx/st(0) at its return. N64C_LOCKSTEP_WATCH=<addr> records every change of that RDRAM word seen
 * at an instrumented entry or exit, naming the function. Nothing here runs unless N64C_LOCKSTEP=1. */
extern void *n64c_memory_ram(unsigned physical, unsigned bytes) __attribute__((weak));
enum { LS_PAGE = 4096, LS_CALLS = 8, LS_SHADOW = 256, LS_WATCH_LINES = 20000 };
static int ls_on, ls_log = -1, ls_store = -1;
static const unsigned char *ls_ram;
static unsigned ls_pages, ls_points, ls_stored_mask, ls_stored_n, ls_zero_stored;
static uint64_t *ls_last, *ls_stored;
static unsigned *ls_calls;
static char *ls_line;
static unsigned char *ls_seen; /* RDRAM as of the last point */
static pthread_mutex_t ls_lock = PTHREAD_MUTEX_INITIALIZER;
static volatile const uint32_t *ls_watch;
static uint32_t ls_watch_value;
static unsigned ls_watch_lines;
typedef struct {
    unsigned long frame, ret;
    unsigned entry, call;
} LsReturn;
static __thread LsReturn ls_shadow[LS_SHADOW];
static __thread unsigned ls_depth;
NOINST static long ls_tid(void)
{
    return (long)syscall(SYS_gettid);
}
/* FNV-1a over little-endian 64-bit words of the page bytes; lockstep.py and the oracle compute the same. */
NOINST uint64_t n64c_lockstep_hash(const unsigned char *page)
{
    uint64_t h = 0xcbf29ce484222325ull;
    for (unsigned i = 0; i < LS_PAGE; i += 8) {
        uint64_t w;
        memcpy(&w, page + i, 8);
        h = (h ^ w) * 0x100000001b3ull;
    }
    return h;
}
NOINST static void ls_write(int fd, const void *data, size_t size)
{
    const char *p = data;
    while (size) {
        const ssize_t n = write(fd, p, size);
        if (n <= 0)
            return;
        p += n;
        size -= (size_t)n;
    }
}
/* 1 when the hash is new to pages.bin (its content must be stored). */
NOINST static int ls_stored_add(uint64_t h)
{
    if (!h) {
        const int fresh = !ls_zero_stored;
        ls_zero_stored = 1;
        return fresh;
    }
    if ((ls_stored_n + 1) * 2 > ls_stored_mask) {
        const unsigned old_mask = ls_stored_mask;
        uint64_t *old = ls_stored, *grown = calloc((size_t)(old_mask + 1) * 2, sizeof *grown);
        if (!grown)
            return 1;
        ls_stored = grown;
        ls_stored_mask = old_mask * 2 + 1;
        for (unsigned i = 0; i <= old_mask; i++)
            if (old[i]) {
                unsigned s = (unsigned)(old[i] >> 17) & ls_stored_mask;
                while (ls_stored[s])
                    s = (s + 1) & ls_stored_mask;
                ls_stored[s] = old[i];
            }
        free(old);
    }
    unsigned s = (unsigned)(h >> 17) & ls_stored_mask;
    while (ls_stored[s] && ls_stored[s] != h)
        s = (s + 1) & ls_stored_mask;
    if (ls_stored[s])
        return 0;
    ls_stored[s] = h;
    ls_stored_n++;
    return 1;
}
NOINST static void ls_init(void)
{
    const char *on = getenv("N64C_LOCKSTEP"), *dir = getenv("N64C_LOCKSTEP_DIR"), *watch = getenv("N64C_LOCKSTEP_WATCH");
    if (!on || strcmp(on, "1") != 0)
        return;
    if (!dir || !*dir)
        dir = "lockstep";
    unsigned bytes = 0;
    for (unsigned b = 0x800000u; b >= 0x400000u && !ls_ram && n64c_memory_ram; b >>= 1)
        if ((ls_ram = n64c_memory_ram(0, b)) != NULL)
            bytes = b;
    char path[4096];
    mkdir(dir, 0777);
    snprintf(path, sizeof path, "%s/lockstep.jsonl", dir);
    ls_log = ls_ram ? open(path, O_WRONLY | O_CREAT | O_TRUNC | O_APPEND | O_CLOEXEC, 0666) : -1;
    snprintf(path, sizeof path, "%s/pages.bin", dir);
    ls_store = ls_log >= 0 ? open(path, O_WRONLY | O_CREAT | O_TRUNC | O_APPEND | O_CLOEXEC, 0666) : -1;
    ls_pages = bytes / LS_PAGE;
    ls_stored_mask = 4095;
    ls_last = calloc(ls_pages ? ls_pages : 1, sizeof *ls_last);
    ls_stored = calloc(ls_stored_mask + 1, sizeof *ls_stored);
    ls_calls = calloc(n64c_trace_count, sizeof *ls_calls);
    ls_line = malloc((size_t)ls_pages * 32 + 1024);
    ls_seen = malloc(bytes ? bytes : 1);
    if (ls_store < 0 || !ls_last || !ls_stored || !ls_calls || !ls_line || !ls_seen) {
        fprintf(stderr, "boot-lockstep: off (%s)\n", ls_ram ? "cannot write its files" : "no RDRAM");
        return;
    }
    if (watch && *watch) {
        char *end;
        const unsigned long address = strtoul(watch, &end, 16);
        const unsigned physical = (unsigned)address & 0x1FFFFFFFu;
        if (!*end && !(address & 3) && physical + 4 <= bytes) {
            ls_watch = (volatile const uint32_t *)(ls_ram + physical);
            ls_watch_value = *ls_watch;
        } else
            fprintf(stderr, "boot-lockstep: watch %s ignored (not an aligned RDRAM word)\n", watch);
    }
    dprintf(ls_log, "{\"k\":\"meta\",\"side\":\"port\",\"ram\":%u,\"page\":%u,\"bytes\":\"native\","
            "\"hash\":\"fnv1a64-le64\",\"calls\":%u,\"trace\":%u,\"watch\":\"%08lx\"}\n", bytes, LS_PAGE, LS_CALLS,
            n64c_trace_count, ls_watch ? 0x80000000ul + (unsigned long)((const unsigned char *)ls_watch - ls_ram) : 0ul);
    ls_on = 1;
    fprintf(stderr, "boot-lockstep: on, %u pages hashed at each first hit, files in %s\n", ls_pages, dir);
}
/* One point: called with the census lock held at the first hit of entry i. */
NOINST static void ls_point(unsigned i)
{
    char *p = ls_line;
    p += sprintf(p, "{\"k\":\"hit\",\"n\":%u,\"order\":%u,\"fn\":\"%s\",\"thr\":%ld,\"pages\":[", ls_points++,
                 n64c_trace_entries[i].order, entry_name(i), ls_tid());
    const char *sep = "";
    for (unsigned pg = 0; pg < ls_pages; pg++) {
        /* Only pages whose raw bytes changed since the last point are hashed: a point costs about one memcmp. */
        unsigned char *copy = ls_seen + (size_t)pg * LS_PAGE;
        if (ls_points > 1 && memcmp(copy, ls_ram + (size_t)pg * LS_PAGE, LS_PAGE) == 0)
            continue;
        memcpy(copy, ls_ram + (size_t)pg * LS_PAGE, LS_PAGE);
        const uint64_t h = n64c_lockstep_hash(copy);
        if (ls_points > 1 && h == ls_last[pg])
            continue;
        ls_last[pg] = h;
        if (ls_stored_add(h)) {
            uint32_t head[4] = {pg, 0, (uint32_t)h, (uint32_t)(h >> 32)};
            ls_write(ls_store, head, sizeof head);
            ls_write(ls_store, copy, LS_PAGE);
        }
        p += sprintf(p, "%s[%u,\"%016" PRIx64 "\"]", sep, pg, h);
        sep = ",";
    }
    p += sprintf(p, "]}\n");
    ls_write(ls_log, ls_line, (size_t)(p - ls_line));
}
NOINST static void ls_line_out(const char *line, int n)
{
    if (n <= 0)
        return;
    pthread_mutex_lock(&ls_lock);
    ls_write(ls_log, line, (size_t)n);
    pthread_mutex_unlock(&ls_lock);
}
NOINST static void ls_watch_check(const void *fn, const char *at)
{
    const uint32_t now = *ls_watch;
    if (now == __atomic_load_n(&ls_watch_value, __ATOMIC_RELAXED) || ls_watch_lines >= LS_WATCH_LINES)
        return;
    pthread_mutex_lock(&ls_lock);
    const uint32_t old = ls_watch_value;
    if (now != old && ls_watch_lines < LS_WATCH_LINES) {
        ls_watch_value = now;
        ls_watch_lines++;
        unsigned offset = 0;
        const char *name = n64c_trace_symbol(fn, &offset);
        dprintf(ls_log, "{\"k\":\"watch\",\"at\":\"%s\",\"fn\":\"%s\",\"thr\":%ld,\"old\":\"%08x\",\"new\":\"%08x\"}\n",
                at, name ? name : "?", ls_tid(), old, now);
    }
    pthread_mutex_unlock(&ls_lock);
}
#if defined(__i386__)
void n64c_lockstep_return_i386(void);
/* The trampoline a recorded call returns through: block holds st(0) (2 words), whether st(0) was full, edx, eax;
 * the returning function's stack pointer is just above it. Returns the original return address. */
NOINST __attribute__((used, force_align_arg_pointer)) unsigned long n64c_lockstep_returned_i386(const unsigned long *block)
{
    const unsigned long esp = (unsigned long)(block + 5);
    while (ls_depth && ls_shadow[ls_depth - 1].frame + 12 < esp) /* frames a longjmp or run-on unwound past */
        ls_depth--;
    if (!ls_depth || ls_shadow[ls_depth - 1].frame + 8 > esp) {
        fprintf(stderr, "boot-lockstep: return through no recorded frame (esp %08lx)\n", esp);
        _Exit(78);
    }
    const LsReturn r = ls_shadow[--ls_depth];
    char line[256], st0[24] = "null";
    if (block[2])
        snprintf(st0, sizeof st0, "\"%08lx%08lx\"", block[1], block[0]);
    ls_line_out(line, snprintf(line, sizeof line, "{\"k\":\"ret\",\"order\":%u,\"i\":%u,\"thr\":%ld,\"r\":[\"%08lx\","
                               "\"%08lx\"],\"st0\":%s}\n", n64c_trace_entries[r.entry].order, r.call, ls_tid(),
                               block[4], block[3], st0));
    return r.ret;
}
__asm__(".text\n"
        ".globl n64c_lockstep_return_i386\n"
        ".type n64c_lockstep_return_i386, @function\n"
        "n64c_lockstep_return_i386:\n"
        "    pushl %eax\n"
        "    pushl %edx\n"
        "    subl $12, %esp\n"
        "    movl $0, 8(%esp)\n"
        "    fxam\n"
        "    fnstsw %ax\n"
        "    andw $0x4500, %ax\n"
        "    cmpw $0x4100, %ax\n"
        "    je 1f\n"
        "    fstl (%esp)\n"
        "    movl $1, 8(%esp)\n"
        "1:  movl %esp, %eax\n"
        "    pushl %eax\n"
        "    call n64c_lockstep_returned_i386\n"
        "    addl $4, %esp\n"
        "    movl %eax, %ecx\n"
        "    addl $12, %esp\n"
        "    popl %edx\n"
        "    popl %eax\n"
        "    jmp *%ecx\n"
        ".size n64c_lockstep_return_i386, .-n64c_lockstep_return_i386\n");
#endif
/* A call of entry i: its argument words (the frame's caller-pushed area) and, through the trampoline, its return. */
NOINST static void ls_call(Slot *slot, void *call_site, const unsigned long *regs)
{
    if (!regs)
        return;
    const unsigned i = slot->index - 1;
    /* GCC instruments inlined bodies too. Their hook runs inside the caller, whose
     * ebp is not an argument frame for this function. Never sample or trampoline it.
     * Keep the first-hit memory point and make the missing call coverage explicit. */
    const uintptr_t pc = regs[4] - 1;
    if (pc < (uintptr_t)slot->fn || pc >= slot->end) {
        if (!__atomic_exchange_n(&slot->unframed, 1, __ATOMIC_RELAXED)) {
            char line[160];
            int n = snprintf(line, sizeof line,
                             "{\"k\":\"unframed\",\"order\":%u,\"pc\":\"%08lx\"}\n",
                             n64c_trace_entries[i].order, (unsigned long)pc);
            ls_line_out(line, n);
        }
        return;
    }
    const unsigned call = __atomic_fetch_add(&ls_calls[i], 1, __ATOMIC_RELAXED);
    if (call >= LS_CALLS)
        return;
    unsigned long *frame = (unsigned long *)regs[3];
    char line[400];
    int n = snprintf(line, sizeof line, "{\"k\":\"call\",\"order\":%u,\"i\":%u,\"thr\":%ld,\"sp\":\"%08lx\",\"a\":[",
                     n64c_trace_entries[i].order, call, ls_tid(), (unsigned long)frame);
    for (int k = 0; k < 8; k++)
        n += snprintf(line + n, sizeof line - (size_t)n, "%s\"%08lx\"", k ? "," : "", frame[2 + k]);
    n += snprintf(line + n, sizeof line - (size_t)n, "]}\n");
    ls_line_out(line, n);
#if defined(__i386__)
    if (frame[1] == (unsigned long)call_site && ls_depth < LS_SHADOW) {
        ls_shadow[ls_depth++] = (LsReturn){(unsigned long)frame, frame[1], i, call};
        frame[1] = (unsigned long)n64c_lockstep_return_i386;
    }
#else
    (void)call_site;
#endif
}
NOINST void n64c_trace_init(void)
{
    const unsigned total = n64c_trace_count;
    unsigned size = 1024;
    while (size < total * 4)
        size <<= 1;
    slots = calloc(size, sizeof *slots);
    resolved = calloc(total, 1);
    found = calloc(total, sizeof *found);
    hit = calloc(total, 1);
    if (!slots || !resolved || !found || !hit)
        return;
    slot_mask = size - 1;
    symbols_open();
    for (unsigned i = 0; i < total; i++) {
        const N64cTraceEntry *e = &n64c_trace_entries[i];
        if (e->shim_bound) {
            shim_bound++;
            continue;
        }
        void *fn = NULL;
        uintptr_t end = 0;
        const char *name = NULL;
        for (int k = 0; e->names[k] && !fn; k++)
            if ((fn = symbol_address(e->names[k], &end)) != NULL)
                name = e->names[k];
        unsigned s = slot_of(fn) & slot_mask;
        while (fn && slots[s].index && slots[s].fn != fn)
            s = (s + 1) & slot_mask;
        if (!fn || slots[s].index) { /* absent from the link, or an alias of an earlier entry */
            unresolved++;
            continue;
        }
        slots[s].fn = fn;
        slots[s].end = end;
        slots[s].index = i + 1;
        resolved[i] = 1;
        found[i] = name;
        eligible++;
    }
    fprintf(stderr, "boot-step: trace armed: %u of %u functions watched (%u shim-bound, %u not linked)\n",
            eligible, total, shim_bound, unresolved);
    fflush(stderr);
    ls_init();
    __atomic_store_n(&armed, 1, __ATOMIC_RELEASE);
}
NOINST static void first_hit(unsigned i)
{
    pthread_mutex_lock(&lock);
    if (!hit[i]) {
        const int expected = expected_entry();
        hit[i] = 1;
        hits++;
        last_order = n64c_trace_entries[i].order;
        last_name = entry_name(i);
        if (ls_on)
            ls_point(i);
        fprintf(stderr, "boot-step: %u/%u %s\n", hits, n64c_trace_count, last_name);
        if (expected >= 0 && (unsigned)expected != i && (unsigned)expected + 1 != expected_noted) {
            expected_noted = (unsigned)expected + 1;
            fprintf(stderr, "boot-step: out of oracle order: %s (order %u) before %s (order %u)\n", last_name,
                    last_order, entry_name((unsigned)expected), n64c_trace_entries[expected].order);
        }
        fflush(stderr);
    }
    pthread_mutex_unlock(&lock);
}
/* The frame record of one instrumented entry. regs: ebx, esi, edi, ebp (the entered function's frame), the hook's
 * return address, then the two hook arguments (fn, call_site), as the i386 entry stub below lays them out. */
NOINST static void push_frame(void *fn, void *call_site, const unsigned long *regs)
{
    N64cTraceStack *mine = own;
    if (!mine) {
        if (!(mine = calloc(1, sizeof *mine)))
            return;
        mine->thread = (unsigned long)pthread_self();
        pthread_mutex_lock(&lock);
        mine->next = stacks;
        stacks = mine;
        pthread_mutex_unlock(&lock);
        own = mine;
    }
    if (mine->depth < N64C_TRACE_DEPTH) {
        N64cTraceFrame *f = &mine->frame[mine->depth];
        f->fn = fn;
        f->site = call_site;
        f->frame = regs ? (void *)regs[3] : NULL;
        for (int i = 0; i < 3; i++)
            f->regs[i] = regs ? regs[i] : 0;
    }
    mine->depth++;
    last_stack = mine;
}
NOINST static void trace_enter(void *fn, void *call_site, const unsigned long *regs)
{
    if (!__atomic_load_n(&armed, __ATOMIC_ACQUIRE))
        return;
    push_frame(fn, call_site, regs);
    if (ls_watch)
        ls_watch_check(fn, "enter");
    for (unsigned s = slot_of(fn) & slot_mask; slots[s].index; s = (s + 1) & slot_mask)
        if (slots[s].fn == fn) {
            const unsigned i = slots[s].index - 1;
            if (!__atomic_load_n(&hit[i], __ATOMIC_RELAXED))
                first_hit(i);
            if (ls_on)
                ls_call(&slots[s], call_site, regs);
            break;
        }
}
#if defined(__i386__)
/* The entry hook keeps the callee-saved registers as the instrumented function's caller left them: ebx, esi and
 * edi are not touched by a function before it saves them, and ebp is the function's own frame. */
NOINST __attribute__((used, force_align_arg_pointer)) void n64c_trace_enter_i386(const unsigned long *regs)
{
    trace_enter((void *)regs[5], (void *)regs[6], regs);
}
__asm__(".text\n"
        ".globl __cyg_profile_func_enter\n"
        ".type __cyg_profile_func_enter, @function\n"
        "__cyg_profile_func_enter:\n"
        "    pushl %ebp\n"
        "    pushl %edi\n"
        "    pushl %esi\n"
        "    pushl %ebx\n"
        "    movl %esp, %eax\n"
        "    subl $8, %esp\n"
        "    pushl %eax\n"
        "    call n64c_trace_enter_i386\n"
        "    addl $12, %esp\n"
        "    popl %ebx\n"
        "    popl %esi\n"
        "    popl %edi\n"
        "    popl %ebp\n"
        "    ret\n"
        ".size __cyg_profile_func_enter, .-__cyg_profile_func_enter\n");
#else
NOINST void __cyg_profile_func_enter(void *fn, void *call_site)
{
    trace_enter(fn, call_site, NULL);
}
#endif
NOINST void __cyg_profile_func_exit(void *fn, void *call_site)
{
    (void)call_site;
    if (ls_watch)
        ls_watch_check(fn, "exit");
    N64cTraceStack *mine = own;
    if (!mine || !mine->depth)
        return;
    /* A frame entered before the census armed has no record: only the matching record is dropped. */
    if (mine->depth > N64C_TRACE_DEPTH || mine->frame[mine->depth - 1].fn == fn)
        mine->depth--;
}
NOINST N64cTraceStack *n64c_trace_stack(void)
{
    return own;
}
NOINST N64cTraceStack *n64c_trace_last_stack(void)
{
    return last_stack;
}
NOINST N64cTraceStack *n64c_trace_stacks(void)
{
    pthread_mutex_lock(&lock);
    N64cTraceStack *first = stacks;
    pthread_mutex_unlock(&lock);
    return first;
}
NOINST const char *n64c_trace_symbol(const void *address, unsigned *offset)
{
    const uintptr_t at = (uintptr_t)address;
    for (unsigned i = 0; i < sym_count; i++)
        if (ELF32_ST_TYPE(syms[i].st_info) == STT_FUNC && syms[i].st_value <= at && syms[i].st_size &&
            at - syms[i].st_value < syms[i].st_size) {
            if (offset)
                *offset = (unsigned)(at - syms[i].st_value);
            return strings + syms[i].st_name;
        }
    return NULL;
}
NOINST unsigned n64c_trace_position(unsigned *total, const char **last)
{
    pthread_mutex_lock(&lock);
    const unsigned n = hits;
    if (last)
        *last = last_name;
    pthread_mutex_unlock(&lock);
    if (total)
        *total = n64c_trace_count;
    return n;
}
NOINST void n64c_trace_event(const char *kind)
{
    pthread_mutex_lock(&lock);
    unsigned i = 0;
    while (i < event_count && strcmp(events[i], kind) != 0)
        i++;
    if (i == event_count && event_count < sizeof events / sizeof *events) {
        events[event_count++] = kind;
        fprintf(stderr, "boot-step: %s (after %u/%u)\n", kind, hits, n64c_trace_count);
        fflush(stderr);
    }
    pthread_mutex_unlock(&lock);
}
NOINST void n64c_trace_summary(void)
{
    pthread_mutex_lock(&lock);
    if (armed && !summarised) {
        summarised = 1;
        const int expected = expected_entry();
        fprintf(stderr, "boot-step: census %u/%u hit, %u watched, last %s (order %u)", hits, n64c_trace_count,
                eligible, last_name ? last_name : "none", last_order);
        if (expected >= 0)
            fprintf(stderr, ", next in oracle order %s (order %u)", entry_name((unsigned)expected),
                    n64c_trace_entries[expected].order);
        fputc('\n', stderr);
        if (ls_on)
            fprintf(stderr, "boot-lockstep: %u points written\n", ls_points);
        fflush(stderr);
    }
    pthread_mutex_unlock(&lock);
}
