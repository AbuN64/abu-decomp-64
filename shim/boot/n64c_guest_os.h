/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_GUEST_OS_H
#define N64C_GUEST_OS_H
/* Original 32-bit C ABI imports. Written from the callers and the shim's public
 * API, not vendor headers. The linker binds these names to the native shim.
 * A guest owns its record layouts; pointers and message words cross unchanged.
 * Include this only in guest units, never beside the native n64c_os.h model. */
struct OSMesgQueue;
struct OSPfs;
typedef void *OSMesg;
typedef unsigned int OSIntMask;
OSIntMask __osDisableInt(void);
void __osRestoreInt(OSIntMask mask);
void osCreateMesgQueue(struct OSMesgQueue *queue, OSMesg *storage, int count);
int osSendMesg(struct OSMesgQueue *queue, OSMesg message, int flags);
int osRecvMesg(struct OSMesgQueue *queue, OSMesg *message, int flags);
int __osSiRawStartDma(int direction, void *buffer);
void *__osPopThread(void *queue);
void __osEnqueueThread(void *queue, void *thread);
void osStartThread(void *thread);
unsigned int osGetThreadId(void *thread);
int __osGetCurrFaultedThread(void);
void osInitialize(void);
int __osSiRawReadIo(unsigned int address, unsigned int *value);
int __osSiRawWriteIo(unsigned int address, unsigned int value);
void osYieldThread(void);
int osVirtualToPhysical(int address);
void osUnmapTLBAll(void);
void osWritebackDCacheAll(void);
void osSpTaskYield(void);
void osViBlack(int enabled);
void *osViGetCurrentFramebuffer(void);
void osViSetSpecialFeatures(int features);
unsigned int osAiGetStatus(void);
int osPfsInitPak(void *queue, struct OSPfs *pak, int channel);
#endif
