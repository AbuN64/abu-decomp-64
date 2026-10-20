/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Hardware register offsets from https://n64.readthedocs.io/#memory-map.
 * Component-owned effects use the same PI/VI/AI/SI APIs as native callers.
 * DMA completes synchronously, matching this shim's existing component model.
 * Unimplemented register operations fail; no game-address cases live here. */
#include "n64c_mmio.h"
#include "n64c_memory.h"
#include "n64c_internal.h"
#include <string.h>

static u32 video_clock, vi[14], ai[6], pi[13], si_dram;
void n64c_mmio_reset(u32 clock)
{
    video_clock=clock;
    memset(vi,0,sizeof vi);
    memset(ai,0,sizeof ai);
    memset(pi,0,sizeof pi);
    si_dram=0;
}
int n64c_mmio_read(u32 physical,u32 *value)
{
    u32 block=physical&0xFFF00000u,offset=physical&0xFFFFFu;
    if (!value || (offset&3)) return 0;
    if (block==0x04400000u && offset<sizeof vi) {
        if (offset==4) *value=n64c_memory_physical(osViGetCurrentFramebuffer());
        else if (offset==0x10) return 0; /* scanline clock is not modeled */
        else *value=vi[offset/4];
        return 1;
    }
    if (block==0x04500000u && offset<sizeof ai) {
        *value=offset==4 ? osAiGetLength() : offset==0xC ? osAiGetStatus() : ai[offset/4];
        return 1;
    }
    if (block==0x04600000u && offset<sizeof pi) {
        *value=offset==0x10 ? 0 : pi[offset/4];
        return 1;
    }
    if (block==0x04800000u && (offset==0 || offset==4 || offset==0x10 || offset==0x18)) {
        *value=offset==0 ? si_dram : 0;
        return 1;
    }
    return 0;
}
int n64c_mmio_write(u32 physical,u32 value)
{
    u32 block=physical&0xFFF00000u,offset=physical&0xFFFFFu;
    if (offset&3) return 0;
    if (block==0x04400000u && offset<sizeof vi && offset!=0x10) {
        if (offset==4) {
            void *ram=n64c_memory_ram(value&0x00FFFFFFu,1);
            if (!ram) return 0;
            osViSwapBuffer(ram);
        }
        if (offset==0) osViBlack((u8)((value&3u)==0));
        vi[offset/4]=value;
        return 1;
    }
    if (block==0x04500000u && offset<sizeof ai) {
        if (offset==4) {
            u32 bytes=value&0x3FFF8u;
            void *ram=n64c_memory_ram(ai[0]&0x00FFFFF8u,bytes);
            if (!ram || osAiSetNextBuffer(ram,bytes)!=0) return 0;
        } else if (offset==0x10) {
            if (osAiSetFrequency(video_clock/((value&0x3FFFu)+1))<0) return 0;
        } else if (offset==0xC) return 1; /* acknowledge synchronous completion */
        ai[offset/4]=value;
        return 1;
    }
    if (block==0x04600000u && offset<sizeof pi) {
        if (offset==8) return 0; /* cartridge writes require a save-device owner */
        if (offset==0xC) {
            u32 bytes=(value&0x00FFFFFFu)+1;
            void *ram=n64c_memory_ram(pi[0]&0x00FFFFFEu,bytes);
            if (!ram || n64c_pi_read(pi[1]&0x0FFFFFFFu,ram,bytes)!=0) return 0;
            pi[offset/4]=value;
            n64c_event_post(OS_EVENT_PI);
            return 1;
        } else if (offset==0x10) return 1;
        pi[offset/4]=value;
        return 1;
    }
    if (block==0x04800000u) {
        if (offset==0) { si_dram=value; return 1; }
        if (offset==0x18) return 1;
        if (offset==4 || offset==0x10) {
            void *ram=n64c_memory_ram(si_dram&0x00FFFFF8u,64);
            if (value!=0x1FC007C0u || !ram) return 0;
            return __osSiRawStartDma(offset==4 ? OS_READ : OS_WRITE,ram)==0;
        }
    }
    return 0;
}
