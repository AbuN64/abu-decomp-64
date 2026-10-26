/* SPDX-License-Identifier: GPL-3.0-or-later */
/* n64compat OS surface: hand-written from public libultra documentation
 * (function names, argument lists and structure roles; no vendor headers). Pointers are native, so a
 * -m32 build keeps the original structure sizes. Only the platform/n64compat directory may include this. */
#ifndef N64C_OS_H
#define N64C_OS_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
typedef int8_t s8;
typedef int16_t s16;
typedef int32_t s32;
typedef int64_t s64;
typedef float f32;
typedef double f64;
typedef u64 OSTime;
typedef u32 OSIntMask;
/* ABI values: CPU SR interrupt bits plus the six MI enables in bits 16..21. */
#define OS_IM_NONE 0x00000001u
#define OS_IM_SW1 0x00000501u
#define OS_IM_SW2 0x00000601u
#define OS_IM_CART 0x00000C01u
#define OS_IM_PRENMI 0x00001401u
#define OS_IM_COUNTER 0x00008401u
#define OS_IM_SP 0x00010401u
#define OS_IM_SI 0x00020401u
#define OS_IM_AI 0x00040401u
#define OS_IM_VI 0x00080401u
#define OS_IM_PI 0x00100401u
#define OS_IM_DP 0x00200401u
#define OS_IM_ALL 0x003FFF01u
typedef s32 OSPri;
typedef s32 OSId;
#define OS_CPU_COUNTER 46875000u
#define OS_PRIORITY_MAX 255
#define OS_PRIORITY_VIMGR 254
#define OS_PRIORITY_PIMGR 150
#define OS_PRIORITY_SIMGR 140
#define OS_PRIORITY_APPMAX 127
#define OS_PRIORITY_IDLE 0
#define OS_STATE_STOPPED 1
#define OS_STATE_RUNNABLE 2
#define OS_STATE_RUNNING 4
#define OS_STATE_WAITING 8
#define OS_MESG_NOBLOCK 0
#define OS_MESG_BLOCK 1
typedef union {
    u8 data8;
    u16 data16;
    u32 data32;
    void *ptr;
} OSMesg;
typedef struct OSThread {
    struct OSThread *next;     /* queue link */
    OSPri priority;
    struct OSThread **queue;   /* queue head this thread is on */
    struct OSThread *tlnext;
    u16 state;
    u16 flags;
    OSId id;
    s32 fp;
    void *thprof;
    u8 context[0x190];         /* opaque; the first word holds the host thread's slot + 1 */
} OSThread;
typedef struct OSMesgQueue {
    OSThread *mtqueue;   /* threads blocked on receive */
    OSThread *fullqueue; /* threads blocked on send */
    s32 validCount;
    s32 first;
    s32 msgCount;
    OSMesg *msg;
} OSMesgQueue;
typedef struct OSTimer {
    struct OSTimer *next, *prev;
    OSTime interval, value;
    OSMesgQueue *mq;
    OSMesg msg;
} OSTimer;
/* events usable with osSetEventMesg */
enum { OS_EVENT_SW1, OS_EVENT_SW2, OS_EVENT_CART, OS_EVENT_COUNTER, OS_EVENT_SP, OS_EVENT_SI, OS_EVENT_AI,
       OS_EVENT_VI, OS_EVENT_PI, OS_EVENT_DP, OS_EVENT_CPU_BREAK, OS_EVENT_SP_BREAK,
       OS_EVENT_FAULT, OS_EVENT_THREADSTATUS, OS_EVENT_PRENMI, OS_NUM_EVENTS };
/* scheduler / mesg (n64c_sched.c) */
void osCreateMesgQueue(OSMesgQueue *mq, OSMesg *buf, s32 count);
s32 osSendMesg(OSMesgQueue *mq, OSMesg msg, s32 flag);
s32 osJamMesg(OSMesgQueue *mq, OSMesg msg, s32 flag);
s32 osRecvMesg(OSMesgQueue *mq, OSMesg *msg, s32 flag);
void osSetEventMesg(u32 event, OSMesgQueue *mq, OSMesg msg);
void osCreateThread(OSThread *t, OSId id, void (*entry)(void *), void *arg, void *sp, OSPri pri);
void osStartThread(OSThread *t);
void osDestroyThread(OSThread *t);
void osSetThreadPri(OSThread *t, OSPri pri);
OSPri osGetThreadPri(OSThread *t);
OSId osGetThreadId(OSThread *t);
void osYieldThread(void);
void osSetTimer(OSTimer *t, OSTime countdown, OSTime interval, OSMesgQueue *mq, OSMesg msg);
s32 osStopTimer(OSTimer *t);
OSTime osGetTime(void);
u32 osGetCount(void);
void __osSetCompare(u32 value);
/* Native scheduler extension: current guest identity, NULL on the host. This is not a public libultra name. */
OSThread *n64c_running_thread(void);
/* The thread the last CPU fault stopped (public libultra contract): NULL before any fault. */
OSThread *osGetCurrFaultedThread(void);
OSIntMask osSetIntMask(OSIntMask mask);
/* Interrupt critical sections; cache operations need no host emulation. */
u32 __osDisableInt(void);
void __osRestoreInt(u32 mask);
void osWritebackDCache(void *p, size_t n);
void osWritebackDCacheAll(void);
void osInvalDCache(void *p, size_t n);
void osInvalICache(void *p, size_t n);
void osMapTLBRdb(void);
void osUnmapTLB(s32 index);
void osUnmapTLBAll(void);
/* One TLB entry: the even/odd page pair of size (pm|0x1FFF)+1 halves at vaddr onto RDRAM (paddr's low
 * 29 bits; -1 leaves a page invalid). The native window is a shared mapping of the same RDRAM pages. */
