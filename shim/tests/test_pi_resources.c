/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Byte-order descriptors of ROM resources (the rows of decomp/us_rev1.json "resources"), exercised through the PI
 * layer with hand-derived goldens: a menu-archive header (21 big-endian words), its 28-byte image records
 * (u32 u32 u16 u16 u16 u16 u16 u16 u32 u32), a resource directory (u32 table), and the menu archive's font records,
 * glyph tables, bitmap offsets, string index, resource map, the audio section table and an audio control bank container
 * (record graph walked for its field widths). Bytes outside a descriptor stay
 * as in the ROM. */
#include "../n64c_os.h"
#include "../n64c_pi.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int checks;
#define CHECK(value) do { checks++; if (!(value)) {     fprintf(stderr,"FAIL resources:%d: %s\n",__LINE__,#value); exit(1); } } while (0)
static OSMesgQueue queue;
static OSMesg messages[4];
static void be32(u8 *where,u32 value)
{
    where[0]=(u8)(value>>24);where[1]=(u8)(value>>16);where[2]=(u8)(value>>8);where[3]=(u8)value;
}
static void be16(u8 *where,unsigned value)
{
    where[0]=(u8)(value>>8);where[1]=(u8)value;
}
static void event(int success)
{
    OSMesg message;
    CHECK((osRecvMesg(&queue,&message,OS_MESG_NOBLOCK)==0)==success);
}
typedef struct { u32 zero0, zero4; s16 width, offsetY, height, count, spacing, unk12; u32 glyphs, bitmaps, size; } FontRecord;
_Static_assert(sizeof(FontRecord)==32,"32-byte menu archive font record");
typedef struct { u32 texture; s8 width, height, offsetX, offsetY; } Glyph;
_Static_assert(sizeof(Glyph)==8,"8-byte glyph");
typedef struct { u32 pointer, link; u16 format, width, height, colours, spare, runtime; u32 palette, pixels; } ImageRecord;
_Static_assert(sizeof(ImageRecord)==28,"28-byte menu archive image record");
int main(void)
{
    const char *path="n64c_resources_test.rom";
    enum { HEADER=0x40, IMAGES=0x40+0x54, DIRECTORY=0x140, TEXT=0x180 };
    u8 rom[0xC00]={0};
    static const u32 words[21]={0x54,0x15fc,0x1650,0x60,0x16b0,0x58,0x1708,0x900,0x2008,8,0x2010,0x1888,0x3898,
        0x1b0,0x3a48,0x381f4,0x3bc3c,0x4c2b0,0x87eec,0x3358,0xffffffffu};
    for (unsigned i=0;i<21;i++) be32(rom+HEADER+4*i,words[i]);
    /* record 0: format 8, 284x222, 256 colours, pixels at 0xF9C0; record 1: 284x183, 128 colours */
    be16(rom+IMAGES+8,8);be16(rom+IMAGES+10,284);be16(rom+IMAGES+12,222);be16(rom+IMAGES+14,256);
    be32(rom+IMAGES+24,0xF9C0);
    be16(rom+IMAGES+28+8,8);be16(rom+IMAGES+28+10,284);be16(rom+IMAGES+28+12,183);be16(rom+IMAGES+28+14,128);
    be32(rom+IMAGES+28+20,0xFBC0);be32(rom+IMAGES+28+24,0x1C9A0);
    be32(rom+DIRECTORY,3);be32(rom+DIRECTORY+4,0x14);be32(rom+DIRECTORY+8,0xE8F50);be32(rom+DIRECTORY+12,0x640);
    memcpy(rom+TEXT,"Display Buffer",15);
    enum { FONTS=0x200, OFFSETS=0x260, GLYPHS=0x2C0, INDEX=0x4C0, MAP=0x4D0, AUDIO=0x560 };
    /* two 32-byte font records: width 8, offset -9, height 10, 7 bitmaps, spacing -2; second with 16, -6, 9, 11, -2 */
    be16(rom+FONTS+8,8);be16(rom+FONTS+10,0xFFF7);be16(rom+FONTS+12,10);be16(rom+FONTS+14,7);be16(rom+FONTS+16,0xFFFE);
    be32(rom+FONTS+0x14,0);be32(rom+FONTS+0x18,0);be32(rom+FONTS+0x1C,0x12A8);
    be16(rom+FONTS+32+8,16);be16(rom+FONTS+32+10,0xFFFA);be16(rom+FONTS+32+12,9);be16(rom+FONTS+32+14,11);be16(rom+FONTS+32+16,0xFFFE);
    be32(rom+FONTS+32+0x14,0x300);be32(rom+FONTS+32+0x18,0x1318);be32(rom+FONTS+32+0x1C,0xEA8);
    be32(rom+OFFSETS,0x12A8);be32(rom+OFFSETS+4,0x12B8);be32(rom+OFFSETS+8,0x21C0);
    /* glyph 0: texture 0, 5x6, offsets 16, -4; glyph 1: texture 0x30, 5x9 */
    be32(rom+GLYPHS,0);rom[GLYPHS+4]=5;rom[GLYPHS+5]=6;rom[GLYPHS+6]=0x10;rom[GLYPHS+7]=0xFC;
    be32(rom+GLYPHS+8,0x30);rom[GLYPHS+12]=5;rom[GLYPHS+13]=9;rom[GLYPHS+14]=0x10;rom[GLYPHS+15]=0xF8;
    be32(rom+INDEX,0);be32(rom+INDEX+4,0x34C);
    be32(rom+MAP,0);be32(rom+MAP+4,4804);be32(rom+MAP+8,0);
    be32(rom+MAP+12,4804);be32(rom+MAP+16,916);be32(rom+MAP+20,0);
    be32(rom+AUDIO,0x28B264);be32(rom+AUDIO+4,0x299510);be32(rom+AUDIO+8,0x299510);be32(rom+AUDIO+12,0x503CB8);
    /* three back-to-back widget records of different sizes (tag = u16 at +0x0E): type 1 (0x2C), 3 (0x30), 4 (0x3C), then
       a record whose tag no layout names */
    enum { PAGE=0x600, PAGE_BYTES=0x2C+0x30+0x3C };
    be32(rom+PAGE,0x11223344);be16(rom+PAGE+0xC,0x3B);be16(rom+PAGE+0xE,1);rom[PAGE+0x10]=0xFF;be16(rom+PAGE+0x12,0x2002);
    be16(rom+PAGE+0x14,0x5B);be32(rom+PAGE+0x20,0x3F800000);
    be16(rom+PAGE+0x2C+0xE,3);be32(rom+PAGE+0x2C+0x2C,0xCAFEBABEu);
    for (unsigned i=0;i<6;i++) rom[PAGE+0x5C+0x2C+i]=(u8)(i+1);
    be16(rom+PAGE+0x5C+0xE,4);be16(rom+PAGE+0x5C+0x32,0x0102);be32(rom+PAGE+0x5C+0x34,7);be32(rom+PAGE+0x5C+0x38,0x1378);
    be16(rom+PAGE+0x98+0xE,99);
    /* cue sound containers (tag = record count at +0x1C): one of 56 bytes, one of 80 with two 24-byte records */
    enum { CUE=0x700, CUE_BYTES=56+80 };
    static const u32 cueA[8]={2,0x10,0x18,0x38,0xAAAA0001u,0,0x18,1}, cueB[8]={2,0x10,0x18,0x50,0xBBBB0002u,0,0x18,2};
    for (unsigned i=0;i<8;i++) { be32(rom+CUE+4*i,cueA[i]); be32(rom+CUE+56+4*i,cueB[i]); }
    for (unsigned r=0;r<3;r++) {
        u8 *record=rom+CUE+(r==0 ? 32 : 56+32+24*(r-1));
        for (unsigned h=0;h<7;h++) be16(record+2*h,0x1100+(r<<4)+h);
        record[0xE]=0x7F;record[0xF]=0x81;
        for (unsigned h=0;h<4;h++) be16(record+0x10+2*h,0x2200+(r<<4)+h);
    }
    /* an audio control bank container read in place (pointers linked at 0x00800000): header, bank file, bank, one
     * instrument, one sound with envelope, key map and an ADPCM wave with loop and book; a copy with a misaligned
     * sound pointer is refused at registration */
    enum { CTL=0x800, CTL_BYTES=0x130, BADCTL=0xA00 };
    u8 *c=rom+CTL;
    be32(c,1);be32(c+4,0x800014);be32(c+8,0x12345678);
    be16(c+0x14,0x4231);be16(c+0x16,1);be32(c+0x18,0x800020);
    be16(c+0x20,1);c[0x22]=5;be32(c+0x24,22050);be32(c+0x28,0);be32(c+0x2C,0x800040);
    c[0x40]=100;c[0x41]=64;be16(c+0x4C,200);be16(c+0x4E,1);be32(c+0x50,0x800060);
    be32(c+0x60,0x800070);be32(c+0x64,0x800080);be32(c+0x68,0x800090);c[0x6C]=64;c[0x6D]=127;c[0x6E]=2;
    be32(c+0x70,1000);be32(c+0x74,2000);be32(c+0x78,0x12345678);c[0x7C]=127;c[0x7D]=100;
    c[0x80]=1;c[0x81]=127;c[0x82]=20;c[0x83]=90;c[0x84]=60;c[0x85]=0xF6;
    be32(c+0x90,0x4000);be32(c+0x94,9000);c[0x98]=0;c[0x99]=1;be32(c+0x9C,0x8000B0);be32(c+0xA0,0x8000E0);
    be32(c+0xB0,16);be32(c+0xB4,4000);be32(c+0xB8,3);
    for (unsigned i=0;i<16;i++) be16(c+0xBC+2*i,0xFF00u+i);
    be32(c+0xE0,2);be32(c+0xE4,2);
    for (unsigned i=0;i<32;i++) be16(c+0xE8+2*i,(i*257u-4000u)&0xFFFFu);
    memcpy(rom+BADCTL,c,CTL_BYTES);
    be32(rom+BADCTL+0x50,0x800062);
    /* overlay data the source types (IMAGE): the ROM holds the big-endian rows, the native image the typed ones */
    enum { OVL=0xB40 };
    typedef struct { u32 info, flags; s32 behaviour; f32 gravity; } Setup;
    static const Setup setups[2]={{0x80104458u,0x3C5729u,5,-2304.0f},{0,0,0,0.0f}};
    be32(rom+OVL,0x80104458u);be32(rom+OVL+4,0x3C5729u);be32(rom+OVL+8,5);be32(rom+OVL+12,0xC5100000u);
    rom[OVL-1]=0x5A;rom[OVL+sizeof setups]=0xA5;
    for (u32 i=0;i<16;i++) rom[0xBE0+i]=(u8)(i+1);
    FILE *file=fopen(path,"wb");
    CHECK(file && fwrite(rom,1,sizeof rom,file)==sizeof rom && fclose(file)==0);
    N64cConfig config={.rom_path=path};
    n64c_init(&config);
    osCreateMesgQueue(&queue,messages,4);
    CHECK(n64c_pi_register(HEADER,HEADER+0x54,N64C_RES_SWAP32,NULL)==0);
    CHECK(n64c_pi_register(IMAGES,IMAGES+56,N64C_RES_STRUCT,"4 4 2 2 2 2 2 2 4 4")==0);
    CHECK(n64c_pi_register(DIRECTORY,DIRECTORY+16,N64C_RES_SWAP32,NULL)==0);
    CHECK(n64c_pi_register(TEXT,TEXT+15,N64C_RES_RAW,NULL)==0);
    CHECK(n64c_pi_register(FONTS,FONTS+64,N64C_RES_STRUCT,"4 4 2 2 2 2 2 2 4 4 4")==0);
    CHECK(n64c_pi_register(OFFSETS,OFFSETS+12,N64C_RES_SWAP32,NULL)==0);
    CHECK(n64c_pi_register(GLYPHS,GLYPHS+16,N64C_RES_STRUCT,"4 1 1 1 1")==0);
    CHECK(n64c_pi_register(INDEX,INDEX+8,N64C_RES_SWAP32,NULL)==0);
    CHECK(n64c_pi_register(MAP,MAP+24,N64C_RES_SWAP32,NULL)==0);
    CHECK(n64c_pi_register(AUDIO,AUDIO+32,N64C_RES_SWAP32,NULL)==0);
    CHECK(n64c_pi_register(HEADER+0x50,HEADER+0x58,N64C_RES_SWAP32,NULL)==-1); /* overlaps the header */
    static const char *widgets="14/2;1=4 4 4 2 2 1 1 2 2 2 2 2 2 2 4 4 4;3=4 4 4 2 2 1 1 2 2 2 2 2 2 2 4 4 4 4;"
        "4=4 4 4 2 2 1 1 2 2 2 2 2 2 2 4 4 4 1 1 1 1 1 1 2 4 4";
    CHECK(n64c_pi_register(PAGE+0x200,PAGE+0x240,N64C_RES_TAGGED,NULL)==-1);
    CHECK(n64c_pi_register(PAGE+0x200,PAGE+0x240,N64C_RES_TAGGED,"14/3;1=4 4 4 2 2")==-1);          /* tag width */
    CHECK(n64c_pi_register(PAGE+0x200,PAGE+0x240,N64C_RES_TAGGED,"14/2;1=4 4")==-1);                /* record shorter than its tag */
    CHECK(n64c_pi_register(PAGE+0x200,PAGE+0x240,N64C_RES_TAGGED,"14/2;1=4 4 4 2 2;1=4 4 4 2 2")==-1); /* tag twice */
    CHECK(n64c_pi_register(PAGE+0x200,PAGE+0x240,N64C_RES_TAGGED,"14/2")==-1);                      /* no layout */
    CHECK(n64c_pi_register(PAGE,PAGE+PAGE_BYTES+0x30,N64C_RES_TAGGED,widgets)==0);
    {
        static const char cueRecord[]="2 2 2 2 2 2 2 1 1 2 2 2 2 ";
        char layouts[200];
        snprintf(layouts,sizeof layouts,"28/4;1=4 4 4 4 4 4 4 4 %s;2=4 4 4 4 4 4 4 4 %s%s",cueRecord,cueRecord,cueRecord);
        CHECK(n64c_pi_register(CUE,CUE+CUE_BYTES,N64C_RES_TAGGED,layouts)==0);    /* 34 fields in one layout */
    }
    /* the whole header, then one word out of it */
    u32 header[21];
    CHECK(osPiStartDma(NULL,0,OS_READ,HEADER,header,sizeof header,&queue)==0);
    event(1);
    CHECK(header[0]==0x54 && header[1]==0x15fc && header[9]==8 && header[15]==0x381f4 && header[20]==0xffffffffu);
    u32 word=0;
    CHECK(osPiStartDma(NULL,0,OS_READ,HEADER+4*15,&word,4,&queue)==0);
    event(1);
    CHECK(word==0x381f4);
    /* both image records in one DMA */
    ImageRecord records[2];
    CHECK(osPiStartDma(NULL,0,OS_READ,IMAGES,records,sizeof records,&queue)==0);
    event(1);
    CHECK(records[0].pointer==0 && records[0].format==8 && records[0].width==284 && records[0].height==222 &&
        records[0].colours==256 && records[0].palette==0 && records[0].pixels==0xF9C0);
    CHECK(records[1].width==284 && records[1].height==183 && records[1].colours==128 &&
        records[1].palette==0xFBC0 && records[1].pixels==0x1C9A0);
    /* record 1 alone, and its first 14 bytes (whole fields only) */
    ImageRecord one;
    memset(&one,0,sizeof one);
    CHECK(osPiStartDma(NULL,0,OS_READ,IMAGES+28,&one,sizeof one,&queue)==0);
    event(1);
    CHECK(one.height==183 && one.pixels==0x1C9A0);
    u16 halves[7]={0};
    CHECK(osPiStartDma(NULL,0,OS_READ,IMAGES+28+8,halves,6,&queue)==0);
    event(1);
    CHECK(halves[0]==8 && halves[1]==284 && halves[2]==183);
    /* Partial fields return native-order slices and post completion. */
    u8 scratch[28];
    CHECK(osPiStartDma(NULL,0,OS_READ,IMAGES+28,scratch,9,&queue)==0);
    event(1);
    CHECK(memcmp(scratch,&one,9)==0);
    CHECK(osPiStartDma(NULL,0,OS_READ,IMAGES+28+8,scratch,5,&queue)==0);
    event(1);
    CHECK(memcmp(scratch,(u8 *)&one+8,5)==0);
    /* A bulk read ending one byte into a neighbouring scalar must complete.
     * This also covers raw bytes before a typed range and every cut of a word. */
    static const u8 scalar_bytes[4]={3,0,0,0};
    for (u32 begin=0;begin<4;begin++) {
        for (u32 size=1;size<=4-begin;size++) {
            u8 guarded[6];
            memset(guarded,0xA5,sizeof guarded);
            CHECK(osPiStartDma(NULL,0,OS_READ,DIRECTORY+begin,guarded+1,size,&queue)==0);
            event(1);event(0);
            CHECK(memcmp(guarded+1,scalar_bytes+begin,size)==0);
            CHECK(guarded[0]==0xA5 && guarded[size+1]==0xA5);
            u32 origin=0;
            CHECK(n64c_pi_origin(guarded+1,&origin) && origin==DIRECTORY+begin);
        }
    }
    osSetEventMesg(OS_EVENT_PI,&queue,(OSMesg){.data32=0x1234});
    u8 boundary[3]={0xA5,0xA5,0xA5};
    CHECK(osPiRawStartDma(OS_READ,DIRECTORY-1,boundary,2)==0);
    event(1);event(0);
    CHECK(boundary[0]==0 && boundary[1]==3 && boundary[2]==0xA5);
    /* Graph and fixed-record fields allow eight-byte scalars; exercise all
     * starts and ends, including a start seven bytes beyond the width marker. */
    CHECK(n64c_pi_register(0xBE0,0xBE8,N64C_RES_GRAPH,"F:8")==0);
    CHECK(n64c_pi_register(0xBE8,0xBF0,N64C_RES_STRUCT,"8")==0);
    for (u32 kind=0;kind<2;kind++) {
        for (u32 begin=0;begin<8;begin++) {
            for (u32 size=1;size<=8-begin;size++) {
                u8 bytes[10];memset(bytes,0xA5,sizeof bytes);
                CHECK(osPiStartDma(NULL,0,OS_READ,0xBE0+kind*8+begin,bytes+1,size,&queue)==0);
                event(1);event(0);
                for (u32 i=0;i<size;i++) CHECK(bytes[i+1]==8+kind*8-begin-i);
                CHECK(bytes[0]==0xA5 && bytes[size+1]==0xA5);
            }
        }
    }
    /* directory table */
    u32 directory[4];
    CHECK(osPiStartDma(NULL,0,OS_READ,DIRECTORY,directory,sizeof directory,&queue)==0);
    event(1);
    CHECK(directory[0]==3 && directory[1]==0x14 && directory[2]==0xE8F50 && directory[3]==0x640);
    /* raw text and an unregistered range keep the ROM bytes */
    char text[15];
    CHECK(osPiStartDma(NULL,0,OS_READ,TEXT,text,sizeof text,&queue)==0);
    event(1);
    CHECK(strcmp(text,"Display Buffer")==0);
    u8 plain[4];
    CHECK(osPiStartDma(NULL,0,OS_READ,HEADER+0x54+56,plain,4,&queue)==0);
    event(1);
    CHECK(plain[0]==rom[HEADER+0x54+56] && plain[3]==rom[HEADER+0x54+59]);
    /* font records: halfwords signed, words whole, in one DMA and as the second record alone */
    FontRecord fonts[2];
    CHECK(osPiStartDma(NULL,0,OS_READ,FONTS,fonts,sizeof fonts,&queue)==0);
    event(1);
    CHECK(fonts[0].width==8 && fonts[0].offsetY==-9 && fonts[0].height==10 && fonts[0].count==7 && fonts[0].spacing==-2 &&
        fonts[0].glyphs==0 && fonts[0].bitmaps==0 && fonts[0].size==0x12A8);
    CHECK(fonts[1].width==16 && fonts[1].offsetY==-6 && fonts[1].count==11 && fonts[1].glyphs==0x300 &&
        fonts[1].bitmaps==0x1318 && fonts[1].size==0xEA8);
    FontRecord font;
    CHECK(osPiStartDma(NULL,0,OS_READ,FONTS+32,&font,sizeof font,&queue)==0);
    event(1);
    CHECK(font.glyphs==0x300 && font.spacing==-2);
    /* bitmap offsets are 32-bit values */
    u32 offsets[3];
    CHECK(osPiStartDma(NULL,0,OS_READ,OFFSETS,offsets,sizeof offsets,&queue)==0);
    event(1);
    CHECK(offsets[0]==0x12A8 && offsets[1]==0x12B8 && offsets[2]==0x21C0);
    /* glyphs: the texture word is swapped, the four signed bytes are not */
    Glyph glyphs[2];
    CHECK(osPiStartDma(NULL,0,OS_READ,GLYPHS,glyphs,sizeof glyphs,&queue)==0);
    event(1);
    CHECK(glyphs[0].texture==0 && glyphs[0].width==5 && glyphs[0].height==6 && glyphs[0].offsetX==16 && glyphs[0].offsetY==-4);
    CHECK(glyphs[1].texture==0x30 && glyphs[1].width==5 && glyphs[1].height==9 && glyphs[1].offsetY==-8);
    /* string index, resource map entries and the audio section table */
    u32 index[2];
    CHECK(osPiStartDma(NULL,0,OS_READ,INDEX,index,sizeof index,&queue)==0);
    event(1);
    CHECK(index[0]==0 && index[1]==0x34C);
    u32 map[6];
    CHECK(osPiStartDma(NULL,0,OS_READ,MAP,map,sizeof map,&queue)==0);
    event(1);
    CHECK(map[1]==4804 && map[2]==0 && map[3]==4804 && map[4]==916 && map[5]==0);
    u32 audio[4];
    CHECK(osPiStartDma(NULL,0,OS_READ,AUDIO,audio,sizeof audio,&queue)==0);
    event(1);
    CHECK(audio[0]==0x28B264 && audio[1]==0x299510 && audio[2]==0x299510 && audio[3]==0x503CB8);
    /* widget page: the whole range, one record on its own, one word out of a record */
    u8 page[PAGE_BYTES+0x30];
    CHECK(osPiStartDma(NULL,0,OS_READ,PAGE,page,PAGE_BYTES,&queue)==0);
    event(1);
    u32 w32;u16 w16;
    memcpy(&w32,page,4);CHECK(w32==0x11223344);
    memcpy(&w16,page+0xC,2);CHECK(w16==0x3B);
    memcpy(&w16,page+0xE,2);CHECK(w16==1);
    CHECK(page[0x10]==0xFF);
    memcpy(&w16,page+0x12,2);CHECK(w16==0x2002);
    memcpy(&w16,page+0x14,2);CHECK(w16==0x5B);
    memcpy(&w32,page+0x20,4);CHECK(w32==0x3F800000);
    memcpy(&w32,page+0x2C+0x2C,4);CHECK(w32==0xCAFEBABEu);
    for (unsigned i=0;i<6;i++) CHECK(page[0x5C+0x2C+i]==i+1);
    memcpy(&w16,page+0x5C+0x32,2);CHECK(w16==0x0102);
    memcpy(&w32,page+0x5C+0x34,4);CHECK(w32==7);
    memcpy(&w32,page+0x5C+0x38,4);CHECK(w32==0x1378);
    u8 second[0x30+0x3C];
    CHECK(osPiStartDma(NULL,0,OS_READ,PAGE+0x2C,second,sizeof second,&queue)==0);
    event(1);
    memcpy(&w16,second+0xE,2);CHECK(w16==3);
    memcpy(&w32,second+0x2C,4);CHECK(w32==0xCAFEBABEu);
    memcpy(&w32,second+0x30+0x38,4);CHECK(w32==0x1378);
    u32 image=0;
    CHECK(osPiStartDma(NULL,0,OS_READ,PAGE+0x2C+0x2C,&image,4,&queue)==0);
    event(1);
    CHECK(image==0xCAFEBABEu);
    u8 cut[8];
    CHECK(osPiStartDma(NULL,0,OS_READ,PAGE+0x2C+0x2C,cut,3,&queue)==0);
    event(1);
    CHECK(cut[0]==0xBE && cut[1]==0xBA && cut[2]==0xFE);
    CHECK(osPiStartDma(NULL,0,OS_READ,PAGE+PAGE_BYTES,cut,4,&queue)==-1);  /* a record no layout names */
    event(0);
    u8 cue[CUE_BYTES];
    CHECK(osPiStartDma(NULL,0,OS_READ,CUE,cue,sizeof cue,&queue)==0);
    event(1);
    memcpy(&w32,cue+0x10,4);CHECK(w32==0xAAAA0001u);
    memcpy(&w32,cue+0x1C,4);CHECK(w32==1);
    memcpy(&w16,cue+0x20,2);CHECK(w16==0x1100);
    memcpy(&w16,cue+0x2C,2);CHECK(w16==0x1106);
    CHECK(cue[0x2E]==0x7F && cue[0x2F]==0x81);
    memcpy(&w16,cue+0x30,2);CHECK(w16==0x2200);
    memcpy(&w16,cue+0x36,2);CHECK(w16==0x2203);
    memcpy(&w32,cue+56+0x1C,4);CHECK(w32==2);
    memcpy(&w16,cue+56+0x20,2);CHECK(w16==0x1110);
    memcpy(&w16,cue+56+0x38+0xC,2);CHECK(w16==0x1126);
    CHECK(n64c_pi_register(CTL,CTL+CTL_BYTES,N64C_RES_ALCTL,"4")==-1);
    CHECK(n64c_pi_register(BADCTL,BADCTL+CTL_BYTES,N64C_RES_ALCTL,NULL)==-1);
    CHECK(n64c_pi_register(CTL,CTL+CTL_BYTES,N64C_RES_ALCTL,NULL)==0);
    u8 ctlA[0x80],ctlB[CTL_BYTES-0x80];           /* two page-like reads split between records */
    CHECK(osPiStartDma(NULL,0,OS_READ,CTL,ctlA,sizeof ctlA,&queue)==0);
    event(1);
    CHECK(osPiStartDma(NULL,0,OS_READ,CTL+0x80,ctlB,sizeof ctlB,&queue)==0);
    event(1);
    s16 h16;
    memcpy(&w32,ctlA,4);CHECK(w32==1);
    memcpy(&w32,ctlA+4,4);CHECK(w32==0x800014);
    memcpy(&w32,ctlA+8,4);CHECK(w32==0x12345678);
    memcpy(&h16,ctlA+0x14,2);CHECK(h16==0x4231);
    memcpy(&w32,ctlA+0x18,4);CHECK(w32==0x800020);
    memcpy(&h16,ctlA+0x20,2);CHECK(h16==1);
    CHECK(ctlA[0x22]==5);
    memcpy(&w32,ctlA+0x24,4);CHECK(w32==22050);
    memcpy(&w32,ctlA+0x2C,4);CHECK(w32==0x800040);
    CHECK(ctlA[0x40]==100 && ctlA[0x41]==64);
    memcpy(&h16,ctlA+0x4C,2);CHECK(h16==200);
    memcpy(&h16,ctlA+0x4E,2);CHECK(h16==1);
    memcpy(&w32,ctlA+0x68,4);CHECK(w32==0x800090);
    CHECK(ctlA[0x6C]==64 && ctlA[0x6D]==127 && ctlA[0x6E]==2);
    memcpy(&w32,ctlA+0x78,4);CHECK(w32==0x12345678);
    CHECK(ctlA[0x7C]==127 && ctlA[0x7D]==100);
    CHECK(ctlB[0]==1 && ctlB[2]==20 && ctlB[5]==0xF6);   /* the key map stays bytes */
    memcpy(&w32,ctlB+0x10,4);CHECK(w32==0x4000);
    memcpy(&w32,ctlB+0x14,4);CHECK(w32==9000);
    CHECK(ctlB[0x18]==0 && ctlB[0x19]==1);
    memcpy(&w32,ctlB+0x20,4);CHECK(w32==0x8000E0);
    memcpy(&w32,ctlB+0x38,4);CHECK(w32==3);
    /* loop state and codebook halfwords arrive in the audio ABI's word-swapped layout: element k sits at k ^ 1 */
    memcpy(&h16,ctlB+0x3C,2);CHECK(h16==(s16)0xFF01);
    memcpy(&h16,ctlB+0x3C+30,2);CHECK(h16==(s16)0xFF0E);
    memcpy(&w32,ctlB+0x64,4);CHECK(w32==2);
    memcpy(&h16,ctlB+0x68,2);CHECK(h16==257-4000);
    memcpy(&h16,ctlB+0x68+62,2);CHECK(h16==30*257-4000);
    CHECK(osPiStartDma(NULL,0,OS_READ,CTL+0x92,cut,4,&queue)==0);
    event(1);
    CHECK(cut[0]==0 && cut[1]==0 && cut[2]==0x28 && cut[3]==0x23);
    CHECK(osPiStartDma(NULL,0,OS_READ,CTL+0x90,cut,6,&queue)==0);
    event(1);
    CHECK(memcmp(cut,ctlB+0x10,6)==0);
    u8 cueTail[0x20];
    CHECK(osPiStartDma(NULL,0,OS_READ,CUE+56+0x20,cueTail,sizeof cueTail,&queue)==0);  /* starts at the second container's records */
    event(1);
    memcpy(&w16,cueTail,2);CHECK(w16==0x1110);
    CHECK(n64c_pi_register_image(OVL,OVL+sizeof setups,NULL)==-1);
    CHECK(n64c_pi_register_image(CTL+0x10,CTL+0x20,(const u8 *)setups)==-1);  /* overlaps the bank */
    CHECK(n64c_pi_register_image(OVL,OVL+sizeof setups,(const u8 *)setups)==0);
    CHECK(n64c_pi_register(OVL+4,OVL+8,N64C_RES_SWAP32,NULL)==-1);               /* overlaps the image */
    u8 window[sizeof setups+8];
    memset(window,0xEE,sizeof window);
    CHECK(osPiStartDma(NULL,0,OS_READ,OVL-4,window,sizeof window,&queue)==0);
    event(1);
    CHECK(window[3]==0x5A && window[4+sizeof setups]==0xA5);                       /* outside: ROM bytes */
    CHECK(memcmp(window+4,setups,sizeof setups)==0);
    Setup parts[2];                                                                /* a DMA cutting a field */
    CHECK(osPiStartDma(NULL,0,OS_READ,OVL,parts,6,&queue)==0);
    event(1);
    CHECK(osPiStartDma(NULL,0,OS_READ,OVL+6,(u8 *)parts+6,sizeof parts-6,&queue)==0);
    event(1);
    CHECK(memcmp(parts,setups,sizeof setups)==0 && parts[0].behaviour==5 && parts[0].gravity==-2304.0f);
    /* Splitting one entry in a full provenance history must evict the oldest
     * fragment without corrupting the ROM handle or losing the newest origin. */
    {
        enum { ORIGIN_CAPACITY=1024 };
        static u8 buffers[ORIGIN_CAPACITY][4];
        u32 source;
        for (unsigned index=0;index<ORIGIN_CAPACITY;index++) {
            CHECK(osPiStartDma(NULL,0,OS_READ,TEXT,buffers[index],sizeof buffers[index],&queue)==0);
            event(1);
        }
        n64c_pi_forget(buffers[0]+1,1);
        CHECK(!n64c_pi_origin(buffers[0],&source));
        CHECK(!n64c_pi_origin(buffers[0]+1,&source));
        CHECK(n64c_pi_origin(buffers[0]+2,&source) && source==TEXT+2);
        CHECK(n64c_pi_origin(buffers[ORIGIN_CAPACITY-1]+3,&source) && source==TEXT+3);
        CHECK(osPiStartDma(NULL,0,OS_READ,TEXT,scratch,4,&queue)==0);
        event(1);
        CHECK(memcmp(scratch,"Disp",4)==0);
        CHECK(!n64c_pi_origin(buffers[0]+2,&source));
        CHECK(n64c_pi_origin(buffers[ORIGIN_CAPACITY-1],&source) && source==TEXT);
    }
    n64c_shutdown();
    CHECK(remove(path)==0);
    printf("PI resource descriptors: %d checks; swap32 header, image and font records, glyphs, offsets, map, audio table, directory, raw text, tagged widget pages, audio control bank, overlay image, refusals\n",checks);
    return 0;
}
