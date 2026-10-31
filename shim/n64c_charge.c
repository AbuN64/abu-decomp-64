/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The original-function entry charge (n64c_charge.h): the project's calibration and charged table, the entry hook. */
#include "n64c_charge.h"
#include "n64c_internal.h"
#include <elf.h>
#include <fcntl.h>
#include <link.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

/* Declared once by the project in its generated charged table (charged.py): the work scale as a decimal string in
 * guest ns per entry, and the charged functions' names. Absent from a link that has no table: init refuses it. */
extern const char n64c_guest_ns_per_entry[] __attribute__((weak));
extern const char *const n64c_charged_names[] __attribute__((weak));
extern const unsigned n64c_charged_count __attribute__((weak));

static const void *const no_functions[1];
const void *const *n64c_charged_fns = no_functions;
unsigned n64c_charged_mask;

__attribute__((noreturn)) static void refuse(const char *what)
{
    fprintf(stderr, "boot-missing: guest work charge: %s\n", what);
    fflush(stderr);
    _Exit(7);
}
/* The charged names against the running image's own symbol table (.symtab of /proc/self/exe, which needs no dynamic
 * exports): every function symbol with a charged name goes into the address set. */
static unsigned resolve(const void **set, unsigned mask)
{
    unsigned count = 0;
    int fd = open("/proc/self/exe", O_RDONLY);
    struct stat st;
    if (fd < 0 || fstat(fd, &st) || st.st_size < (off_t)sizeof(ElfW(Ehdr)))
        refuse("cannot read the executable's symbol table");
    const unsigned char *image = mmap(NULL, (size_t)st.st_size, PROT_READ, MAP_PRIVATE, fd, 0);
    close(fd);
    if (image == MAP_FAILED)
        refuse("cannot map the executable's symbol table");
    const ElfW(Ehdr) *eh = (const ElfW(Ehdr) *)image;
    if (memcmp(eh->e_ident, ELFMAG, SELFMAG) || eh->e_shoff + (size_t)eh->e_shnum * sizeof(ElfW(Shdr)) > (size_t)st.st_size)
        refuse("the executable is not an ELF image");
    const ElfW(Shdr) *sh = (const ElfW(Shdr) *)(image + eh->e_shoff);
    unsigned names = 1024;
    while (names < n64c_charged_count * 2)
        names <<= 1;
    unsigned *by_name = calloc(names, sizeof *by_name); /* charged index + 1, open-addressed on the name */
    if (!by_name)
        refuse("out of memory");
    for (unsigned i = 0; i < n64c_charged_count; i++) {
        unsigned h = 2166136261u;
        for (const char *p = n64c_charged_names[i]; *p; p++)
            h = (h ^ (unsigned char)*p) * 16777619u;
        unsigned s = h & (names - 1);
        while (by_name[s])
            s = (s + 1) & (names - 1);
        by_name[s] = i + 1;
    }
    for (unsigned i = 0; i < eh->e_shnum; i++) {
        if (sh[i].sh_type != SHT_SYMTAB || sh[i].sh_link >= eh->e_shnum)
            continue;
        const ElfW(Sym) *syms = (const ElfW(Sym) *)(image + sh[i].sh_offset);
        const char *strings = (const char *)(image + sh[sh[i].sh_link].sh_offset);
        for (unsigned k = 0; k < sh[i].sh_size / sizeof(ElfW(Sym)); k++) {
            if (ELF32_ST_TYPE(syms[k].st_info) != STT_FUNC || !syms[k].st_value)
                continue;
            const char *name = strings + syms[k].st_name;
            unsigned h = 2166136261u;
            for (const char *p = name; *p; p++)
                h = (h ^ (unsigned char)*p) * 16777619u;
            unsigned s = h & (names - 1);
            while (by_name[s] && strcmp(n64c_charged_names[by_name[s] - 1], name))
                s = (s + 1) & (names - 1);
            if (!by_name[s])
                continue;
            const void *fn = (const void *)(uintptr_t)syms[k].st_value;
            unsigned a = (((unsigned)(uintptr_t)fn * 2654435761u) >> 7) & mask;
            while (set[a] && set[a] != fn)
                a = (a + 1) & mask;
            count += !set[a];
            set[a] = fn;
        }
    }
    free(by_name);
    munmap((void *)image, (size_t)st.st_size);
    return count;
}
unsigned long long n64c_charge_init(void)
{
    if (!n64c_guest_ns_per_entry || !*n64c_guest_ns_per_entry)
        refuse("the project declares no work scale (n64c_guest_ns_per_entry)");
    unsigned long long fx = n64c_clock_parse_work(n64c_guest_ns_per_entry);
    if (!fx)
        refuse("the declared work scale is not a positive decimal number of ns per entry");
    if (!n64c_charged_names || !n64c_charged_count)
        refuse("the project declares no charged functions");
    if (n64c_charged_fns == no_functions) { /* once: the table is the same for every reset */
        unsigned size = 1024;
        while (size < n64c_charged_count * 2)
            size <<= 1;
        const void **set = calloc(size, sizeof *set);
        if (!set)
            refuse("out of memory");
        if (!resolve(set, size - 1))
            refuse("no charged function is in the executable");
        n64c_charged_mask = size - 1;
        n64c_charged_fns = set;
    }
    return fx;
}
/* The compiler's entry and exit calls of the original units. A project that traces (boot/trace.c) defines its own
 * pair, which calls n64c_charge_entry itself. */
__attribute__((weak, no_instrument_function)) void __cyg_profile_func_enter(void *fn, void *call_site)
{
    (void)call_site;
    n64c_charge_entry(fn);
}
__attribute__((weak, no_instrument_function)) void __cyg_profile_func_exit(void *fn, void *call_site)
{
    (void)fn;
    (void)call_site;
}
