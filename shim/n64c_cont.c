/* SPDX-License-Identifier: GPL-3.0-or-later */
/* n64c_cont: serial interface adapter. Controllers come from a port-supplied source (live device or recorded
 * input script); EEPROM and the Controller Pak are plain files. Absent files mean absent hardware, which is
 * what the oracle reports (no Pak). */
#include "n64c_prof.h"
#include "n64c_internal.h"
#include "n64c_cont.h"
#include "n64c_cpak.h"
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
static N64cPortFn presence_fn;
static void *presence_user;
static OSContPad latched[MAXCONTROLLERS];
static u8 serial_packet[64];
static char eeprom_path[512], pak_path[512];
static int eeprom_present, eeprom_16k;
static int last_cmd_read; /* libultra's __osContLastCmd == read: the request is already in the PIF, only the answer is fetched */
static u8 pak_ram[N64C_CPAK_BYTES];
static int pak_loaded, pak_ok;
/* The card in the slot is the file: a missing file is a new card (formatted and saved at once), an existing one
 * must be a whole 32 KiB image, else the slot stays empty and the reason goes to stderr. */
static void pak_open(void)
{
    FILE *f = fopen(pak_path, "rb");
    pak_loaded = 1;
    if (f) {
        size_t got = fread(pak_ram, 1, sizeof pak_ram, f);
        int more = fgetc(f);
        fclose(f);
        if (got != sizeof pak_ram || more != EOF) {
            fprintf(stderr, "n64c: Controller Pak %s is not a 32 KiB image; the slot stays empty\n", pak_path);
            pak_path[0] = 0;
            return;
        }
        pak_ok = 1;
        return;
    }
    n64c_cpak_format(pak_ram);
    f = fopen(pak_path, "wb");
    if (!f || fwrite(pak_ram, 1, sizeof pak_ram, f) != sizeof pak_ram) {
        fprintf(stderr, "n64c: cannot create the Controller Pak %s; the slot stays empty\n", pak_path);
        if (f)
            fclose(f);
        pak_path[0] = 0;
        return;
    }
    fclose(f);
    pak_ok = 1;
}

void n64c_cont_set_input(N64cInputFn fn, void *user)
{
    input_fn = fn;
    input_user = user;
}
void n64c_cont_set_presence(N64cPortFn fn, void *user)
{
    presence_fn = fn;
    presence_user = user;
}
static int port_present(int port)
{
    return presence_fn ? presence_fn(port, presence_user) != 0 : port < n64c_tas_pads();
}
void n64c_cont_reset(const N64cConfig *cfg)
{
#ifdef N64C_INPUT_SDL3
    n64c_input_stop();
#endif
    memset(latched, 0, sizeof latched);
    memset(serial_packet,0,sizeof serial_packet);
    last_cmd_read = 0;
    eeprom_present = cfg && cfg->eeprom_path != NULL;
    eeprom_16k = cfg ? cfg->eeprom_16k : 0;
    eeprom_path[0] = pak_path[0] = 0;
    pak_loaded = 0;
    pak_ok = 0;
    if (eeprom_present)
        snprintf(eeprom_path, sizeof eeprom_path, "%s", cfg->eeprom_path);
    if (cfg && cfg->pak_path) {
        snprintf(pak_path, sizeof pak_path, "%s", cfg->pak_path);
        pak_open();
    }
    input_fn = NULL;
    presence_fn = NULL;
    n64c_si_reset();
}
void n64c_cont_close(void)
{
#ifdef N64C_INPUT_SDL3
    n64c_input_stop();
#endif
    input_fn = NULL;
    input_user = NULL;
    presence_fn = NULL;
}
static int pak_on(int channel) { return channel == 0 && pak_path[0] && pak_ok; }
int n64c_cont_pak_present(int channel) { return pak_on(channel) != 0; }
static void query(OSContStatus *s)
{
    for (int i = 0; i < MAXCONTROLLERS; i++) {
        int present = port_present(i); /* scripted: standard controllers on the first N64C_PADS ports (default 1) */
        s[i].type = present ? 0x0005 : 0;
        s[i].status = pak_on(i) ? CONT_CARD_ON : 0;
        s[i].errno_ = present ? 0 : CONT_ERR_NO_CONTROLLER;
    }
}
static OSContStatus last_status[MAXCONTROLLERS];
static OSMesgQueue *si_mq;
OSMesgQueue *n64c_si_queue(void) { return si_mq; }
static void done(OSMesgQueue *mq)
{
    OSMesg m;
    m.ptr = NULL;
    if (mq) {
        si_mq = mq;
        osSendMesg(mq, m, OS_MESG_NOBLOCK);
    }
}
s32 osContInit(OSMesgQueue *mq, u8 *bitpattern, OSContStatus *status)
{
    query(last_status);
    if (status)
        memcpy(status, last_status, sizeof last_status);
    if (bitpattern) {
        *bitpattern = 0;
        for (int i = 0; i < MAXCONTROLLERS; i++)
            *bitpattern |= (u8)(port_present(i) << i);
    }
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
    last_cmd_read = 0;
    query(last_status);
    done(mq);
    return 0;
}
void osContGetQuery(OSContStatus *status) { memcpy(status, last_status, sizeof last_status); }
/* The pad sample of one controller read: the live source or the recorded movie, one TAS poll. */
static void sample_pads(void)
{
    for (int i = 0; i < MAXCONTROLLERS; i++) {
        memset(&latched[i], 0, sizeof latched[i]);
        if (input_fn && (presence_fn ? port_present(i) : i == 0))
            input_fn(i, &latched[i], input_user);
        else if (!port_present(i))
            latched[i].errno_ = CONT_ERR_NO_CONTROLLER;
    }
    N64C_ZONE(zi,"input-poll");
    n64c_tas_poll(&latched[0]);
    N64C_ZONE_END(zi);
}
/* A packet exchange on the serial bus takes the console's time, and its end is the SI interrupt: the PIF takes the
 * 64-byte request in 64 us (6000 cycles of the bus clock), then answers the read in 256 us plus 320 us for each
 * controller that talks. The numbers are those of the oracle's SI adapter (CakeTaste64 hosts/n64recomp/si_adapter.cpp),
 * so the interrupt lands at the same virtual time on both sides. */
