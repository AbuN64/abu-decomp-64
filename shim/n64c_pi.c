/* SPDX-License-Identifier: GPL-3.0-or-later */
/* n64c_pi: cartridge DMA served from the ROM file (.z64, big-endian).
 * After the copy the destination is fixed up to host byte order according to the resource type that owns the
 * ROM range. The descriptors are port data registered at start-up (n64c_pi_register), never generated code:
 *   RAW      bytes stay as in the ROM (code blobs, texels, TLUTs, audio sample data)
 *   SWAP16   array of 16-bit values
 *   SWAP32   array of 32-bit values (also display lists, which are 32-bit word pairs)
 *   STRUCT   repeated record described by a field string, e.g. "2222 4 11" (digits are field widths in bytes)
 *   TAGGED   records of different sizes laid back to back; a tag (1, 2 or 4 bytes, big-endian, at a fixed offset of
 *            each record) selects the record's field string and so its size (widget pages)
 * A DMA may cover any sub-range of a resource; alignment is taken relative to the resource start. */
#include "n64c_internal.h"
#include "n64c_pi.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct { u32 tag, stride; char fields[64]; } TagLayout;
typedef struct { u32 offset, width, count; TagLayout layout[16]; } Tagged;
typedef struct { u32 lo, hi; N64cResKind kind; char fields[64]; u32 stride; const Tagged *tagged; } Res;
static Res res[256];
static int n_res;
static Tagged tagged_pool[8];
static int n_tagged;
static FILE *rom;
static N64cRelocateFn relocate;
static void *relocate_user;

void n64c_pi_set_relocator(N64cRelocateFn resolve,void *user)
{
    relocate=resolve;
    relocate_user=user;
}

