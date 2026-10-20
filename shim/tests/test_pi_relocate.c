/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "../n64c_os.h"
#include "../n64c_pi.h"
#include "../n64c_memory.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int checks,bindings,allow_extra;
static u32 values[]={11,22};
#define CHECK(value) do { checks++; if (!(value)) {     fprintf(stderr,"FAIL relocate:%d: %s\n",__LINE__,#value); exit(1); } } while (0)
typedef struct { u32 number,pointer; u16 half; u8 a,b; } Record;
_Static_assert(sizeof(Record)==12,"serialized native32-bit record");
#if UINTPTR_MAX==UINT32_MAX
static int function(void) { return 37; }
#endif
static u32 data_address(void)
{
#if UINTPTR_MAX==UINT32_MAX
    return (u32)(uintptr_t)&values[1];
#else
    return 0x12345004;
#endif
}
static u32 function_address(void)
{
#if UINTPTR_MAX==UINT32_MAX
    return (u32)(uintptr_t)function;
#else
    return 0x45670000;
#endif
}
static int resolve(u32 original,u32 *native,void *user)
{
    CHECK(user==values);
    bindings++;
    if (original==0x80200004) { *native=data_address(); return 1; }
    if (original==0x80300000) { *native=function_address(); return 1; }
    if (original==0x80400000 && allow_extra) { *native=data_address(); return 1; }
    return 0;
}
static void be32(u8 *where,u32 value)
{
    where[0]=(u8)(value>>24);where[1]=(u8)(value>>16);
    where[2]=(u8)(value>>8);where[3]=(u8)value;
}
static OSMesgQueue queue;
static OSMesg messages[4];
static void event(int success)
{
    OSMesg message;
    CHECK((osRecvMesg(&queue,&message,OS_MESG_NOBLOCK)==0)==success);
}
int main(void)
{
    const char *path="n64c_relocate_test.rom";
    u8 image[256]={0};
    be32(image+0x20,0x11223344);be32(image+0x24,0x80200004);
    image[0x28]=0x55;image[0x29]=0x66;image[0x2A]=0xAA;image[0x2B]=0xBB;
    be32(image+0x2C,9);image[0x34]=0x77;image[0x35]=0x88;
    image[0x36]=0xCC;image[0x37]=0xDD;
    be32(image+0x60,0x80300000);be32(image+0x70,0x80400000);
    image[0x90]=0x5A;
    FILE *file=fopen(path,"wb");
    CHECK(file && fwrite(image,1,sizeof image,file)==sizeof image && fclose(file)==0);
    N64cConfig config={.rom_path=path};
    n64c_init(&config);
    osCreateMesgQueue(&queue,messages,4);
    for (unsigned i=0;i<300;i++)
        CHECK(n64c_pi_register(0x20,0x38,N64C_RES_STRUCT,NULL)==-1);
    CHECK(n64c_pi_register(0x20,0x38,N64C_RES_STRUCT,"")==-1);
    CHECK(n64c_pi_register(0x20,0x23,N64C_RES_SWAP16,NULL)==-1);
    CHECK(n64c_pi_register(0x20,0x40,N64C_RES_STRUCT,"1111111111111111111111111111111111111111111111111111111111111111")==-1);
    CHECK(n64c_pi_register(0x20,0x38,(N64cResKind)99,NULL)==-1);
    CHECK(n64c_pi_register(0x20,0x38,N64C_RES_STRUCT,"4p2 11")==0);
    CHECK(n64c_pi_register(0x24,0x2C,N64C_RES_SWAP32,NULL)==-1);
    CHECK(n64c_pi_register(0x60,0x64,N64C_RES_STRUCT,"p")==0);
    CHECK(n64c_pi_register(0x70,0x74,N64C_RES_STRUCT,"p")==0);
    Record records[2];
    CHECK(osPiStartDma(NULL,0,OS_READ,0x20,records,sizeof records,&queue)==-1);
    event(0);
    n64c_pi_set_relocator(resolve,values);
    CHECK(osPiStartDma(NULL,0,OS_READ,0x20,records,sizeof records,&queue)==0);
    event(1);
    CHECK(records[0].number==0x11223344 && records[0].pointer==data_address() &&
        records[0].half==0x5566 && records[0].a==0xAA && records[0].b==0xBB);
    CHECK(records[1].number==9 && records[1].pointer==0 && records[1].half==0x7788 &&
        records[1].a==0xCC && records[1].b==0xDD && bindings==1);
    u32 entry;
    CHECK(osPiStartDma(NULL,0,OS_READ,0x60,&entry,4,&queue)==0);
    event(1);
    CHECK(entry==function_address() && bindings==2);
#if UINTPTR_MAX==UINT32_MAX
    int (*call)(void);
    memcpy(&call,&entry,4);
    CHECK(call()==37 && *(u32 *)(uintptr_t)records[0].pointer==22);
#endif
    CHECK(osPiStartDma(NULL,0,OS_READ,0x70,&entry,4,&queue)==-1);
    event(0);
    CHECK(osPiStartDma(NULL,0,OS_READ,0x25,&entry,3,&queue)==-1);
    event(0);
    CHECK(osPiStartDma(NULL,0,OS_READ,0x21,&entry,3,&queue)==-1);
    event(0);
    u8 byte=0;
    CHECK(osPiStartDma(NULL,0,OS_READ,0x90,&byte,1,&queue)==0 && byte==0x5A);
    event(1);
#if defined(__linux__) && defined(__i386__)
    N64cMemoryConfig memory={0x800000,sizeof image,48681812};
    CHECK(!n64c_memory_open(&memory)); /* unresolved pointer refuses mapped ROM */
    allow_extra=1;
    CHECK(n64c_memory_open(&memory));
    CHECK(*(u32 *)(uintptr_t)0xB0000024u==data_address());
    entry=*(u32 *)(uintptr_t)0xB0000060u;
    memcpy(&call,&entry,4);
    CHECK(call()==37);
#endif
    n64c_shutdown();
    n64c_init(&config);
    osCreateMesgQueue(&queue,messages,4);
    CHECK(n64c_pi_register(0x60,0x64,N64C_RES_STRUCT,"p")==0);
    CHECK(osPiStartDma(NULL,0,OS_READ,0x60,&entry,4,&queue)==-1);
    event(0); /* previous console's resolver was released */
    n64c_shutdown();
    CHECK(remove(path)==0);
    printf("typed PI: %d checks; native data/function relocation, null, refusal, lifecycle\n",checks);
    return 0;
}
