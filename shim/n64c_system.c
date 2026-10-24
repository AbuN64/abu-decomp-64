/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Behavioral contracts (no SDK implementation copied):
 * ultra64.ca/files/documentation/online-manuals/man-v5-2/allman52/n64man/os/osInitialize.htm
 * ultra64.ca/files/documentation/online-manuals/functions_reference_manual_2.0i/os/osGlobals.html
 * Native hardware services are already installed by n64c_init/memory_open.
 * This adapter owns observable globals and the calling thread's initial FPU mode. */
#define _GNU_SOURCE
#include "n64c_system.h"
#include "n64c_diag.h"
#include <fenv.h>
#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#if (defined(__linux__) && (defined(__i386__) || defined(__x86_64__))) || (defined(_WIN32) && defined(__i386__))
#include <xmmintrin.h>
#define NATIVE_FPU 1
#define NATIVE_SSE 1
#elif defined(__linux__) && defined(__arm__)
/* ARM VFP: the rounding mode and flags are the host's; trapping exceptions are not an ARM feature, so a guest that
 * enables them stops by name. No SSE control register to cross-check. */
#define NATIVE_FPU 1
#define NATIVE_SSE 0
#else
#define NATIVE_FPU 0
#define NATIVE_SSE 0
#endif
#if NATIVE_FPU && defined(_WIN32)
/* fegetexcept, feenableexcept and fedisableexcept are glibc's. The trap masks sit in the x87 control word (bits 0..5, a set
 * bit masks) and in MXCSR (bits 7..12), both in the FE_ bit order, and are kept equal. */
static int fegetexcept(void)
{
    unsigned short word;
    __asm__ volatile("fnstcw %0" : "=m"(word));
    return ~word & FE_ALL_EXCEPT;
}
static void set_masks(int excepts, int masked)
{
    unsigned short word;
    excepts &= FE_ALL_EXCEPT;
    __asm__ volatile("fnstcw %0" : "=m"(word));
    word = (unsigned short)(masked ? word | excepts : word & ~excepts);
    __asm__ volatile("fldcw %0" : : "m"(word));
    unsigned csr = _mm_getcsr();
    csr = masked ? csr | ((unsigned)excepts << 7) : csr & ~((unsigned)excepts << 7);
    _mm_setcsr(csr);
}
static int feenableexcept(int excepts) { const int previous = fegetexcept(); set_masks(excepts, 0); return previous; }
static int fedisableexcept(int excepts) { const int previous = fegetexcept(); set_masks(excepts, 1); return previous; }
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
    char subject[64];
    snprintf(subject,sizeof subject,"system-%s",reason);
    if (n64c_diag_stop) (void)n64c_diag_stop(subject,0); /* run-on: recorded; no way on */
    fprintf(stderr,"boot-missing: %s\n",subject);
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
#if !NATIVE_SSE
    if (enabled<0) enabled=0; /* no trap control on this CPU: nothing is enabled */
#endif
    unsigned i;
    for (i=0;i<4 && rounding[i]!=mode;i++) {}
    if (i==4 || enabled<0) stop("FPU-control");
    result=i;
    for (i=0;i<5;i++) if (enabled & exceptions[i]) result |= 1u<<(7+i);
#if NATIVE_SSE
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
#endif
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
#if NATIVE_SSE
    unsigned csr=_mm_getcsr() & ~((1u<<15)|(1u<<6));
    if (control & (1u<<24)) csr |= (1u<<15)|(1u<<6);
    _mm_setcsr(csr);
#else
    if (control & (1u<<24)) stop("FPU-flush"); /* flush-to-zero has no host mapping here */
#endif
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
#if NATIVE_SSE
    /* Normalize the host flush pair before importing the initial OS mode. */
    _mm_setcsr(_mm_getcsr() & ~((1u<<15)|(1u<<6)));
#endif
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
/* The callback form of the same formatting contract (ISO C vsnprintf): the text is formatted once and handed to
 * the emit callback in one span; the callback returns the next output position, NULL meaning it refused the
 * span. Result: characters formatted, or -1 when the callback refused. */
s32 n64c_printf(void *(*emit)(void *, const void *, s32), void *target, const char *format, va_list args)
{
    char small[512];
    char *text = small;
    va_list again;
    va_copy(again, args);
    int count = vsnprintf(small, sizeof small, format, again);
    va_end(again);
    if (count >= (int)sizeof small) {
        text = malloc((size_t)count + 1);
        if (!text) return -1;
        vsnprintf(text, (size_t)count + 1, format, args);
    }
    if (count > 0 && !emit(target, text, count)) count = -1;
    if (text != small) free(text);
    return count;
}
/* The emit callback sprintf-style callers pass: copy the span, return the position after it. */
void *n64c_printf_copy(void *destination, const void *source, s32 count)
{
    return (char *)n64c_copy_bytes(destination, source, count) + count;
}
/* First byte equal to value in a zero-terminated string; the terminator itself when value is 0; NULL otherwise. */
u8 *n64c_string_find(u8 *string, u8 value)
{
    while (*string != value) {
        if (!*string) return NULL;
        string++;
    }
    return string;
}
static void *dev_handler;
void n64c_dev_handler_set(void *handler) { dev_handler = handler; }
void *n64c_dev_handler(void) { return dev_handler; }

/* The guest runs with trap-enabled floating point (osSetFpcCsr). Code of the host (the renderer, a graphics driver, the audio
 * backend) computes with values that set invalid-operation flags harmlessly: it runs with the traps masked. Linux and the
 * Windows build mask them only where a host library is called; no other platform needs it. */
#if NATIVE_FPU && defined(_WIN32)
/* The saved state is the low 16 bits of MXCSR and the x87 control word, packed: restore puts both back exactly. */
int n64c_fp_traps_mask(void)
{
    unsigned short word;
    __asm__ volatile("fnstcw %0" : "=m"(word));
    const unsigned csr=_mm_getcsr();
    if ((csr & (0x3Fu<<7))!=(0x3Fu<<7) || (word & 0x3Fu)!=0x3Fu) {
        __asm__ volatile("fnclex");
        _mm_setcsr((csr | (0x3Fu<<7)) & ~0x3Fu);
        const unsigned short masked=(unsigned short)(word | 0x3Fu);
        __asm__ volatile("fldcw %0" : : "m"(masked));
    }
    return (int)((csr & 0xFFFFu) | ((unsigned)word<<16));
}
void n64c_fp_traps_restore(int saved)
{
    const unsigned csr=_mm_getcsr();
    const unsigned short word=(unsigned short)((unsigned)saved>>16);
    __asm__ volatile("fnclex");
    _mm_setcsr((csr & ~0xFFFFu) | ((unsigned)saved & 0xFFC0u)); /* control bits back, the flags raised meanwhile cleared */
    __asm__ volatile("fldcw %0" : : "m"(word));
}
#elif NATIVE_FPU && defined(__ANDROID__)
int n64c_fp_traps_mask(void)
{
    const int enabled=fegetexcept();
    if (enabled>0) (void)fedisableexcept(FE_ALL_EXCEPT);
    return enabled;
}
void n64c_fp_traps_restore(int enabled)
{
    if (enabled>0) {
        (void)feclearexcept(FE_ALL_EXCEPT);
        (void)feenableexcept(enabled);
    }
}
#else
int n64c_fp_traps_mask(void) { return 0; }
void n64c_fp_traps_restore(int enabled) { (void)enabled; }
#endif
