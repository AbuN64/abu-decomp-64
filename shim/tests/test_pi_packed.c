/* SPDX-License-Identifier: GPL-3.0-or-later */
/* RNC streams through the PI layer (n64c_pi_register_packed): the header reaches RAM in host order even when a DMA
 * cuts it, the packed bytes stay raw, the DMA's origin is remembered, and the unpack wrappers type the unpacked
 * bytes of a stream they trace back to its ROM range: a STRUCT layout (method 1) and a GRAPH container (method 2:
 * a directory of a pool and a leaf of records whose offsets type pool records, one shared, one null). A ROM range
 * typed by a GRAPH (an uncompressed container), malformed programs and a layout that does not fit are refused. */
#include "../n64c_os.h"
#include "../n64c_pi.h"
#include "../n64c_rnc.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

static int checks;
#define CHECK(value) do { checks++; if (!(value)) { fprintf(stderr,"FAIL packed:%d: %s\n",__LINE__,#value); exit(1); } } while (0)
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
static void dma(u32 rom,void *ram,u32 bytes)
{
    OSMesg message;
    CHECK(osPiStartDma(NULL,0,OS_READ,rom,ram,bytes,&queue)==0);
    CHECK(osRecvMesg(&queue,&message,OS_MESG_NOBLOCK)==0);
}
/* RNC1 stream unpacking to "abcdddd" (the test_rnc.c writer: a literal run of 4, a match of 3 at distance 1). */
static u8 rnc1[64];
static int pos1,slot1,count1;
static void emit(unsigned v,int n)
{
    for (int i=0;i<n;i++) {
        if (!count1) { slot1=pos1; pos1+=2; rnc1[slot1]=rnc1[slot1+1]=0; }
        if ((v>>i)&1) rnc1[slot1+count1/8]|=(u8)(1u<<(count1%8));
        count1=(count1+1)%16;
    }
}
static int stream1(void)
{
    pos1=18;
    emit(0,2);
    emit(4,5); emit(1,4); emit(0,4); emit(0,4); emit(1,4);
    emit(1,5); emit(1,4);
    emit(2,5); emit(0,4); emit(1,4);
    emit(2,16);
    emit(1,1); emit(0,2); memcpy(rnc1+pos1,"abcd",4); pos1+=4;
    emit(0,1); emit(0,1);
    emit(0,1);
    memcpy(rnc1,"RNC\1",4); be32(rnc1+4,7); be32(rnc1+8,(u32)pos1-18);
    be16(rnc1+12,n64c_rnc_crc16("abcdddd",7)); be16(rnc1+14,n64c_rnc_crc16(rnc1+18,(u32)pos1-18));
    rnc1[16]=0; rnc1[17]=1;
    return pos1;
}
/* RNC2 stream of literals only: a flag bit 0 then the byte, MSB-first flag bytes. */
static u8 rnc2[256];
static int pos2,slot2,count2;
static void bits2(unsigned v,int n)
{
    for (int i=n-1;i>=0;i--) {
        if (!count2) { slot2=pos2++; rnc2[slot2]=0; }
        if ((v>>i)&1) rnc2[slot2]|=(u8)(0x80u>>count2);
        count2=(count2+1)%8;
    }
}
static int stream2(const u8 *bytes,int size)
{
    pos2=18; count2=0;
    bits2(0,2);
    for (int i=0;i<size;i++) { bits2(0,1); rnc2[pos2++]=bytes[i]; }
    bits2(15,4); rnc2[pos2++]=0; bits2(0,1);
    memcpy(rnc2,"RNC\2",4); be32(rnc2+4,(u32)size); be32(rnc2+8,(u32)pos2-18);
    be16(rnc2+12,n64c_rnc_crc16(bytes,(u32)size)); be16(rnc2+14,n64c_rnc_crc16(rnc2+18,(u32)pos2-18));
    rnc2[16]=0; rnc2[17]=1;
    return pos2;
}
/* The container: directory {count 2, pool at 16, leaf at 32, end 64}; pool: raw filler, Pair {u32, u16, u16} at 4,
 * Colour {4 bytes} at 12; leaf {stride 12, count 2}: {u16, u16, @Pair, @Colour}, the second sharing the Pair and
 * with a null Colour. */