void osMapTLB(s32 index, u32 pm, void *vaddr, u32 evenpaddr, u32 oddpaddr, s32 asid);
u32 osVirtualToPhysical(void *p);
/* Native extension for the TLB probe: physical address or UINT32_MAX if unmapped. */
u32 n64c_probe_tlb(const void *pointer);
/* Packed 4x4 signed 16.16 matrix, in host word order (64 bytes). */
void guMtxIdent(void *matrix);
/* VI (n64c_vi.c) */
#define OS_VI_NTSC_HZ_NUM 60000u /* 60000/1001 retraces per second */
#define OS_VI_NTSC_HZ_DEN 1001u
void osCreateViManager(OSPri priority);
void osViSetEvent(OSMesgQueue *mq, OSMesg msg, u32 retraceCount);
void osViSwapBuffer(void *frameBuf);
void osViBlack(u8 active);
void osViSetYScale(f32 y);
void *osViGetCurrentFramebuffer(void);
void *osViGetNextFramebuffer(void);
u32 osViGetRetraceCount(void);
/* VI mode: the public register-set layout (one common set, one per field). */
typedef struct {
    u32 ctrl, width, burst, vSync, hSync, leap, hStart, xScale, vCurrent;
} OSViCommonRegs;
typedef struct {
    u32 origin, yScale, vStart, vBurst, vIntr;
} OSViFieldRegs;
typedef struct {
    u8 type;
    OSViCommonRegs comRegs;
    OSViFieldRegs fldRegs[2];
} OSViMode;
void osViSetMode(OSViMode *mode);
/* Special features: each ON/OFF pair sets/clears one VI control bit. */
#define OS_VI_GAMMA_ON 0x0001u
#define OS_VI_GAMMA_OFF 0x0002u
#define OS_VI_GAMMA_DITHER_ON 0x0004u
#define OS_VI_GAMMA_DITHER_OFF 0x0008u
#define OS_VI_DIVOT_ON 0x0010u
#define OS_VI_DIVOT_OFF 0x0020u
#define OS_VI_DITHER_FILTER_ON 0x0040u
#define OS_VI_DITHER_FILTER_OFF 0x0080u
/* VI control register bits the features drive. */
#define VI_CTRL_GAMMA_DITHER_ON 0x00004u
#define VI_CTRL_GAMMA_ON 0x00008u
#define VI_CTRL_DIVOT_ON 0x00010u
#define VI_CTRL_DITHER_FILTER_ON 0x10000u
#define VI_CTRL_ANTIALIAS_MASK 0x00300u
void osViSetSpecialFeatures(u32 func);
/* Native extensions for presentation: the mode last set (NULL before any) and the VI control word. */
const OSViMode *n64c_vi_mode(void);
u32 n64c_vi_control(void);
/* Rows of the colour image the VI shows: field 0's line window (vStart: first and last half-line) times its y
 * scale (2.10, times osViSetYScale), rounded up; 0 before any mode. */
u32 n64c_vi_rows(void);
/* AI: word-swapped stereo S16 buffer copied synchronously to the host sink.
 * Rates are host sample rates (no AI divider quantization); AI event follows the copy. */
s32 osAiSetFrequency(u32 frequency);
s32 osAiSetNextBuffer(void *buffer, u32 bytes);
u32 osAiGetLength(void);
/* AI status register: the DMA FIFO holds the playing buffer and one queued buffer. A buffer stays in the FIFO
 * until its frames have played on the virtual clock (osGetTime) at the current rate; a third submission while
 * both are held is refused (-1), as the hardware refuses it. */
