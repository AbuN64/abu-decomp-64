/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Behavioral contracts (no SDK implementation copied):
 * ultra64.ca/files/documentation/online-manuals/man-v5-2/allman52/n64man/os/osInitialize.htm
 * ultra64.ca/files/documentation/online-manuals/functions_reference_manual_2.0i/os/osGlobals.html
 * Native hardware services are already installed by n64c_init/memory_open.
 * This adapter owns observable globals and the calling thread's initial FPU mode. */
#define _GNU_SOURCE
#include "n64c_system.h"
#include <fenv.h>
#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#if defined(__linux__) && (defined(__i386__) || defined(__x86_64__))
#include <xmmintrin.h>
#define NATIVE_FPU 1
#else
#define NATIVE_FPU 0
#endif
static N64cSystemGlobals state;
static fenv_t host_environment;
static int environment_saved;

void n64c_system_reset(void)
{
    if (environment_saved) (void)fesetenv(&host_environment);
    environment_saved=0;
    memset(&state,0,sizeof state);
}
int n64c_system_bind(const N64cSystemGlobals *globals,u32 tv_type,u32 reset_type,u32 memory_size)
{
    if (!globals || state.initialized || !globals->initialized || !globals->tv_type ||
        !globals->reset_type || !globals->memory_size || !globals->video_clock ||
        !globals->clock_rate || !globals->nmi_buffer || tv_type>2 || reset_type>1 ||
        (memory_size!=0x400000u && memory_size!=0x800000u)) return 0;
    state=*globals;
    *state.tv_type=tv_type;
    *state.reset_type=reset_type;
    *state.memory_size=memory_size;
    return 1;
}
static _Noreturn void stop(const char *reason)
{
    fprintf(stderr,"boot-missing: system-%s\n",reason);
    fflush(stderr);
    _Exit(78);
}
/* Register contract:
 * ultra64.ca/files/documentation/online-manuals/man-v5-2/allman52/n64man/os/osGetFpcCsr.htm
 * arch/mips/include/asm/mipsregs.h: FPU_CSR_RM/ALL_E/FS.
 * Host FPU state is the only store; pthread context switches preserve it.
 * This is intentionally not an emulation of MIPS instruction status/condition. */
#if NATIVE_FPU
static const int rounding[] = {FE_TONEAREST, FE_TOWARDZERO, FE_UPWARD, FE_DOWNWARD};
static const int exceptions[] = {FE_INEXACT, FE_UNDERFLOW, FE_OVERFLOW, FE_DIVBYZERO, FE_INVALID};
#endif

u32 n64c_fpu_get_control(void)
{
#if NATIVE_FPU
    if (fetestexcept(FE_ALL_EXCEPT)) stop("FPU-status");
    u32 result=0;
    int mode=fegetround(), enabled=fegetexcept();
    unsigned i;
    for (i=0;i<4 && rounding[i]!=mode;i++) {}
    if (i==4 || enabled<0) stop("FPU-control");
    result=i;
    for (i=0;i<5;i++) if (enabled & exceptions[i]) result |= 1u<<(7+i);
    unsigned csr=_mm_getcsr();
    /* Original units use SSE arithmetic. Reject disagreement with x87 control
     * rather than silently reporting whichever control register was read first. */
    static const unsigned sse_rounding[]={0,3,2,1};
    if (((csr>>13)&3u)!=sse_rounding[result&3u])
        stop("FPU-rounding-mismatch");
    static const unsigned sse_exception_bits[]={5,4,3,2,0};
    for (i=0;i<5;i++)
        if (((csr>>(7+sse_exception_bits[i]))&1u)==!!(enabled & exceptions[i]))
            stop("FPU-trap-mismatch");
    unsigned flush=csr & ((1u<<15)|(1u<<6));
    if (flush && flush!=((1u<<15)|(1u<<6))) stop("FPU-flush-mismatch");
    if (flush) result |= 1u<<24;
    return result;
#else
    stop("FPU-platform");
#endif
}

u32 n64c_fpu_set_control(u32 control)
{
#if NATIVE_FPU
    if (control & ~0x01000f83u) stop("FPU-unsupported-bits");
    u32 previous=n64c_fpu_get_control();
    int enabled=0;
    for (unsigned i=0;i<5;i++) if (control & (1u<<(7+i))) enabled |= exceptions[i];
    if (fedisableexcept(FE_ALL_EXCEPT)<0 || fesetround(rounding[control&3u])!=0 ||
        (enabled && feenableexcept(enabled)<0)) stop("FPU-control");
    unsigned csr=_mm_getcsr() & ~((1u<<15)|(1u<<6));
    if (control & (1u<<24)) csr |= (1u<<15)|(1u<<6);
    _mm_setcsr(csr);
    return previous;
#else
    (void)control;
    stop("FPU-platform");
#endif
}

/* Each new guest thread starts with the documented OS FPU defaults,
 * independently of its creator's current controls or pending IEEE flags.
 * This touches only the calling host thread, not the saved host environment. */
void n64c_fpu_thread_start(void)
{
#if NATIVE_FPU
    if (fedisableexcept(FE_ALL_EXCEPT)<0 || feclearexcept(FE_ALL_EXCEPT)!=0 ||
        fesetround(FE_TONEAREST)!=0) stop("FPU-control");
    /* Normalize the host flush pair before importing the initial OS mode. */
    _mm_setcsr(_mm_getcsr() & ~((1u<<15)|(1u<<6)));
    (void)n64c_fpu_set_control(0x01000800u);
#else
    stop("FPU-platform");
#endif
}

void n64c_system_initialize(void)
{
    if (!state.initialized) stop("globals");
    u32 clock;
    if (osPiReadIo(4,&clock)!=0) stop("ROM-clock");
    clock &= ~15u;
    if (!clock) clock=62500000u;
    /* The scheduler's virtual ticks are CPU-counter ticks. Refuse a different
     * rate until that clock contract can represent it consistently. */
    u64 counter=(u64)clock*3u/4u;
    if (counter!=OS_CPU_COUNTER) stop("counter-rate");
    if (*state.tv_type>2 || *state.reset_type>1) stop("boot-values");
#if NATIVE_FPU
    if (!environment_saved) {
        if (fegetenv(&host_environment)!=0) stop("FPU-save");
        environment_saved=1;
    }
    n64c_fpu_thread_start();
#else
    stop("FPU-platform");
#endif
    static const u32 video_clocks[]={49656530u,48681812u,48628316u};
    *state.clock_rate=counter;
    *state.video_clock=video_clocks[*state.tv_type];
    if (!*state.reset_type) memset(state.nmi_buffer,0,64);
    *state.initialized=1;
}

/* ISO C sprintf contract, implemented by the host C library. The boot is i386,
 * so the caller and host agree on pointer, long and varargs widths. */
s32 n64c_sprintf(char *buffer, const char *format, ...)
{
    va_list args;
    va_start(args, format);
    int count = vsprintf(buffer, format, args);
    va_end(args);
    return count;
}

/* Rounds to nearest, as the host conversion does. */
f64 n64c_floatdidf(s64 value) { return (f64)value; }
void *n64c_copy_bytes(void *destination, const void *source, s32 count)
{
    u8 *out = destination;
    const u8 *in = source;
    for (s32 left = count; left != 0; left--) *out++ = *in++;
    return destination;
}
s32 n64c_string_length(const u8 *string)
{
    const u8 *end = string;
    while (*end) end++;
    return (s32)(end - string);
}
static void *dev_handler;
void n64c_dev_handler_set(void *handler) { dev_handler = handler; }
void *n64c_dev_handler(void) { return dev_handler; }
