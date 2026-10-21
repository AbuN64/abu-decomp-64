/* SPDX-License-Identifier: GPL-3.0-or-later */
/* n64c_cont: serial interface adapter. Controllers come from a port-supplied source (live device or recorded
 * input script); EEPROM and the Controller Pak are plain files. Absent files mean absent hardware, which is
 * what the oracle reports (no Pak). */
#include "n64c_internal.h"
#include "n64c_cont.h"
#include "n64c_si.h"
#include "n64c_tas.h"
#ifdef N64C_INPUT_SDL3
#include "n64c_input.h"
#endif
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static N64cInputFn input_fn;
static void *input_user;
static OSContPad latched[MAXCONTROLLERS];
static u8 serial_packet[64];
static char eeprom_path[512], pak_path[512];
static int eeprom_present, eeprom_16k;
static u8 pak_ram[0x8000];
static int pak_loaded;

void n64c_cont_set_input(N64cInputFn fn, void *user)
{
    input_fn = fn;
    input_user = user;
}
void n64c_cont_reset(const N64cConfig *cfg)
{
#ifdef N64C_INPUT_SDL3
    n64c_input_stop();
#endif
    memset(latched, 0, sizeof latched);
    memset(serial_packet,0,sizeof serial_packet);
    eeprom_present = cfg && cfg->eeprom_path != NULL;
    eeprom_16k = cfg ? cfg->eeprom_16k : 0;
    eeprom_path[0] = pak_path[0] = 0;
    pak_loaded = 0;
    if (eeprom_present)
        snprintf(eeprom_path, sizeof eeprom_path, "%s", cfg->eeprom_path);
    if (cfg && cfg->pak_path)
        snprintf(pak_path, sizeof pak_path, "%s", cfg->pak_path);
    input_fn = NULL;
    n64c_si_reset();
}
void n64c_cont_close(void)
{
#ifdef N64C_INPUT_SDL3
    n64c_input_stop();
#endif
    input_fn = NULL;
    input_user = NULL;
}
static int pak_on(int channel) { return channel == 0 && pak_path[0]; }
int n64c_cont_pak_present(int channel) { return pak_on(channel) != 0; }
static void query(OSContStatus *s)
{
    for (int i = 0; i < MAXCONTROLLERS; i++) {
        s[i].type = i == 0 ? 0x0005 : 0; /* standard controller on port 1 only */
        s[i].status = i == 0 && pak_path[0] ? CONT_CARD_ON : 0;
        s[i].errno_ = i == 0 ? 0 : CONT_ERR_NO_CONTROLLER;
    }
}
static OSContStatus last_status[MAXCONTROLLERS];
static void done(OSMesgQueue *mq)
{
    OSMesg m;
    m.ptr = NULL;
    if (mq)
        osSendMesg(mq, m, OS_MESG_NOBLOCK);
}
s32 osContInit(OSMesgQueue *mq, u8 *bitpattern, OSContStatus *status)
{
    query(last_status);
    if (status)
        memcpy(status, last_status, sizeof last_status);
    if (bitpattern)
        *bitpattern = 1;
    (void)mq;
    return 0;
}
s32 n64c_cont_rescan(OSMesgQueue *mq, OSContStatus *status)
{
    (void)mq;
    query(last_status);
    if (status)
        memcpy(status, last_status, sizeof last_status);
    return 0;
}
s32 osContStartQuery(OSMesgQueue *mq)
{
    query(last_status);
    done(mq);
    return 0;
}
void osContGetQuery(OSContStatus *status) { memcpy(status, last_status, sizeof last_status); }
s32 osContStartReadData(OSMesgQueue *mq)
{
    for (int i = 0; i < MAXCONTROLLERS; i++) {
        memset(&latched[i], 0, sizeof latched[i]);
        if (i == 0 && input_fn)
            input_fn(i, &latched[i], input_user);
        else if (i != 0)
            latched[i].errno_ = CONT_ERR_NO_CONTROLLER;
    }
    n64c_tas_poll(&latched[0]);
    done(mq);
    return 0;
}
void osContGetReadData(OSContPad *pad) { memcpy(pad, latched, sizeof latched); }
/* ---- EEPROM: 8-byte blocks, 4 Kbit (64 blocks) or 16 Kbit (256 blocks) ---- */
s32 osEepromProbe(OSMesgQueue *mq)
{
    (void)mq;
    return eeprom_present ? (eeprom_16k ? 2 : 1) : 0;
}
static s32 eeprom_io(u8 block, u8 *buf, int n, int write)
{
    int blocks = eeprom_16k ? 256 : 64;
    if (!eeprom_present || block + (n + 7) / 8 > blocks)
        return -1;
    FILE *f = fopen(eeprom_path, "r+b");
    if (!f && !(f = fopen(eeprom_path, "w+b")))
        return -1;
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    for (long i = size; i < blocks * 8; i++)
        fputc(0xFF, f); /* erased */
    fseek(f, block * 8L, SEEK_SET);
    size_t ok = write ? fwrite(buf, 1, (size_t)n, f) : fread(buf, 1, (size_t)n, f);
    fclose(f);
    return ok == (size_t)n ? 0 : -1;
}
s32 osEepromLongRead(OSMesgQueue *mq, u8 block, u8 *buf, int n)
{
    s32 r = eeprom_io(block, buf, n, 0);
    done(mq);
    return r;
}
s32 osEepromLongWrite(OSMesgQueue *mq, u8 block, u8 *buf, int n)
{
    s32 r = eeprom_io(block, buf, n, 1);
    done(mq);
    return r;
}
/* ---- Controller Pak: 32 KiB, accessed in 32-byte blocks ---- */
static int pak_load(void)
{
    if (!pak_loaded) {
        FILE *f = fopen(pak_path, "rb");
        memset(pak_ram, 0, sizeof pak_ram);
        if (f) {
            if (fread(pak_ram, 1, sizeof pak_ram, f) == 0)
                memset(pak_ram, 0, sizeof pak_ram);
            fclose(f);
        }
        pak_loaded = 1;
    }
    return 1;
}
s32 osContRamRead(OSMesgQueue *mq, int channel, u16 address, u8 *buffer)
{
    if (!pak_on(channel) || (u32)address * 32 + 32 > sizeof pak_ram)
        return -1;
    pak_load();
    memcpy(buffer, pak_ram + address * 32, 32);
    done(mq);
    return 0;
}
s32 osContRamWrite(OSMesgQueue *mq, int channel, u16 address, u8 *buffer)
{
    if (!pak_on(channel) || (u32)address * 32 + 32 > sizeof pak_ram)
        return -1;
    pak_load();
    memcpy(pak_ram + address * 32, buffer, 32);
    FILE *f = fopen(pak_path, "wb");
    if (!f)
        return -1;
    size_t ok = fwrite(pak_ram, 1, sizeof pak_ram, f);
    fclose(f);
    done(mq);
    return ok == sizeof pak_ram ? 0 : -1;
}


