/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Windows owns the size/alignment of an exception context, including XState.
 * The generic seam stores only the fixed register record; this owner retains
 * the complete context until the serviced fault resumes. Include windows.h first. */
#ifndef N64C_WIN_CONTEXT_H
#define N64C_WIN_CONTEXT_H
typedef struct {
    void *buffer;
    DWORD bytes;
    PCONTEXT context;
} N64cWinContext;

static inline void n64c_win_context_destroy(N64cWinContext *state)
{
    if (state->buffer) HeapFree(GetProcessHeap(),0,state->buffer);
    *state=(N64cWinContext){0};
}

static inline int n64c_win_context_save(N64cWinContext *state,PCONTEXT source)
{
    DWORD bytes=0;
    if (InitializeContext(NULL,source->ContextFlags,NULL,&bytes) ||
        GetLastError()!=ERROR_INSUFFICIENT_BUFFER || !bytes) return 0;
    if (bytes>state->bytes) {
        void *buffer=HeapAlloc(GetProcessHeap(),0,bytes);
        if (!buffer) { SetLastError(ERROR_NOT_ENOUGH_MEMORY);return 0; }
        n64c_win_context_destroy(state);
        state->buffer=buffer;
        state->bytes=bytes;
    }
    bytes=state->bytes;
    state->context=NULL;
    if (!InitializeContext(state->buffer,source->ContextFlags,&state->context,&bytes)) return 0;
    return CopyContext(state->context,source->ContextFlags,source)!=0;
}

static inline int n64c_win_context_merge(N64cWinContext *state,PCONTEXT registers)
{
    /* MMIO emulation changes general registers/PC. Copy those fixed fields back
     * without discarding the XState captured before dispatch. */
    return state->context && CopyContext(state->context,registers->ContextFlags & CONTEXT_ALL,registers);
}
#endif
