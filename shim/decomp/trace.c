/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Boot hit census: the -finstrument-functions entry hook of the original units. */
#define _GNU_SOURCE
#include "trace.h"
#include <elf.h>
#include <link.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define NOINST __attribute__((no_instrument_function))
typedef struct {
    void *fn;
    unsigned index; /* entry index + 1; 0 marks a free slot */
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
NOINST static void *symbol_address(const char *name)
{
    for (unsigned i = 0; i < sym_count; i++)
        if (ELF32_ST_TYPE(syms[i].st_info) == STT_FUNC && syms[i].st_value && strings[syms[i].st_name] == name[0] &&
            !strcmp(strings + syms[i].st_name, name))
            return (void *)(uintptr_t)syms[i].st_value;
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
        const char *name = NULL;
        for (int k = 0; e->names[k] && !fn; k++)
            if ((fn = symbol_address(e->names[k])) != NULL)
                name = e->names[k];
        unsigned s = slot_of(fn) & slot_mask;
        while (fn && slots[s].index && slots[s].fn != fn)
            s = (s + 1) & slot_mask;
        if (!fn || slots[s].index) { /* absent from the link, or an alias of an earlier entry */
            unresolved++;
            continue;
        }
        slots[s].fn = fn;
        slots[s].index = i + 1;
        resolved[i] = 1;
        found[i] = name;
        eligible++;
    }
    fprintf(stderr, "boot-step: trace armed: %u of %u functions watched (%u shim-bound, %u not linked)\n",
            eligible, total, shim_bound, unresolved);
    fflush(stderr);
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
NOINST void __cyg_profile_func_enter(void *fn, void *call_site)
{
    (void)call_site;
    if (!__atomic_load_n(&armed, __ATOMIC_ACQUIRE))
        return;
    for (unsigned s = slot_of(fn) & slot_mask; slots[s].index; s = (s + 1) & slot_mask)
        if (slots[s].fn == fn) {
            const unsigned i = slots[s].index - 1;
            if (!__atomic_load_n(&hit[i], __ATOMIC_RELAXED))
                first_hit(i);
            return;
        }
}
NOINST void __cyg_profile_func_exit(void *fn, void *call_site)
{
    (void)fn;
    (void)call_site;
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
        fflush(stderr);
    }
    pthread_mutex_unlock(&lock);
}
