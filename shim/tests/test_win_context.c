/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Mock the variable-size Windows context API; no Windows installation needed. */
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef unsigned long DWORD;
typedef struct { DWORD ContextFlags, reg; } CONTEXT, *PCONTEXT;
enum { CONTEXT_ALL=0x1003f, XSTATE=0x40, ERROR_INSUFFICIENT_BUFFER=122,
       ERROR_NOT_ENOUGH_MEMORY=8 };
static DWORD error, required=192;
static int query_error, allocation_error, init_error, copy_error, live, allocations;
static DWORD GetLastError(void) { return error; }
static void SetLastError(DWORD value) { error=value; }
static void *GetProcessHeap(void) { return NULL; }
static void *HeapAlloc(void *heap,DWORD flags,size_t bytes)
{
    (void)heap;(void)flags;
    if (allocation_error) return NULL;
    void *p=malloc(bytes);
    if (p) { live++;allocations++; }
    return p;
}
static int HeapFree(void *heap,DWORD flags,void *p)
{
    (void)heap;(void)flags;
    free(p);live--;return 1;
}
static int InitializeContext(void *buffer,DWORD flags,PCONTEXT *out,DWORD *bytes)
{
    if (!buffer) {
        error=query_error ? 87 : ERROR_INSUFFICIENT_BUFFER;
        *bytes=required;return 0;
    }
    if (init_error) { error=87;return 0; }
    assert(*bytes>=required);
    /* Deliberately return an interior address, as the Windows API does. */
    *out=(PCONTEXT)((unsigned char *)buffer+32);
    (*out)->ContextFlags=flags;
    return 1;
}
static int CopyContext(PCONTEXT dest,DWORD flags,PCONTEXT src)
{
    if (copy_error) { error=87;return 0; }
    dest->reg=src->reg;
    if (flags & XSTATE) memcpy(dest+1,src+1,64);
    return 1;
}
#include "../os/win_context.h"
int main(void)
{
    struct { CONTEXT context; unsigned char xstate[64]; } src;
    src.context=(CONTEXT){CONTEXT_ALL|XSTATE,73};
    memset(src.xstate,0xa5,sizeof src.xstate);
    N64cWinContext state={0};
    assert(n64c_win_context_save(&state,&src.context));
    assert(state.context!=(PCONTEXT)state.buffer && state.context->reg==73);
    assert(!memcmp(state.context+1,src.xstate,64) && live==1);
    CONTEXT changed={CONTEXT_ALL|XSTATE,91};
    assert(n64c_win_context_merge(&state,&changed));
    assert(state.context->reg==91 && !memcmp(state.context+1,src.xstate,64));
    assert(n64c_win_context_save(&state,&src.context) && allocations==1);
    required=384;
    allocation_error=1;
    assert(!n64c_win_context_save(&state,&src.context));
    assert(error==ERROR_NOT_ENOUGH_MEMORY && state.bytes==192 && live==1);
    allocation_error=0;
    assert(n64c_win_context_save(&state,&src.context) && state.bytes==384 && live==1);
    query_error=1;
    assert(!n64c_win_context_save(&state,&src.context) && error==87);
    query_error=0;init_error=1;
    assert(!n64c_win_context_save(&state,&src.context) && !state.context);
    init_error=0;copy_error=1;
    assert(!n64c_win_context_save(&state,&src.context));
    assert(!n64c_win_context_merge(&state,&changed));
    copy_error=0;
    n64c_win_context_destroy(&state);
    assert(!state.buffer && !state.context && !state.bytes && live==0);
    assert(!n64c_win_context_merge(&state,&changed));
    puts("win_context: complete XState, register merge, reuse, resize and failures passed");
    return 0;
}