enum { SI_WRITE_NS = 64000, SI_READ_NS = 256000, SI_READ_PORT_NS = 320000 };
static OSTime si_ticks(u32 ns) { return (OSTime)ns * OS_CPU_COUNTER / 1000000000u; }
static OSTime si_deadline;
static void si_complete(void) { n64c_event_post(OS_EVENT_SI); }
static void si_start(u32 ns)
{
    si_deadline = n64c_clock_now() + si_ticks(ns);
    n64c_clock_arm_si(si_deadline, si_complete);
}
/* libultra's osContStartReadData (RENDER controller poll: the game's SI queue is the one it passed to osSetEventMesg for
 * OS_EVENT_SI). Unless the last exchange was already a read, the request is written first and the caller waits on the
 * queue for the write's interrupt; then the read starts, the pads are sampled as the PIF answers (the oracle's TAS poll
 * is at this same point), and the call returns with the exchange still running. Its SI interrupt, one read time later,
 * is the message the game's next poll waits for. A host caller with no queue (the serial adapter, tools) gets the sample
 * at once, with no bus time. */
s32 osContStartReadData(OSMesgQueue *mq)
{
    if (!mq) {
        sample_pads();
        return 0;
    }
    if (n64c_clock_si_armed())
        return -1; /* the SI is busy: libultra's raw DMA refuses */
    si_mq = mq;
    __osSiGetAccess();
    if (!last_cmd_read) {
        si_start(SI_WRITE_NS);
        if (n64c_sched_on_guest()) {
            osRecvMesg(mq, NULL, OS_MESG_BLOCK);
        } else { /* no guest thread to block: the bus runs out its time here */
            n64c_clock_advance_to(si_deadline);
            osRecvMesg(mq, NULL, OS_MESG_NOBLOCK);
        }
    }
    sample_pads();
    int talking = 0;
    for (int i = 0; i < MAXCONTROLLERS; i++)
        talking += port_present(i) != 0;
    si_start(SI_READ_NS + SI_READ_PORT_NS * (u32)talking);
    last_cmd_read = 1;
    __osSiRelAccess();
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
u8 *n64c_cont_pak_image(int channel) { return pak_on(channel) ? pak_ram : NULL; }
/* Persist a changed range of the card to its file. */
int n64c_cont_pak_commit(u32 offset, u32 length)
{
    if (!pak_on(0) || offset > sizeof pak_ram || length > sizeof pak_ram - offset)
        return -1;
    FILE *f = fopen(pak_path, "r+b");
    if (!f)
        return -1;
    int ok = fseek(f, (long)offset, SEEK_SET) == 0 && fwrite(pak_ram + offset, 1, length, f) == length;
    return (fclose(f) == 0 && ok) ? 0 : -1;
}
s32 osContRamRead(OSMesgQueue *mq, int channel, u16 address, u8 *buffer)
{
    if (!pak_on(channel) || (u32)address * 32 + 32 > sizeof pak_ram)
        return -1;
    memcpy(buffer, pak_ram + address * 32, 32);
    done(mq);
    return 0;
}
s32 osContRamWrite(OSMesgQueue *mq, int channel, u16 address, u8 *buffer)
{
    if (!pak_on(channel) || (u32)address * 32 + 32 > sizeof pak_ram)
        return -1;
    memcpy(pak_ram + address * 32, buffer, 32);
    if (n64c_cont_pak_commit((u32)address * 32, 32) != 0)
        return -1;
    done(mq);
    return 0;
}
/* Pak packets on the serial bus: read is [2, addr_hi, addr_lo] answered by 32 bytes and their CRC; write is
 * [3, addr_hi, addr_lo, 32 bytes] answered by the CRC of the data. The low five bits of the address field are the
 * address CRC. Blocks below 0x400 are the card; 0x400.. is the accessory area (bank select, rumble), which a
 * memory card answers with zeros and ignores. A packet whose address CRC is wrong is answered with an inverted
 * data CRC, which the caller sees as a failed transfer. */
static void pak_packet(u8 *command, u8 *reply, int write)
{
    u16 field = (u16)((command[1] << 8) | command[2]);
    u16 block = (u16)(field >> 5);
    int good = n64c_cpak_address_crc(block) == (field & 0x1F);
    u8 data[32];
    if (write) {
        if (good && block < N64C_CPAK_BYTES / 32)
            good = (memcpy(pak_ram + block * 32u, command + 3, 32), n64c_cont_pak_commit(block * 32u, 32) == 0);
        reply[0] = (u8)(n64c_cpak_data_crc(command + 3) ^ (good ? 0 : 0xFF));
    } else {
        memset(data, 0, sizeof data);
        if (good && block < N64C_CPAK_BYTES / 32)
            memcpy(data, pak_ram + block * 32u, 32);
        memcpy(reply, data, 32);
        reply[32] = (u8)(n64c_cpak_data_crc(data) ^ (good ? 0 : 0xFF));
    }
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
        } else if (command[0]==2 && tx==3 && rx==33 && (status[channel].status&CONT_CARD_ON)) {
            pak_packet(command,reply,0);
        } else if (command[0]==3 && tx==35 && rx==1 && (status[channel].status&CONT_CARD_ON)) {
            pak_packet(command,reply,1);
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