/* PIF command framing and query/poll formats:
 * https://libdragon.dev/ref/group__joybus.html
 * https://libdragon.dev/ref/group__joybus__commands.html
 * Other commands are an explicit stop until their existing save component
 * has a qualified raw packet adapter. They never return fabricated payloads. */
static int serial_execute(void)
{
    size_t cursor=0;
    unsigned channel=0;
    OSContStatus status[MAXCONTROLLERS];
    OSContPad pads[MAXCONTROLLERS];
    query(status);
    if (!(serial_packet[63]&1)) return 0;
    while (cursor<63) {
        u8 tx=serial_packet[cursor];
        if (tx==0xFE) break;
        if (tx==0xFF) { cursor++; continue; }
        if (tx==0) { cursor++; channel++; continue; }
        if (tx>0x3F || cursor+2>=63) return -1;
        u8 rx=serial_packet[cursor+1]&0x3F;
        if (!tx || cursor+2+tx+rx>63) return -1;
        u8 *command=serial_packet+cursor+2;
        u8 *reply=command+tx;
        if (channel>=MAXCONTROLLERS || status[channel].errno_) {
            serial_packet[cursor+1]|=0x80;
        } else if ((command[0]==0 || command[0]==0xFF) && tx==1 && rx==3) {
            reply[0]=(u8)status[channel].type;
            reply[1]=(u8)(status[channel].type>>8);
            reply[2]=(status[channel].status&CONT_CARD_ON) ? 1 : 2;
        } else if (command[0]==1 && tx==1 && rx==4) {
            osContStartReadData(NULL);
            osContGetReadData(pads);
            reply[0]=(u8)(pads[channel].button>>8);
            reply[1]=(u8)pads[channel].button;
            reply[2]=(u8)pads[channel].stick_x;
            reply[3]=(u8)pads[channel].stick_y;
        } else return -1;
        cursor+=2+tx+rx;
        channel++;
    }
    serial_packet[63]=0;
    return 0;
}
s32 __osSiRawStartDma(s32 direction,void *dram)
{
    if (!dram || (direction!=OS_READ && direction!=OS_WRITE)) return -1;
    if (direction==OS_WRITE) {
        memcpy(serial_packet,dram,sizeof serial_packet);
        if (serial_execute()!=0) return -1;
    } else memcpy(dram,serial_packet,sizeof serial_packet);
    n64c_event_post(OS_EVENT_SI);
    return 0;
}
