/* SPDX-License-Identifier: GPL-3.0-or-later */
/* PI DMA with typed endian fixup, controller input, EEPROM and Pak as files, and the SP task dispatch. */
#include "../n64c_os.h"
#include "../n64c_pi.h"
#include "../n64c_cont.h"
#include <stdio.h>
#include <string.h>

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

static void pad_source(int port, OSContPad *p, void *user)
{
    (void)user;
    p->button = 0x8000 | (u16)port; /* A button; port in the low bits to prove the call */
    p->stick_x = -5;
    p->stick_y = 7;
}
static int gfx_seen, aud_seen;
static void gfx_task(OSTask *t, void *u) { (void)t; gfx_seen += *(int *)u; }
static void aud_task(OSTask *t, void *u) { (void)t; aud_seen += *(int *)u; }

int main(void)
{
    const char *rom = "n64c_test_rom.z64", *ee = "n64c_test_eeprom.bin", *pak = "n64c_test_pak.bin";
    remove(ee);
    remove(pak);
    u8 image[0x80];
    for (int i = 0; i < 0x80; i++)
        image[i] = (u8)i;
    FILE *f = fopen(rom, "wb");
    fwrite(image, 1, sizeof image, f);
    fclose(f);
    N64cConfig cfg = {.rom_path = rom, .eeprom_path = ee, .pak_path = NULL};
    n64c_init(&cfg);
    n64c_pi_register(0x00, 0x10, N64C_RES_RAW, NULL);
    n64c_pi_register(0x10, 0x20, N64C_RES_SWAP16, NULL);
    n64c_pi_register(0x20, 0x30, N64C_RES_SWAP32, NULL);
    n64c_pi_register(0x30, 0x50, N64C_RES_STRUCT, "222222 1111"); /* 16-byte vertex: six halfwords, four bytes */
    OSMesgQueue mq;
    OSMesg buf[4], m;
    osCreateMesgQueue(&mq, buf, 4);
    u8 out[0x80];
    memset(out, 0xEE, sizeof out);
    OSIoMesg io = {0};
    CHECK(osPiStartDma(&io, 0, OS_READ, 0x00, out, 0x50, &mq) == 0);
    CHECK(osRecvMesg(&mq, &m, OS_MESG_NOBLOCK) == 0 && m.ptr == &io);
    for (int i = 0; i < 0x10; i++) CHECK(out[i] == i);                          /* raw */
    for (int i = 0; i < 0x10; i++) CHECK(out[0x10 + i] == (i ^ 1) + 0x10);      /* 16-bit swapped */
    for (int i = 0; i < 0x10; i++) CHECK(out[0x20 + i] == (i ^ 3) + 0x20);      /* 32-bit swapped */
    for (int i = 0; i < 12; i++) CHECK(out[0x30 + i] == (i ^ 1) + 0x30);        /* x y z flag s t */
    for (int i = 12; i < 16; i++) CHECK(out[0x30 + i] == 0x30 + i);              /* rgba bytes untouched */
    for (int i = 0; i < 12; i++) CHECK(out[0x40 + i] == (i ^ 1) + 0x40);        /* second record */
    /* a DMA starting mid-resource keeps the resource alignment */
    memset(out, 0xEE, sizeof out);
    CHECK(osPiStartDma(&io, 0, OS_READ, 0x12, out, 6, &mq) == 0);
    CHECK(out[0] == 0x13 && out[1] == 0x12 && out[2] == 0x15 && out[5] == 0x16);
    /* register-level read: same bytes, completion through the OS_EVENT_PI message */
    OSMesgQueue piq; OSMesg pib[1];
    osCreateMesgQueue(&piq, pib, 1);
    osSetEventMesg(OS_EVENT_PI, &piq, (OSMesg){.ptr = &piq});
    memset(out, 0xEE, sizeof out);
    CHECK(osPiRawStartDma(OS_READ, 0x00, out, 0x10) == 0);
    CHECK(osRecvMesg(&piq, &m, OS_MESG_NOBLOCK) == 0 && m.ptr == &piq);
    for (int i = 0; i < 0x10; i++) CHECK(out[i] == i);
    CHECK(osPiRawStartDma(OS_WRITE, 0x00, out, 0x10) == -1);
    u32 word;
    CHECK(osPiReadIo(0x20, &word) == 0 && word == 0x20212223u);
    /* controller */
    n64c_cont_set_input(pad_source, NULL);
    u8 bits = 0;
    OSContStatus st[MAXCONTROLLERS];
    OSContPad pad[MAXCONTROLLERS];
    CHECK(osContInit(&mq, &bits, st) == 0 && bits == 1 && st[0].errno_ == 0 && st[1].errno_ == CONT_ERR_NO_CONTROLLER);
    CHECK(!(st[0].status & CONT_CARD_ON)); /* no Pak, like the oracle */
    CHECK(osContStartReadData(&mq) == 0 && osRecvMesg(&mq, &m, OS_MESG_NOBLOCK) == 0);
    osContGetReadData(pad);
    /* a controller reply wait with nothing started and no SI event pending is an idle-bus round trip, not a stop */
    CHECK(osRecvMesg(&mq, &m, OS_MESG_BLOCK) == 0);
    CHECK(osContStartQuery(&mq) == 0 && osRecvMesg(&mq, &m, OS_MESG_BLOCK) == 0 && mq.validCount == 0);
    CHECK(pad[0].button == 0x8000 && pad[0].stick_x == -5 && pad[0].stick_y == 7 && pad[1].errno_ == CONT_ERR_NO_CONTROLLER);
    /* EEPROM as a file */
    CHECK(osEepromProbe(&mq) == 1);
    u8 blk[16], rd[16];
    for (int i = 0; i < 16; i++) blk[i] = (u8)(0xA0 + i);
    CHECK(osEepromLongWrite(&mq, 3, blk, 16) == 0);
    CHECK(osEepromLongRead(&mq, 3, rd, 16) == 0 && memcmp(blk, rd, 16) == 0);
    CHECK(osEepromLongRead(&mq, 0, rd, 8) == 0 && rd[0] == 0xFF); /* erased */
    CHECK(osEepromLongRead(&mq, 63, rd, 16) == -1);                  /* beyond 4 Kbit */
    CHECK(osContRamRead(&mq, 0, 0, rd) == -1);                       /* no Pak configured */
    n64c_shutdown();
    /* Pak present */
    cfg.pak_path = pak;
    n64c_init(&cfg);
    osCreateMesgQueue(&mq, buf, 4);
    u8 b32[32], r32[32];
    for (int i = 0; i < 32; i++) b32[i] = (u8)(i * 3);
    CHECK(osContInit(&mq, &bits, st) == 0 && (st[0].status & CONT_CARD_ON));
    CHECK(osContRamWrite(&mq, 0, 5, b32) == 0 && osContRamRead(&mq, 0, 5, r32) == 0 && memcmp(b32, r32, 32) == 0);
    n64c_shutdown();
    n64c_init(&cfg); /* persists across runs */
    CHECK(osContRamRead(&mq, 0, 5, r32) == 0 && memcmp(b32, r32, 32) == 0);
    /* SP task dispatch: handler runs, then SP and DP events reach the registered queues */
    OSMesgQueue spq, dpq;
    OSMesg spb[2], dpb[2];
    int one = 1, ten = 10;
    osCreateMesgQueue(&spq, spb, 2);
    osCreateMesgQueue(&dpq, dpb, 2);
    osSetEventMesg(OS_EVENT_SP, &spq, m);
    osSetEventMesg(OS_EVENT_DP, &dpq, m);
    n64c_set_task_handler(M_GFXTASK, gfx_task, &one);
    n64c_set_task_handler(M_AUDTASK, aud_task, &ten);
    OSTask t = {.type = M_GFXTASK};
    osSpTaskStartGo(&t);
    CHECK(gfx_seen == 1 && aud_seen == 0 && spq.validCount == 1 && dpq.validCount == 1);
    t.type = M_AUDTASK;
    osSpTaskStartGo(&t);
    CHECK(aud_seen == 10 && spq.validCount == 2 && dpq.validCount == 1);
    /* VI swap latches at the next retrace */
    char fb1[1], fb2[1];
    osViSwapBuffer(fb1);
    CHECK(osViGetCurrentFramebuffer() == NULL);
    n64c_frame();
    CHECK(osViGetCurrentFramebuffer() == fb1);
    osViSwapBuffer(fb2);
    CHECK(osViGetCurrentFramebuffer() == fb1 && osViGetNextFramebuffer() == fb2);
    n64c_frame();
    CHECK(osViGetCurrentFramebuffer() == fb2);
    n64c_shutdown();
    remove(rom); remove(ee); remove(pak);
    printf(fails ? "FAILED %d\n" : "all passed\n", fails);
    return fails != 0;
}
