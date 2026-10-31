/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The guest's virtual ranges on Windows (the TLB zones the shim models), kept as placeholders. The process start reserves every
 * 64 KiB chunk of a zone as a placeholder before any DLL or heap can take it (adopted by the process the loader runs), and a
 * mapping replaces the placeholders it covers in one call: a view of the section for a 64 KiB aligned TLB page, a private
 * reservation for the 64 KiB block that holds 4 KiB pages. Giving a view or block back turns the range into placeholders again,
 * so a range is never unowned between two mappings. The Win32 calls are the ops table (os_win32.c supplies the real ones, the
 * unit test a model of their rules), so the sequence is checked without Windows. Free of OS headers. */
#ifndef N64C_WIN_SPACE_H
#define N64C_WIN_SPACE_H
#include <stddef.h>
#include <stdint.h>

enum { N64C_SPACE_GRANULE=65536, N64C_SPACE_ZONES=2, N64C_SPACE_CHUNKS=48 /* 1 MiB at 0x00400000 and 2 MiB at 0x00800000 */ };

/* What VirtualQuery says about the address: the region it lies in and its owner. `module` names the image or heap, or is empty. */
typedef struct {
    uintptr_t base, allocation_base;
    size_t size;
    unsigned state, type;     /* the Windows MEM_ values */
    char module[260];
} N64cSpaceRegion;

typedef struct {
    void *context;            /* the process the calls address */
    int (*reserve)(void *context, uintptr_t address, size_t bytes);      /* a new placeholder; 1 on success (all calls) */
    int (*release)(void *context, uintptr_t address, size_t bytes);      /* a view or private allocation becomes a placeholder; a range inside one placeholder is split off */
    int (*coalesce)(void *context, uintptr_t address, size_t bytes);     /* the placeholders that exactly fill the range become one */
    int (*map)(void *context, uintptr_t address, size_t bytes, uint32_t physical);  /* a view of the store replaces the placeholder */
    int (*unmap)(void *context, uintptr_t address, size_t bytes);        /* the view becomes one placeholder again */
    int (*alloc)(void *context, uintptr_t address, size_t bytes);        /* a private reservation replaces the placeholder */
    int (*query)(void *context, uintptr_t address, N64cSpaceRegion *region);
    void (*log)(void *context, const char *line);
} N64cSpaceOps;

typedef struct {
    N64cSpaceOps ops;
    unsigned char chunk[N64C_SPACE_CHUNKS];   /* 0 not held, else what the chunk is now */
} N64cSpace;

/* Reserve every chunk of the zones that is free as a placeholder, through ops (which may address another process). Returns the
 * number of chunks that could not be reserved, each reported with what occupies it. */
unsigned n64c_space_reserve(const N64cSpaceOps *ops);
/* Hold the chunks that are reserved placeholders in this process (the reservation the parent made). Returns the number of
 * chunks that are not, each reported. */
unsigned n64c_space_adopt(N64cSpace *space, const N64cSpaceOps *ops);

/* A view of the store at [address, address+bytes), 64 KiB aligned. 1 mapped; 0 when no chunk of the range is held (the caller
 * maps it directly); -1 when a held range could not be mapped (reported). */
int n64c_space_view(N64cSpace *space, uintptr_t address, size_t bytes, uint32_t physical);
/* End the view that starts at address. 1 when the range is placeholders again; 0 when it was not a held view. */
int n64c_space_view_end(N64cSpace *space, uintptr_t address, size_t bytes);
/* The 64 KiB block that holds address becomes a private reservation. 1 ready (also when it already is); 0 when the chunk is not
 * held; -1 when it could not be taken (reported). */
int n64c_space_block(N64cSpace *space, uintptr_t address);
/* Give the block back as a placeholder. 1 done; 0 when it was not a held block. */
int n64c_space_block_end(N64cSpace *space, uintptr_t address);
/* Say on ops->log what occupies [address, address+bytes): base, size, state, type, allocation base and owner of each region. */
void n64c_space_report(const N64cSpaceOps *ops, uintptr_t address, size_t bytes, const char *what);

#endif