enum { DIR=0, POOL=16, LEAF=32, SIZE=64 };
static const char program[]="D:-,1;L:2;F:2 2 @0:3 @0:4;F:4 2 2;F:1 1 1 1";
static void container(u8 *c)
{
    memset(c,0,SIZE);
    be32(c,2); be32(c+4,POOL); be32(c+8,LEAF); be32(c+12,SIZE);
    be32(c+POOL,0xA1A2A3A4u);
    be32(c+POOL+4,0x11223344u); be16(c+POOL+8,0x5566); be16(c+POOL+10,0x7788);
    c[POOL+12]=1; c[POOL+13]=2; c[POOL+14]=3; c[POOL+15]=4;
    be32(c+LEAF,12); be32(c+LEAF+4,2);
    be16(c+LEAF+8,0x0102); be16(c+LEAF+10,0x0304); be32(c+LEAF+12,4); be32(c+LEAF+16,12);
    be16(c+LEAF+20,0x0506); be16(c+LEAF+22,0x0708); be32(c+LEAF+24,4); be32(c+LEAF+28,0);
}
static void check_container(const u8 *c)
{
    u32 w; u16 h;
    memcpy(&w,c,4); CHECK(w==2);
    memcpy(&w,c+12,4); CHECK(w==SIZE);
    CHECK(c[POOL]==0xA1 && c[POOL+3]==0xA4);                                 /* reached by no type: raw */
    memcpy(&w,c+POOL+4,4); CHECK(w==0x11223344u);
    memcpy(&h,c+POOL+10,2); CHECK(h==0x7788);
    CHECK(c[POOL+12]==1 && c[POOL+15]==4);
    memcpy(&w,c+LEAF,4); CHECK(w==12);
    memcpy(&h,c+LEAF+8,2); CHECK(h==0x0102);
    memcpy(&w,c+LEAF+16,4); CHECK(w==12);
    memcpy(&h,c+LEAF+22,2); CHECK(h==0x0708);
    memcpy(&w,c+LEAF+28,4); CHECK(w==0);
}
static int exits78(void (*body)(void))
{
    pid_t pid=fork();
    CHECK(pid>=0);
    if (!pid) { close(2); body(); _Exit(0); }
    int status;
    CHECK(waitpid(pid,&status,0)==pid);
    return WIFEXITED(status) && WEXITSTATUS(status)==78;
}
/* Tagged records and a head + elements record: directory {count 2, a leaf (stride 8, count 2) of T records whose
 * u16 tag picks "2 2 4" or "2 1 1 2 2", a head u32 then u16 elements to the end offset 50}. */
static const char program2[]="D:1,2;L:3;R:4,5;T:0/2:1=6,2=7;F:4;F:2;F:2 2 4;F:2 1 1 2 2";
static void container2(u8 *c)
{
    be32(c,2); be32(c+4,16); be32(c+8,40); be32(c+12,50);
    be32(c+16,8); be32(c+20,2);
    be16(c+24,1); be16(c+26,0x0203); be32(c+28,0x04050607u);
    be16(c+32,2); c[34]=8; c[35]=9; be16(c+36,0x0A0B); be16(c+38,0x0C0D);
    be32(c+40,0x11121314u); be16(c+44,0x2122); be16(c+46,0x2324); be16(c+48,0x2526);
}
enum { S1=0x100, S2=0x200, BAD=0x400, ROMGRAPH=0x500, MISFIT=0x600, ROMGRAPH2=0x680, NEST=0x740, PLANES=0x7A0,
       PLANES2=0x800, UNTYPED=0x870 };