int n64c_pi_open(const char *path)
{
    if (rom)
        fclose(rom);
    rom = path ? fopen(path, "rb") : NULL;
    return rom != NULL;
}
void n64c_pi_close(void)
{
    if (rom)
        fclose(rom);
    rom = NULL;
    n_res = 0;
    n_tagged = 0;
    n64c_pi_set_relocator(NULL,NULL);
}
static u32 field_width(char field)
{
    if (field=='p') return 4;
    return field>='1' && field<='8' ? (u32)(field-'0') : 0;
}
/* "offset/width;tag=fields;tag=fields..." (decimal numbers), each fields string as for STRUCT. */
static int parse_tagged(Tagged *out,const char *spec)
{
    char *end;
    unsigned long offset=strtoul(spec,&end,10);
    if (end==spec || *end!='/') return 0;
    spec=end+1;
    unsigned long width=strtoul(spec,&end,10);
    if (end==spec || (width!=1 && width!=2 && width!=4) || *end!=';') return 0;
    out->offset=(u32)offset;
    out->width=(u32)width;
    while (*end==';') {
        spec=end+1;
        unsigned long tag=strtoul(spec,&end,10);
        if (end==spec || *end!='=' || out->count>=16) return 0;
        TagLayout *layout=&out->layout[out->count];
        for (unsigned i=0;i<out->count;i++)
            if (out->layout[i].tag==tag) return 0;
        layout->tag=(u32)tag;
        unsigned count=0;
        for (spec=end+1;*spec && *spec!=';';spec++) {
            if (*spec==' ') continue;
            u32 w=field_width(*spec);
            if (!w || count>=sizeof layout->fields-1) return 0;
            layout->fields[count++]=*spec;
            layout->stride+=w;
        }
        if (!count || layout->stride<out->offset+out->width) return 0;
        out->count++;
        end=(char *)spec;
    }
    return *end==0 && out->count>0;
}
int n64c_pi_register(u32 lo,u32 hi,N64cResKind kind,const char *fields)
{
    if (n_res>=256 || hi<=lo || (unsigned)kind>N64C_RES_TAGGED) return -1;
    for (int i=0;i<n_res;i++)
        if (lo<res[i].hi && hi>res[i].lo) return -1;
    Res entry={.lo=lo,.hi=hi,.kind=kind};
    if (kind==N64C_RES_TAGGED) {
        if (!fields || n_tagged>=8) return -1;
        Tagged *tagged=&tagged_pool[n_tagged];
        memset(tagged,0,sizeof *tagged);
        if (!parse_tagged(tagged,fields)) return -1;
        entry.tagged=tagged;
        entry.stride=1;
        n_tagged++;
    } else if (kind==N64C_RES_STRUCT) {
        unsigned count=0;
        if (!fields) return -1;
        for (const char *p=fields;*p;p++) {
            if (*p==' ') continue;
            u32 width=field_width(*p);
            if (!width || count>=sizeof entry.fields-1) return -1;
            entry.fields[count++]=*p;
            entry.stride+=width;
        }
        if (!count) return -1;
    } else {
        entry.stride=kind==N64C_RES_SWAP16 ? 2 : kind==N64C_RES_SWAP32 ? 4 : 1;
        entry.fields[0]=(char)('0'+entry.stride);
    }
    if ((hi-lo)%entry.stride) return -1;
    res[n_res++]=entry;
    return 0;
}
static int convert_field(u8 *bytes,char field)
{
    u32 width=field_width(field);
    if (field=='p') {
        u32 original=(u32)bytes[0]<<24 | (u32)bytes[1]<<16 |
            (u32)bytes[2]<<8 | bytes[3];
        u32 native=0;
        if (original && (!relocate || !relocate(original,&native,relocate_user) || !native)) return 0;
        memcpy(bytes,&native,4);
    } else {
        for (u32 a=0,b=width-1;a<b;a++,b--) {
            u8 value=bytes[a];bytes[a]=bytes[b];bytes[b]=value;
        }
    }
    return 1;
}
/* Convert the fields of the record at ROM address record that lie inside [start, finish) of the buffer. */
static int fix_record(const char *fields,uint64_t record,uint64_t start,uint64_t finish,u32 dev,u8 *destination)
{
    uint64_t position=record;
    for (const char *field=fields;*field;field++) {
        u32 width=field_width(*field);
        uint64_t next=position+width;
        if (position<finish && next>start) {
            if (position<start || next>finish ||
                !convert_field(destination+(size_t)(position-dev),*field)) return 0;
        }
        position=next;
    }
    return 1;
}
/* The tag of the record at ROM address at, read from the file (big-endian); -1 when it is not readable. */
static int64_t record_tag(const Tagged *tagged,uint64_t at)
{
    u8 raw[4];
    if (fseek(rom,(long)(at+tagged->offset),SEEK_SET)!=0 || fread(raw,1,tagged->width,rom)!=tagged->width) return -1;
    int64_t value=0;
    for (u32 i=0;i<tagged->width;i++) value=value<<8|raw[i];
    return value;
}
static int fix_tagged(const Res *entry,uint64_t start,uint64_t finish,u32 dev,u8 *destination)
{
    const Tagged *tagged=entry->tagged;
    for (uint64_t record=entry->lo;record<finish;) {
        int64_t tag=record_tag(tagged,record);
        const TagLayout *layout=NULL;
        for (u32 i=0;tag>=0 && i<tagged->count;i++)
            if (tagged->layout[i].tag==(u32)tag) layout=&tagged->layout[i];
        if (!layout || record+layout->stride>entry->hi) return 0;
        uint64_t next=record+layout->stride;
        if (next>start && !fix_record(layout->fields,record,start,finish,dev,destination)) return 0;
        record=next;
    }
    return 1;
}
static int fixup(u32 dev,u8 *destination,u32 bytes)
{
    uint64_t end=(uint64_t)dev+bytes;
    for (int i=0;i<n_res;i++) {
        const Res *entry=&res[i];
        uint64_t start=dev>entry->lo ? dev : entry->lo;
        uint64_t finish=end<entry->hi ? end : entry->hi;
        if (start>=finish || entry->kind==N64C_RES_RAW) continue;
        if (entry->kind==N64C_RES_TAGGED) {
            if (!fix_tagged(entry,start,finish,dev,destination)) return 0;
            continue;
        }
        uint64_t record=entry->lo+(start-entry->lo)/entry->stride*entry->stride;
        for (;record<finish;record+=entry->stride) {
            if (!fix_record(entry->fields,record,start,finish,dev,destination)) return 0;
        }
    }
    return 1;
}
int n64c_pi_read(u32 address,void *destination,u32 bytes)
{
    if (!rom || !destination) return -1;
    if (fseek(rom, (long)(address & 0x0FFFFFFF), SEEK_SET) != 0)
        return -1;
    size_t got = fread(destination, 1, bytes, rom);
    if (got < bytes)
        memset((u8 *)destination + got, 0, bytes - got); /* past the end of the cartridge: open bus */
    return fixup(address & 0x0FFFFFFF,destination,bytes) ? 0 : -1;
}
extern void n64c_trace_event(const char *kind) __attribute__((weak)); /* boot census, when linked */
s32 osPiStartDma(OSIoMesg *io, s32 pri, s32 dir, u32 devAddr, void *dram, size_t n, OSMesgQueue *mq)
{
    (void)pri;
    if (dir != OS_READ || !rom || !dram)
        return -1;
    if (n>UINT32_MAX || n64c_pi_read(devAddr,dram,(u32)n)!=0) return -1;
    if (n64c_trace_event) n64c_trace_event("first PI DMA");
    if (io)
        io->status = 0;
    OSMesg m;
    m.ptr = io;
    osSendMesg(mq ? mq : (io ? io->retQueue : NULL), m, OS_MESG_NOBLOCK);
    return 0;
}
/* The original programs the PI registers itself (dram, cart address, length) and learns completion
 * from the PI interrupt it registered with osSetEventMesg; the native read completes in the call. */
s32 osPiRawStartDma(s32 dir, u32 devAddr, void *dram, u32 n)
{
    if (dir != OS_READ || !rom || !dram || n64c_pi_read(devAddr,dram,n)!=0)
        return -1;
    n64c_event_post(OS_EVENT_PI);
    return 0;
}
s32 osPiReadIo(u32 devAddr, u32 *data)
{
    u8 b[4];
    if (!rom || fseek(rom, (long)(devAddr & 0x0FFFFFFF), SEEK_SET) != 0 || fread(b, 1, 4, rom) != 4)
        return -1;
    *data = (u32)b[0] << 24 | (u32)b[1] << 16 | (u32)b[2] << 8 | b[3];
    return 0;
}
