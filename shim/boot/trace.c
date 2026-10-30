/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Boot hit census: the -finstrument-functions entry hook of the original units. */
#define _GNU_SOURCE
#include "trace.h"
#include "reach.h"
#include <elf.h>
#include <link.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <inttypes.h>
#include <pthread.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <stddef.h>
#include <stdint.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define NOINST __attribute__((no_instrument_function))
/* The entry hook reads the instrumented caller's saved registers straight off the stack, below any object's frame:
 * AddressSanitizer would report those reads as stack-buffer-overflow, so the hook path is not sanitized. */
#define RAWSTACK __attribute__((no_sanitize_address))
typedef struct {
    void *fn;
    unsigned index; /* entry index + 1; 0 marks a free slot */
    uintptr_t end; /* end of the standalone function, for real call-frame sampling */
    unsigned unframed;
} Slot;
static Slot *slots;
static unsigned slot_mask;
typedef struct {
    void *fn;
    unsigned index;
    unsigned entered;
} ShimSlot;
static ShimSlot *shim_slots;
static unsigned shim_mask;
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
/* Per-call capture (N64C_CALLS=DIR): the lockstep call/ret machinery without the page hashing, for whole movies. */
enum { CA_SLOTS = 1 << 20, CA_PROBES = 64, CA_FIRST = 16 };
typedef struct {
    uint64_t key; /* caller address << 32 | callee entry + 1; 0 = free */
    unsigned n;   /* calls from that caller to that callee so far */
} CaSlot;
static CaSlot *ca_table;
static int ca_on;
static unsigned ls_limit = LS_CALLS; /* calls of one function recorded: LS_CALLS in lockstep, N64C_CALLS_FIRST here */
static long reach_poll;
static char *ls_line;
static unsigned char *ls_seen; /* RDRAM as of the last point */
static pthread_mutex_t ls_lock = PTHREAD_MUTEX_INITIALIZER;
static volatile const uint32_t *ls_watch;
static uint32_t ls_watch_value;
static unsigned ls_watch_lines;
typedef struct {
    unsigned long frame, ret;
    unsigned entry, call;
    unsigned long arg[8]; /* the call's argument words as entered (cdecl area), for the return-time memory windows */
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
/* N64C_CALLS=DIR: DIR/calls.jsonl gets the `call` and `ret` lines of lockstep for the first N64C_CALLS_FIRST (default
 * 16) calls that each caller makes of each census function, each call also naming its caller (the logical parent
 * frame's symbol `caller`), its ordinal `o` among that caller's calls of the function (all calls are counted, recorded
 * or not) and the poll `frame` it ran in, so a whole movie is aligned call by call with the oracle's by (function,
 * caller, ordinal) whatever phase the two sides' polls are in. Off when lockstep is on. */
NOINST static unsigned ca_ordinal(const void *caller, unsigned callee)
{
    const uint64_t key = ((uint64_t)(uintptr_t)caller << 32) | (callee + 1);
    unsigned s = (unsigned)((key * 0x9E3779B97F4A7C15ull) >> 40) & (CA_SLOTS - 1);
    for (unsigned probe = 0; probe < CA_PROBES; probe++, s = (s + 1) & (CA_SLOTS - 1)) {
        uint64_t seen = __atomic_load_n(&ca_table[s].key, __ATOMIC_RELAXED);
        if (!seen && __atomic_compare_exchange_n(&ca_table[s].key, &seen, key, 0, __ATOMIC_RELAXED, __ATOMIC_RELAXED))
            seen = key;
        if (seen == key)
            return __atomic_fetch_add(&ca_table[s].n, 1, __ATOMIC_RELAXED);
    }
    return UINT_MAX; /* table full around this key: the ordinal is unknown */
}
/* Memory a call leaves its results in (the port half of the original's `m`): at the return of a recorded call, every
 * argument word that points into RDRAM or the executable's own segments names a CA_WINDOW-word window, read as the
 * 32-bit values the original's code would see. A word of the window that is itself such a pointer reads `pppppppp`
 * (native addresses are not the original's; whether it is a pointer is what both sides agree on). Stack out-parameters
 * are not read: their neighbours are the port's own frame layout. dl_iterate_phdr names the readable segments once. */
enum { CA_WINDOW = 16, CA_SEGMENTS = 12 };
static unsigned long ca_seg_lo[CA_SEGMENTS], ca_seg_hi[CA_SEGMENTS], ca_ram_lo, ca_ram_hi;
static unsigned ca_segments;
NOINST static int ca_collect(struct dl_phdr_info *info, size_t size, void *data)
{
    (void)size;
    (void)data;
    for (unsigned k = 0; k < info->dlpi_phnum && ca_segments < CA_SEGMENTS; k++)
        if (info->dlpi_phdr[k].p_type == PT_LOAD && (info->dlpi_phdr[k].p_flags & PF_R)) {
            ca_seg_lo[ca_segments] = info->dlpi_addr + info->dlpi_phdr[k].p_vaddr;
            ca_seg_hi[ca_segments++] = info->dlpi_addr + info->dlpi_phdr[k].p_vaddr + info->dlpi_phdr[k].p_memsz;
        }
    return 1; /* the first object is the executable */
}
NOINST static int ca_points_in(unsigned long p, unsigned long bytes)
{
    if (ca_ram_lo && p >= ca_ram_lo && p < ca_ram_hi && ca_ram_hi - p >= bytes) /* no p + bytes: it wraps at 4 GiB on i386 */
        return 1;
    for (unsigned k = 0; k < ca_segments; k++)
        if (p >= ca_seg_lo[k] && p < ca_seg_hi[k] && ca_seg_hi[k] - p >= bytes)
            return 1;
    return 0;
}
NOINST static int ca_windows(char *out, size_t cap, const unsigned long *args)
{
    int n = snprintf(out, cap, "\"m\":{");
    const char *sep = "";
    for (int k = 0; k < 8; k++) {
        const unsigned long p = args[k];
        if ((p & 3) || p < 0x1000 || !ca_points_in(p, CA_WINDOW * 4))
            continue;
        n += snprintf(out + n, cap - (size_t)n, "%s\"%d\":\"", sep, k);
        sep = ",";
        for (int j = 0; j < CA_WINDOW; j++) {
            const unsigned long w = ((const volatile uint32_t *)p)[j];
            if (w >= 0x1000 && ca_points_in(w, 1))
                n += snprintf(out + n, cap - (size_t)n, "pppppppp");
            else
                n += snprintf(out + n, cap - (size_t)n, "%08lx", w);
        }
        n += snprintf(out + n, cap - (size_t)n, "\"");
    }
    return n + snprintf(out + n, cap - (size_t)n, "}");
}
/* The calls of one (caller, function) pair a recorded run keeps: the first `first`, then every power of two (a ring that
 * both sides keep at the same ordinals, so the later calls of a long movie are aligned too). */
NOINST static int ca_keeps(unsigned ordinal, unsigned first)
{
    return ordinal < first || (ordinal & (ordinal - 1)) == 0;
}
NOINST static void ca_init(void)
{
    const char *dir = getenv("N64C_CALLS"), *first = getenv("N64C_CALLS_FIRST");
    if (!dir || !*dir || ls_on)
        return;
    char path[4096];
    mkdir(dir, 0777);
    snprintf(path, sizeof path, "%s/calls.jsonl", dir);
    ls_log = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_APPEND | O_CLOEXEC, 0666);
    ls_calls = calloc(n64c_trace_count ? n64c_trace_count : 1, sizeof *ls_calls);
    ca_table = calloc(CA_SLOTS, sizeof *ca_table);
    if (ls_log < 0 || !ls_calls || !ca_table) {
        fprintf(stderr, "boot-calls: off (cannot write its files)\n");
        free(ls_calls);
        free(ca_table);
        ls_calls = NULL;
        ca_table = NULL;
        return;
    }
    ls_limit = first && *first ? (unsigned)strtoul(first, NULL, 10) : CA_FIRST;
    dl_iterate_phdr(ca_collect, NULL);
    for (unsigned b = 0x800000u; b >= 0x400000u && !ca_ram_lo && n64c_memory_ram; b >>= 1) {
        const unsigned char *ram = n64c_memory_ram(0, b);
        if (ram) {
            ca_ram_lo = (unsigned long)ram;
            ca_ram_hi = ca_ram_lo + b;
        }
    }
    dprintf(ls_log, "{\"k\":\"meta\",\"side\":\"port\",\"mode\":\"calls\",\"first\":%u,\"trace\":%u,"
            "\"ring\":\"pow2\",\"window\":%d}\n", ls_limit, n64c_trace_count, CA_WINDOW);
    ca_on = 1;
    fprintf(stderr, "boot-calls: on, first %u calls of each function, file %s\n", ls_limit, path);
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
    char line[2048], st0[24] = "null";
    if (block[2])
        snprintf(st0, sizeof st0, "\"%08lx%08lx\"", block[1], block[0]);
    int n = snprintf(line, sizeof line, "{\"k\":\"ret\",\"order\":%u,\"i\":%u,\"thr\":%ld,\"r\":[\"%08lx\","
                     "\"%08lx\"],\"st0\":%s", n64c_trace_entries[r.entry].order, r.call, ls_tid(), block[4], block[3], st0);
    if (ca_on) {
        line[n++] = ',';
        n += ca_windows(line + n, sizeof line - (size_t)n, r.arg);
    }
    n += snprintf(line + n, sizeof line - (size_t)n, "}\n");
    ls_line_out(line, n);
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
    /* Recursive inlining stays inside the same standalone symbol. Its logical
     * parent still owns the physical frame, so the PC range alone is insufficient.
     * trace_enter has already pushed this entry onto the logical stack. */
    const N64cTraceStack *stack = own;
    const int shared_frame = stack && (stack->depth > N64C_TRACE_DEPTH ||
        (stack->depth > 1 && stack->frame[stack->depth - 2].frame == (void *)regs[3]));
    if (shared_frame || pc < (uintptr_t)slot->fn || pc >= slot->end) {
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
    unsigned ordinal = 0;
    const char *caller = "?";
    if (ca_on) {
        const void *parent = stack && stack->depth > 1 ? stack->frame[stack->depth - 2].fn : NULL;
        ordinal = ca_ordinal(parent, i);
        if (!ca_keeps(ordinal, ls_limit))
            return;
        unsigned off;
        if (parent && (caller = n64c_trace_symbol(parent, &off)) == NULL)
            caller = "?";
    } else if (call >= LS_CALLS)
        return;
    unsigned long *frame = (unsigned long *)regs[3];
    char line[640];
    int n = snprintf(line, sizeof line, "{\"k\":\"call\",\"order\":%u,\"i\":%u,\"thr\":%ld,\"sp\":\"%08lx\",",
                     n64c_trace_entries[i].order, call, ls_tid(), (unsigned long)frame);
    if (ca_on)
        n += snprintf(line + n, sizeof line - (size_t)n,
                      "\"fn\":\"%s\",\"frame\":%ld,\"caller\":\"%.160s\",\"o\":%u,", entry_name(i), reach_poll, caller,
                      ordinal);
    n += snprintf(line + n, sizeof line - (size_t)n, "\"a\":[");
    for (int k = 0; k < 8; k++)
        n += snprintf(line + n, sizeof line - (size_t)n, "%s\"%08lx\"", k ? "," : "", frame[2 + k]);
    n += snprintf(line + n, sizeof line - (size_t)n, "]}\n");
    ls_line_out(line, n);
#if defined(__i386__)
    if (frame[1] == (unsigned long)call_site && ls_depth < LS_SHADOW) {
        LsReturn *shadow = &ls_shadow[ls_depth++];
        *shadow = (LsReturn){(unsigned long)frame, frame[1], i, call, {0}};
        if (ca_on)
            memcpy(shadow->arg, frame + 2, sizeof shadow->arg);
        frame[1] = (unsigned long)n64c_lockstep_return_i386;
    }
#else
    (void)call_site;
#endif
}
/* Reach stream (GAME-RUNNER 10a). N64C_REACH_ROWS names the bake's fnrows.txt, one function name per fns.json row: every
 * function of the image's own symbol table with such a name gets its row. The entry hook counts entries per row, and
 * n64c_trace_reach_tick (once per poll, at most 30 a second) turns the counts into records for the datagram socket
 * N64C_INPUT + ".reach", which the run's server reads. A name the image does not link, or a build without both
 * variables, simply has no rows or no stream. */
typedef struct {
    void *fn;
    unsigned row;
} RowSlot;
static RowSlot *row_slots;
static unsigned row_mask;
static unsigned *row_entered;
static N64cReach reach;
static int reach_fd = -1;
static socklen_t reach_addr_len;
static struct sockaddr_un reach_addr;
static unsigned long long reach_last_ms;
/* Census-only: while the reach stream is live and nothing reads the logical call stacks (no run-on diagnostics, no
 * last-store watch), the entry hook only counts: it skips the frame record push_frame and the exit hook keep per call. */
static int census_only;
/* The entry hook's one table, keyed by function address: the four per-function facts (the shim bind still to announce,
 * the reach row, the census entry, the guest work charge) behind a single probe. Built once, after the source tables are
 * complete. */
typedef struct {
    void *fn;
    Slot *census;       /* the census entry of this function, NULL when it has none */
    unsigned row;       /* reach row + 1, 0 for none */
    unsigned shim_left; /* shim binds of this function not yet announced; 0 skips the shim probe */
    int charged;        /* an original function whose entry charges guest work (n64c_trace_charged) */
} HookSlot;
static HookSlot *hooks;
static unsigned hook_mask;
static unsigned row_hash(const char *name)
{
    unsigned h = 2166136261u;
    for (; *name; name++)
        h = (h ^ (unsigned char)*name) * 16777619u;
    return h;
}
NOINST static unsigned long long now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (unsigned long long)ts.tv_sec * 1000 + (unsigned long long)ts.tv_nsec / 1000000;
}
/* The rows file's names, one per line; row i is line i. Returns the row count (0: no stream). */
NOINST static unsigned reach_names(const char *path, char **text, const char ***names)
{
    FILE *f = fopen(path, "rb");
    if (!f)
        return 0;
    fseek(f, 0, SEEK_END);
    const long size = ftell(f);
    rewind(f);
    char *buf = size > 0 ? malloc((size_t)size + 1) : NULL;
    if (!buf || fread(buf, 1, (size_t)size, f) != (size_t)size) {
        free(buf);
        fclose(f);
        return 0;
    }
    fclose(f);
    buf[size] = 0;
    unsigned rows = 0;
    for (long i = 0; i < size; i++)
        rows += buf[i] == '\n';
    const char **list = malloc((rows + 1) * sizeof *list);
    if (!list) {
        free(buf);
        return 0;
    }
    unsigned n = 0;
    for (char *line = buf, *nl; n < rows && (nl = strchr(line, '\n')); line = nl + 1) {
        *nl = 0;
        list[n++] = line;
    }
    *text = buf;
    *names = list;
    return n;
}
NOINST static void reach_init(void)
{
    const char *rows_path = getenv("N64C_REACH_ROWS"), *input = getenv("N64C_INPUT");
    if (!rows_path || !*rows_path || !input || input[0] != '@' || !syms)
        return;
    char *text = NULL;
    const char **names = NULL;
    const unsigned rows = reach_names(rows_path, &text, &names);
    if (!rows)
        return;
    unsigned size = 1024;
    while (size < rows * 2)
        size <<= 1;
    unsigned *by_name = calloc(size, sizeof *by_name); /* name hash table: row + 1 */
    row_entered = calloc(rows, sizeof *row_entered);
    if (!by_name || !row_entered || !n64c_reach_init(&reach, rows, row_entered))
        goto out;
    for (unsigned i = 0; i < rows; i++) {
        unsigned s = row_hash(names[i]) & (size - 1);
        while (by_name[s] && strcmp(names[by_name[s] - 1], names[i]))
            s = (s + 1) & (size - 1);
        if (!by_name[s]) /* a repeated name keeps its first row */
            by_name[s] = i + 1;
    }
    unsigned matches = 0;
    for (int pass = 0; pass < 2; pass++) {
        if (pass) {
            unsigned slots_size = 1024;
            while (slots_size < matches * 2)
                slots_size <<= 1;
            row_slots = calloc(slots_size, sizeof *row_slots);
            if (!row_slots)
                goto out;
            row_mask = slots_size - 1;
        }
        for (unsigned i = 0; i < sym_count; i++) {
            if (ELF32_ST_TYPE(syms[i].st_info) != STT_FUNC || !syms[i].st_value)
                continue;
            const char *name = strings + syms[i].st_name;
            unsigned s = row_hash(name) & (size - 1);
            while (by_name[s] && strcmp(names[by_name[s] - 1], name))
                s = (s + 1) & (size - 1);
            if (!by_name[s])
                continue;
            if (!pass) {
                matches++;
                continue;
            }
            void *fn = (void *)(uintptr_t)syms[i].st_value;
            unsigned t = slot_of(fn) & row_mask;
            while (row_slots[t].fn && row_slots[t].fn != fn)
                t = (t + 1) & row_mask;
            if (!row_slots[t].fn) { /* an alias of the same address keeps the first row */
                row_slots[t].fn = fn;
                row_slots[t].row = by_name[s] - 1;
            }
        }
    }
    reach_fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    const size_t len = strlen(input) + strlen(".reach");
    if (reach_fd < 0 || len >= sizeof reach_addr.sun_path) {
        if (reach_fd >= 0)
            close(reach_fd);
        reach_fd = -1;
        goto out;
    }
    reach_addr.sun_family = AF_UNIX; /* the abstract namespace: sun_path[0] stays 0 */
    snprintf(reach_addr.sun_path + 1, sizeof reach_addr.sun_path - 1, "%s.reach", input + 1);
    reach_addr_len = (socklen_t)(offsetof(struct sockaddr_un, sun_path) + len);
    fprintf(stderr, "boot-step: reach stream: %u rows, %u linked, to %s.reach\n", rows, matches, input);
out:
    free(by_name);
    free(text);
    free(names);
    if (reach_fd < 0) {
        free(row_slots);
        row_slots = NULL;
        free(row_entered);
        row_entered = NULL;
        n64c_reach_free(&reach);
    }
}
/* Once per poll: at most 30 records a second, each a datagram that is dropped when nothing listens or its queue is full
 * (the next full record heals the receiver). The first one is the full set so far. */