static u8 ram[512];
static void untyped_dma(void)
{
    dma(UNTYPED,ram,8);
}
static void untyped_stream(void)
{
    dma(PLANES2,ram,8);
}
static void misfit(void)
{
    u8 out[8];
    dma(MISFIT,ram,(u32)pos1);
    n64c_pi_rnc1_unpack(ram+18,out,pos1-18,7);
}
int main(void)
{
    const char *path="n64c_packed_test.rom";
    static u8 rom[0x8B0];
    u8 c[SIZE];
    int size1=stream1();
    memcpy(rom+S1,rnc1,(size_t)size1);
    memcpy(rom+MISFIT,rnc1,(size_t)size1);
    container(c);
    int size2=stream2(c,SIZE);
    CHECK(S2+size2<=BAD);
    memcpy(rom+S2,rnc2,(size_t)size2);
    memcpy(rom+BAD,"RNX\1",4);
    container(rom+ROMGRAPH);
    container2(rom+ROMGRAPH2);
    /* a ROM container holding an RNC2 stream (P type) whose unpacked bytes are a leaf of two u16 */
    u8 leaf[12];
    be32(leaf,2); be32(leaf+4,2); be16(leaf+8,0x0102); be16(leaf+10,0x0304);
    int size3=stream2(leaf,sizeof leaf);
    CHECK(NEST+16+size3<=(int)sizeof rom);
    be32(rom+NEST,1); be32(rom+NEST+4,16); be32(rom+NEST+8,16+(u32)size3);
    memcpy(rom+NEST+16,rnc2,(size_t)size3);
    /* an RNC2 stream of a leaf stored as byte planes: two records {u16, u32}, plane k = byte k of each record */
    static const u8 records[2][6]={{1,2,3,4,5,6},{0x11,0x12,0x13,0x14,0x15,0x16}};
    u8 planes[20];
    be32(planes,6); be32(planes+4,2);
    for (int k=0;k<6;k++) for (int i=0;i<2;i++) planes[8+k*2+i]=records[i][k];
    int size4=stream2(planes,sizeof planes);
    CHECK(PLANES+size4<=(int)sizeof rom);
    memcpy(rom+PLANES,rnc2,(size_t)size4);
    CHECK(PLANES2+size4<=UNTYPED-8);
    memcpy(rom+PLANES2,rnc2,(size_t)size4);
    FILE *file=fopen(path,"wb");
    CHECK(file && fwrite(rom,1,sizeof rom,file)==sizeof rom && fclose(file)==0);
    N64cConfig config={.rom_path=path};
    n64c_init(&config);
    osCreateMesgQueue(&queue,messages,4);
    CHECK(n64c_pi_register_packed(S1,S1+(u32)size1,N64C_RES_STRUCT,"2 2 2 1")==0);
    CHECK(n64c_pi_register_packed(S2,S2+(u32)size2,N64C_RES_GRAPH,program)==0);
    CHECK(n64c_pi_register_packed(BAD,BAD+32,N64C_RES_RAW,NULL)==-1);                  /* no RNC header */
    CHECK(n64c_pi_register_packed(S1+4,S1+40,N64C_RES_RAW,NULL)==-1);                  /* overlaps a stream */
    CHECK(n64c_pi_register_packed(MISFIT,MISFIT+(u32)size1,N64C_RES_GRAPH,"F:")==-1);  /* empty record */
    CHECK(n64c_pi_register_packed(MISFIT,MISFIT+(u32)size1,N64C_RES_GRAPH,"L:1;D:0")==-1); /* leaf of a directory */
    CHECK(n64c_pi_register_packed(MISFIT,MISFIT+(u32)size1,N64C_RES_GRAPH,"F:4 @0:7")==-1); /* no type 7 */
    CHECK(n64c_pi_register_packed(MISFIT,MISFIT+(u32)size1,N64C_RES_STRUCT,"4")==0);   /* 7 bytes: no fit */
    CHECK(n64c_pi_register(ROMGRAPH,ROMGRAPH+SIZE,N64C_RES_GRAPH,program)==0);
    CHECK(n64c_pi_register(0x700,0x740,N64C_RES_GRAPH,"R:1,-;F:4")==-1);                  /* an untyped part */
    CHECK(n64c_pi_register(0x700,0x740,N64C_RES_GRAPH,"T:0/2:1=1;L:0")==-1);              /* a variant leaf */
    CHECK(n64c_pi_register(ROMGRAPH2,ROMGRAPH2+50,N64C_RES_GRAPH,program2)==0);
    CHECK(n64c_pi_register(0x700,0x740,N64C_RES_GRAPH,"P:-")==-1);                      /* no RNC stream there */
    CHECK(n64c_pi_register(NEST,NEST+16+(u32)size3,N64C_RES_GRAPH,"D:1;P:2;L:3;F:2")==0);
    CHECK(n64c_pi_register(0x700,0x740,N64C_RES_GRAPH,"X:1;F:4")==-1);                  /* planes in a ROM range */
    CHECK(n64c_pi_register_packed(MISFIT,MISFIT+(u32)size1,N64C_RES_GRAPH,"X:1;F:4 @0:1")==-1); /* planes: no refs */
    CHECK(n64c_pi_register_packed(PLANES,PLANES+(u32)size4,N64C_RES_GRAPH,"X:1;F:2 4")==0);
    CHECK(n64c_pi_register(UNTYPED,UNTYPED+0x40,N64C_RES_UNTYPED,NULL)==-1);           /* the gate names a blob */
    CHECK(n64c_pi_register(UNTYPED,UNTYPED+0x40,N64C_RES_UNTYPED,"blob.x")==0);
    CHECK(n64c_pi_register_packed(PLANES2,PLANES2+(u32)size4,N64C_RES_UNTYPED,"blob.y")==0);
    /* the header in host order, the packed bytes raw, also when a DMA cuts the header */
    u32 w; u16 h;
    dma(S1,ram,6);
    dma(S1+6,ram+6,(u32)size1-6);
    memcpy(&w,ram,4); CHECK(w==0x524E4301u);
    memcpy(&w,ram+4,4); CHECK(w==7);
    memcpy(&w,ram+8,4); CHECK(w==(u32)size1-18);
    memcpy(&h,ram+14,2); CHECK(h==n64c_rnc_crc16(rnc1+18,(u32)size1-18));
    CHECK(ram[17]==1 && memcmp(ram+18,rnc1+18,(size_t)size1-18)==0);
    CHECK(n64c_pi_origin(ram+18,&w) && w==S1+18);
    u8 out[SIZE+8];
    memset(out,0,sizeof out);
    n64c_pi_rnc1_unpack(ram+18,out,size1-18,7);
    CHECK(memcmp(out,"badcddd",7)==0);                                                 /* "2 2 2 1" */
    CHECK(!n64c_pi_origin(out,&w));                                                    /* unpacked: no origin */
    u8 copy[64];                                                                       /* untraced: raw */
    memcpy(copy,ram,(size_t)size1);
    n64c_pi_forget(ram,sizeof ram);
    CHECK(!n64c_pi_origin(ram+18,&w));
    n64c_pi_rnc1_unpack(copy+18,out,size1-18,7);
    CHECK(memcmp(out,"abcdddd",7)==0);
    dma(S2,ram,(u32)size2);                                                            /* method 2, GRAPH */
    memcpy(&w,ram,4); CHECK(w==0x524E4302u);
    memset(out,0,sizeof out);
    n64c_pi_rnc2_unpack(ram,out,size2-18,SIZE);
    check_container(out);
    u8 window[SIZE];                                                                   /* ROM GRAPH */
    dma(ROMGRAPH,window,SIZE);
    check_container(window);
    u8 tagged[50];
    dma(ROMGRAPH2,tagged,50);
    memcpy(&h,tagged+24,2); CHECK(h==1);
    memcpy(&h,tagged+26,2); CHECK(h==0x0203);
    memcpy(&w,tagged+28,4); CHECK(w==0x04050607u);
    memcpy(&h,tagged+32,2); CHECK(h==2);
    CHECK(tagged[34]==8 && tagged[35]==9);
    memcpy(&h,tagged+38,2); CHECK(h==0x0C0D);
    memcpy(&w,tagged+40,4); CHECK(w==0x11121314u);
    memcpy(&h,tagged+48,2); CHECK(h==0x2526);
    dma(NEST+16,ram,(u32)size3);                                                     /* nested stream */
    memcpy(&w,ram,4); CHECK(w==0x524E4302u);
    memcpy(&w,ram+4,4); CHECK(w==sizeof leaf);
    memset(out,0,sizeof out);
    n64c_pi_rnc2_unpack(ram,out,size3-18,sizeof leaf);
    memcpy(&w,out,4); CHECK(w==2);
    memcpy(&h,out+8,2); CHECK(h==0x0102);
    memcpy(&h,out+10,2); CHECK(h==0x0304);
    dma(PLANES,ram,(u32)size4);                                                       /* byte planes */
    memset(out,0,sizeof out);
    n64c_pi_rnc2_unpack(ram,out,size4-18,sizeof planes);
    memcpy(&w,out,4); CHECK(w==6);
    memcpy(&w,out+4,4); CHECK(w==2);
    u8 built[12];                                                                      /* the game's transpose */
    for (int i=0;i<2;i++) for (int k=0;k<6;k++) built[i*6+k]=out[8+k*2+i];
    memcpy(&h,built,2); CHECK(h==0x0102);
    memcpy(&w,built+2,4); CHECK(w==0x03040506u);
    memcpy(&h,built+6,2); CHECK(h==0x1112);
    memcpy(&w,built+8,4); CHECK(w==0x13141516u);
    CHECK(exits78(misfit));
    dma(UNTYPED-8,ram,16);                                                             /* passes over: raw */
    CHECK(memcmp(ram+8,rom+UNTYPED,8)==0);
    CHECK(exits78(untyped_dma));                                                       /* the byte-order gate */
    CHECK(exits78(untyped_stream));
    CHECK(setenv("N64C_LAYOUT_GATE","log",1)==0);                                      /* diagnostic: raw */
    dma(UNTYPED,ram,8);
    CHECK(memcmp(ram,rom+UNTYPED,8)==0);
    dma(PLANES2,ram,(u32)size4);
    memset(out,0,sizeof out);
    n64c_pi_rnc2_unpack(ram,out,size4-18,sizeof planes);
    CHECK(memcmp(out,planes,sizeof planes)==0);
    CHECK(unsetenv("N64C_LAYOUT_GATE")==0);
    n64c_shutdown();
    CHECK(remove(path)==0);
    printf("PI packed streams: %d checks; host-order headers (cut DMA), raw packed bytes, origins, method 1 STRUCT, "
           "method 2 GRAPH (pool references, shared and null), ROM GRAPH (tagged variants, head + elements, a nested "
           "stream), "
           "byte-plane leaves, the untyped-blob gate (stop and log), untraced raw, refusals\n",checks);
    return 0;
}