#define AI_STATUS_FIFO_FULL 0x80000000u
#define AI_STATUS_DMA_BUSY 0x40000000u
u32 osAiGetStatus(void);
/* PI DMA from the ROM file (n64c_pi.c) */
typedef struct OSIoMesg {
    u16 type;
    u8 pri;
    u8 status;
    OSMesgQueue *retQueue;
} OSIoMesg;
#define OS_READ 0
#define OS_WRITE 1
s32 osPiStartDma(OSIoMesg *io, s32 pri, s32 dir, u32 devAddr, void *dram, size_t n, OSMesgQueue *mq);
/* Register-level PI read: done on return; its completion interrupt posts OS_EVENT_PI. */
s32 osPiRawStartDma(s32 dir, u32 devAddr, void *dram, u32 n);
s32 osPiReadIo(u32 devAddr, u32 *data);
/* controller / EEPROM / Pak (n64c_cont.c) */
#define MAXCONTROLLERS 4
#define CONT_ABSOLUTE 0x0001
#define CONT_ERR_NO_CONTROLLER 0x08
#define CONT_CARD_ON 0x01
typedef struct OSContStatus {
    u16 type;
    u8 status;
    u8 errno_;
} OSContStatus;
typedef struct OSContPad {
    u16 button;
    s8 stick_x, stick_y;
    u8 errno_;
} OSContPad;
s32 osContInit(OSMesgQueue *mq, u8 *bitpattern, OSContStatus *status);
s32 osContStartQuery(OSMesgQueue *mq);
void osContGetQuery(OSContStatus *status);
s32 osContStartReadData(OSMesgQueue *mq);
void osContGetReadData(OSContPad *pad);
s32 osEepromProbe(OSMesgQueue *mq);
s32 osEepromLongRead(OSMesgQueue *mq, u8 block, u8 *buf, int n);
s32 osEepromLongWrite(OSMesgQueue *mq, u8 block, u8 *buf, int n);
s32 osContRamRead(OSMesgQueue *mq, int channel, u16 address, u8 *buffer);
s32 osContRamWrite(OSMesgQueue *mq, int channel, u16 address, u8 *buffer);
/* RSP task dispatch (n64c_glue.c) */
typedef struct OSTask {
    u32 type, flags;
    u64 *ucode_boot;
    u32 ucode_boot_size;
    u64 *ucode;
    u32 ucode_size;
    u64 *ucode_data;
    u32 ucode_data_size;
    u64 *dram_stack;
    u32 dram_stack_size;
    u64 *output_buff, *output_buff_size;
    u64 *data_ptr;
    u32 data_size;
    u64 *yield_data_ptr;
    u32 yield_data_size;
} OSTask;
#define M_GFXTASK 1
#define M_AUDTASK 2
/* RDP command buffer handoff. The native RSP renders a graphics task's RDP work inside osSpTaskStartGo, so the
 * only buffer the RDP can be given is the output buffer of the last graphics task the native RSP ran: its work is
 * done and DP completion was already posted. Returns 0 for that buffer. Any other buffer would need an RDP
 * command interpreter the port does not have: the call exits 78 naming it, never a silent success. */
s32 osDpSetNextBuffer(void *buffer, u64 size);
/* Task scheduler (n64c_scsched.c). Written from the public libultra scheduler contract; the layouts below are the
 * offsets the game's own code fixes (client list, task lists, queue positions), not copied code. Message values
 * on the interrupt queue are the ones the game registers: retrace, RSP done, RDP done, pre-NMI, task submitted. */