NOINST void n64c_trace_reach_tick(long poll)
{
    reach_poll = poll;
    if (reach_fd < 0)
        return;
    const unsigned long long now = now_ms();
    if (reach.started && now - reach_last_ms < 33)
        return;
    reach_last_ms = now;
    unsigned char record[N64C_REACH_MAX];
    const size_t size = n64c_reach_next(&reach, (uint32_t)poll, record);
    if (size)
        sendto(reach_fd, record, size, MSG_DONTWAIT | MSG_NOSIGNAL, (const struct sockaddr *)&reach_addr, reach_addr_len);
}
/* At the run's end: one last full record, so the receiver's set is the final one however the throttle or a dropped datagram left it. */
NOINST static void reach_final(void)
{
    if (reach_fd < 0)
        return;
    unsigned char record[N64C_REACH_MAX];
    n64c_reach_next(&reach, (uint32_t)reach_poll, record); /* takes up the counts since the last tick */
    const size_t size = n64c_reach_full(&reach, (uint32_t)reach_poll, record);
    sendto(reach_fd, record, size, MSG_DONTWAIT | MSG_NOSIGNAL, (const struct sockaddr *)&reach_addr, reach_addr_len);
}
NOINST static HookSlot *hook_slot(void *fn)
{
    unsigned s = slot_of(fn) & hook_mask;
    while (hooks[s].fn && hooks[s].fn != fn)
        s = (s + 1) & hook_mask;
    hooks[s].fn = fn;
    return &hooks[s];
}
/* False when the table cannot be allocated: the trace then stays unarmed. */
NOINST static int hooks_build(void)
{
    unsigned count = n64c_shim_count + n64c_trace_count + n64c_trace_charged_count + (row_slots ? row_mask + 1 : 0);
    unsigned size = 1024, names = 1024;
    while (size < count * 2)
        size <<= 1;
    while (names < n64c_trace_charged_count * 2)
        names <<= 1;
    unsigned *charged = calloc(names, sizeof *charged); /* name hash table: charged index + 1 */
    if (!charged || !(hooks = calloc(size, sizeof *hooks))) {
        free(charged);
        return 0;
    }
    hook_mask = size - 1;
    /* The charged functions by name, in one pass over the image's symbol table. */
    for (unsigned i = 0; i < n64c_trace_charged_count; i++) {
        unsigned s = row_hash(n64c_trace_charged[i]) & (names - 1);
        while (charged[s])
            s = (s + 1) & (names - 1);
        charged[s] = i + 1;
    }
    for (unsigned i = 0; i < sym_count; i++) {
        if (ELF32_ST_TYPE(syms[i].st_info) != STT_FUNC || !syms[i].st_value)
            continue;
        const char *name = strings + syms[i].st_name;
        unsigned s = row_hash(name) & (names - 1);
        while (charged[s] && strcmp(n64c_trace_charged[charged[s] - 1], name))
            s = (s + 1) & (names - 1);
        if (charged[s])
            hook_slot((void *)(uintptr_t)syms[i].st_value)->charged = 1;
    }
    free(charged);
    for (unsigned s = 0; s <= shim_mask; s++)
        if (shim_slots[s].index)
            hook_slot(shim_slots[s].fn)->shim_left++;
    for (unsigned s = 0; s <= slot_mask; s++)
        if (slots[s].index)
            hook_slot(slots[s].fn)->census = &slots[s];
    if (row_slots)
        for (unsigned s = 0; s <= row_mask; s++)
            if (row_slots[s].fn)
                hook_slot(row_slots[s].fn)->row = row_slots[s].row + 1;
    return 1;
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
    unsigned shim_size = 1024;
    while (shim_size < n64c_shim_count * 4)
        shim_size <<= 1;
    shim_slots = calloc(shim_size, sizeof *shim_slots);
    if (!shim_slots)
        return;
    shim_mask = shim_size - 1;
    for (unsigned i = 0; i < n64c_shim_count; i++) {
        fprintf(stderr, "boot-shim-bind: %s %s\n", n64c_shim_entries[i].native, n64c_shim_entries[i].bound);
        uintptr_t end = 0;
        void *fn = symbol_address(n64c_shim_entries[i].native, &end);
        if (!fn)
            continue;
        unsigned s = slot_of(fn) & shim_mask;
        while (shim_slots[s].index)
            s = (s + 1) & shim_mask;
        shim_slots[s].fn = fn;
        shim_slots[s].index = i + 1;
    }
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
    ca_init();
    reach_init();
    const char *run_on = getenv("N64C_RUN_ON");
    census_only = row_slots && !ls_on && !ca_on && !ls_watch && !(run_on && !strcmp(run_on, "1"));
    if (!hooks_build())
        return;
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
NOINST RAWSTACK static void push_frame(void *fn, void *call_site, const unsigned long *regs)
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
/* The work measure both sides charge guest time by (RUNTIME.md "Virtual guest time"): one tick per entry of an original
 * function the port compiles, never of the shim's own instrumented code (n64c_sched.c; a build without the scheduler
 * links none and charges nothing). */
void n64c_clock_work(void) __attribute__((weak));
NOINST RAWSTACK static void trace_enter(void *fn, void *call_site, const unsigned long *regs)
{
    if (!__atomic_load_n(&armed, __ATOMIC_ACQUIRE))
        return;
    HookSlot *h = &hooks[slot_of(fn) & hook_mask];
    while (h->fn && h->fn != fn)
        h = &hooks[(unsigned)(h + 1 - hooks) & hook_mask];
    if (!h->fn)
        h = NULL; /* none of the three tables knows this function */
    if (h && h->charged && n64c_clock_work)
        n64c_clock_work();
    if (h && h->shim_left) {
        for (unsigned s = slot_of(fn) & shim_mask; shim_slots[s].index; s = (s + 1) & shim_mask) {
            if (shim_slots[s].fn != fn || __atomic_load_n(&shim_slots[s].entered, __ATOMIC_RELAXED))
                continue;
            pthread_mutex_lock(&lock);
            if (!shim_slots[s].entered) {
                __atomic_store_n(&shim_slots[s].entered, 1, __ATOMIC_RELAXED);
                h->shim_left--;
                const N64cShimEntry *e = &n64c_shim_entries[shim_slots[s].index - 1];
                fprintf(stderr, "boot-shim: %s %s\n", e->native, e->bound);
                fflush(stderr);
            }
            pthread_mutex_unlock(&lock);
        }
    }
    if (h && h->row) /* a plain load and store: a count lost to a race still reads as entered */
        __atomic_store_n(&row_entered[h->row - 1], __atomic_load_n(&row_entered[h->row - 1], __ATOMIC_RELAXED) + 1,
                         __ATOMIC_RELAXED);
    if (!census_only)
        push_frame(fn, call_site, regs);
    if (ls_watch)
        ls_watch_check(fn, "enter");
    if (h && h->census) {
        const unsigned i = h->census->index - 1;
        if (!__atomic_load_n(&hit[i], __ATOMIC_RELAXED))
            first_hit(i);
        if (ls_on || ca_on)
            ls_call(h->census, call_site, regs);
    }
}
#if defined(__i386__)
/* The entry hook keeps the callee-saved registers as the instrumented function's caller left them: ebx, esi and
 * edi are not touched by a function before it saves them, and ebp is the function's own frame. */
NOINST RAWSTACK __attribute__((used, force_align_arg_pointer)) void n64c_trace_enter_i386(const unsigned long *regs)
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
    if (census_only)
        return;
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
    reach_final();
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
