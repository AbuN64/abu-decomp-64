/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Audio library heap and list helpers. Public behaviour only: a bump allocator handing out zeroed,
 * 16-byte aligned blocks, and an intrusive doubly linked list. */
#include "n64c_al.h"
#include <string.h>

void alHeapInit(ALHeap *hp, u8 *base, s32 len)
{
    hp->base = base;
    hp->cur = base;
    hp->len = len;
    hp->count = 0;
}

void *alHeapDBAlloc(u8 *file, s32 line, ALHeap *hp, s32 num, s32 size)
{
    (void)file;
    (void)line;
    if (!hp || !hp->base || num <= 0 || size <= 0) return NULL;
    uintptr_t start = ((uintptr_t)hp->cur + 15u) & ~(uintptr_t)15u;
    uint64_t bytes = ((uint64_t)(u32)num * (u32)size + 15u) & ~(uint64_t)15u;
    uintptr_t end = (uintptr_t)hp->base + (u32)hp->len;
    if (start > end || bytes > end - start) return NULL;
    hp->cur = (u8 *)(start + (uintptr_t)bytes);
    hp->count++;
    memset((void *)start, 0, (size_t)bytes);
    return (void *)start;
}

void alLink(ALLink *ln, ALLink *to)
{
    ln->next = to->next;
    ln->prev = to;
    if (to->next) to->next->prev = ln;
    to->next = ln;
}

void alUnlink(ALLink *ln)
{
    if (ln->next) ln->next->prev = ln->prev;
    if (ln->prev) ln->prev->next = ln->next;
}