#define N64C_SC_RETRACE_MSG 0x29A
#define N64C_SC_SP_DONE_MSG 0x29B
#define N64C_SC_DP_DONE_MSG 0x29C
#define N64C_SC_PRENMI_MSG 0x29D
#define N64C_SC_CMD_MSG 6
#define OS_SC_NEEDS_RDP 0x0001
#define OS_SC_NEEDS_RSP 0x0002
#define OS_SC_LAST_TASK 0x0020
#define OS_SC_SWAPBUFFER 0x0040
typedef struct OSScClient {
    struct OSScClient *next;
    OSMesgQueue *msgQ;
} OSScClient;
typedef struct OSScTask {
    struct OSScTask *next;
    u32 state;      /* hardware units still owed: OS_SC_NEEDS_RSP / OS_SC_NEEDS_RDP */
    u32 flags;
    void *framebuffer;
    OSTask list;
    OSMesgQueue *msgQ;
    OSMesg msg;
} OSScTask;
typedef struct OSSched {
    s32 retraceMsg;                     /* 0x00: type 1, what clients receive each retrace */
    u8 pad04[0x1C];
    s16 prenmiMsg;                      /* 0x20: type 4 */
    u8 pad22[0x1E];
    OSMesgQueue interruptQueue;         /* 0x40: events and submitted tasks arrive here */
    OSMesg interruptSlots[8];
    OSMesgQueue commandQueue;           /* 0x78: tasks wait here until the scheduler thread drains them */
    OSMesg commandSlots[8];
    union { OSThread thread; u8 raw[0x230]; } th; /* 0xB0 */
    OSScClient *clientList;             /* 0x2E0 */
    OSScTask *audioListHead, *gfxListHead, *audioListTail, *gfxListTail;
    OSScTask *curRSPTask, *curRDPTask;  /* 0x2F4, 0x2F8 */
    u32 frameCount;
    s32 doAudio;
} OSSched;
void osCreateScheduler(OSSched *sc, void *stack, OSPri priority, u8 mode, u8 numFields);
void osScAddClient(OSSched *sc, OSScClient *client, OSMesgQueue *msgQ);
void osScRemoveClient(OSSched *sc, OSScClient *client);
OSMesgQueue *osScGetCmdQ(OSSched *sc);
void n64c_sc_main(void *sc);
/* The scheduler thread's handlers, one per interrupt-queue message, and its task-list helpers. */
void n64c_sc_retrace(OSSched *sc);
void n64c_sc_sp_done(OSSched *sc);
void n64c_sc_dp_done(OSSched *sc);
void n64c_sc_command(OSSched *sc);
void n64c_sc_append(OSSched *sc, OSScTask *task);
void n64c_sc_yield(OSSched *sc);
/* Per-retrace game hook, called on the scheduler thread after the retrace was handled. NULL: none. */
void n64c_sc_set_retrace_hook(void (*hook)(void));
/* Called first in each retrace, before the scheduler touches its lists. A non-zero result means the game has
 * taken the machine over (the retrace is not handled further). NULL: none. */
void n64c_sc_set_pre_retrace_hook(int (*hook)(void));
/* A task's life as the scheduler sees it, for the game's per-task bookkeeping. */
typedef enum { N64C_SC_TASK_START, N64C_SC_RSP_DONE, N64C_SC_RDP_DONE } N64cScTaskEvent;
/* Called on the scheduler thread: at start (just before the RSP is given the task) and as each unit reports.
 * RSP_DONE and RDP_DONE arrive before the task's owner is told. NULL: none. */
void n64c_sc_set_task_hook(void (*hook)(OSSched *sc, OSScTask *task, N64cScTaskEvent event));
/* Additional game framebuffer ownership: zero defers the head graphics task
 * until a later scheduler event. Audio bypasses this hook. NULL: VI ownership only. */
void n64c_sc_set_task_ready_hook(int (*hook)(const OSScTask *task));
/* Called when the pre-reset interrupt message arrives. NULL: the message is ignored. */
void n64c_sc_set_prenmi_hook(void (*hook)(OSSched *sc));
typedef void (*N64cTaskFn)(OSTask *task, void *user);
void n64c_set_task_handler(u32 type, N64cTaskFn fn, void *user);
s32 __osSiRawStartDma(s32 direction,void *dram);
void osSpTaskLoad(OSTask *t);
void osSpTaskStartGo(OSTask *t);
s32 osSpTaskYielded(OSTask *t);
/* host driver (n64compat_glue.c) */
typedef struct N64cConfig {
    const char *rom_path;     /* big-endian .z64 */
    const char *eeprom_path;  /* NULL: no EEPROM */
    const char *pak_path;     /* NULL: no Controller Pak (matches the oracle) */
    int eeprom_16k;
} N64cConfig;
void n64c_init(const N64cConfig *cfg);
void n64c_shutdown(void);
/* Run one video retrace: advance virtual time, fire timers, deliver VI/event messages, step guest threads until
 * all are blocked, then present. Returns the number of guest threads still alive. */
int n64c_frame(void);
/* Run guest threads until none is runnable (used after setup and by tests). */
void n64c_run_idle(void);
typedef void (*N64cPresentFn)(void *framebuffer, void *user);
void n64c_set_present(N64cPresentFn fn, void *user);
uint32_t n64c_waiting_threads(void);
int n64c_thread_count(void);
/* Run stops (n64c_sched.c). n64c_sched_halt: called on a guest thread, ends the run there (never returns):
 * the thread is parked, the host owns the baton and nothing is dispatched again until n64c_shutdown.
 * n64c_sched_halted: nonzero once a guest halted. n64c_sched_on_guest: nonzero on a guest thread. */
__attribute__((noreturn)) void n64c_sched_halt(void);
int n64c_sched_halted(void);
int n64c_sched_on_guest(void);
/* A TLB miss of the running guest thread (the libultra fault path): the thread is stopped with OS_FLAG_FAULT,
 * its context records cause and badvaddr, OS_EVENT_FAULT is posted, and the call returns once osStartThread
 * resumes it. Returns 0 (nothing changed) off a guest thread or when no queue receives OS_EVENT_FAULT. */
int n64c_sched_fault(u32 cause, u32 badvaddr);
#ifdef __cplusplus
}
#endif
#endif
