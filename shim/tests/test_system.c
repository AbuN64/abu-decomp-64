/* SPDX-License-Identifier: GPL-3.0-or-later */
#define _GNU_SOURCE
#include "n64c_system.h"
#include <fenv.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>
#include <xmmintrin.h>
static unsigned checks;
#define CHECK(x) do { checks++; if (!(x)) { fprintf(stderr,"FAIL:%d:%s\n",__LINE__,#x); exit(1); } } while (0)
static u32 header=15;
static int io_fail;
s32 osPiReadIo(u32 address,u32 *value) {
    CHECK(address==4);
    if (io_fail) return -1;
    *value=header;
    return 0;
}
static u32 initialized,tv,reset,mem,video;
static u64 rate;
static u8 buffer[66];
static const N64cSystemGlobals globals={&initialized,&tv,&reset,&mem,&video,&rate,buffer+1};

static void failure(int kind,const char *message,int signal_number)
{
    int fd[2];CHECK(pipe(fd)==0);
    pid_t pid=fork();CHECK(pid>=0);
    if (!pid) {
        close(fd[0]);dup2(fd[1],2);close(fd[1]);
        if (kind==1) n64c_system_reset();
        if (kind==2) header=50000000;
        if (kind==3) io_fail=1;
        if (kind==4) tv=3;
        n64c_system_initialize();
        if (kind==5) { volatile float zero=0; volatile float invalid=zero/zero;(void)invalid; }
        _Exit(1);
    }
    close(fd[1]);char text[160]={0};ssize_t bytes=read(fd[0],text,sizeof text-1);close(fd[0]);CHECK(bytes>=0);
    int status;CHECK(waitpid(pid,&status,0)==pid);
    if (signal_number) CHECK(WIFSIGNALED(status) && WTERMSIG(status)==signal_number);
    else { CHECK(WIFEXITED(status) && WEXITSTATUS(status)==78);CHECK(strcmp(text,message)==0); }
}
static void *refuse(void *target, const void *text, s32 count) { (void)target; (void)text; (void)count; return NULL; }
static s32 call_printf(void *(*emit)(void *, const void *, s32), void *target, const char *format, ...)
{
    va_list args;
    va_start(args, format);
    s32 count = n64c_printf(emit, target, format, args);
    va_end(args);
    return count;
}
int main(void)
{
    char formatted[160];
    memset(formatted, 0x5A, sizeof formatted);
    const char *expected = "port / -17 / 002a / 4294967298 / 1.25 / %";
    s32 written = n64c_sprintf(formatted, "%s / %d / %04x / %lld / %.2f / %%",
        "port", -17, 42u, 4294967298LL, 1.25);
    CHECK(written == (s32)strlen(expected));
    CHECK(strcmp(formatted, expected) == 0 && formatted[written + 1] == 0x5A);
    CHECK(n64c_sprintf(formatted, "%.*s", 0, "unused") == 0 && formatted[0] == 0);

    {
        char out[600], big[1200];
        memset(out, 0x5A, sizeof out);
        va_list none;
        (void)none;
        CHECK(call_printf(n64c_printf_copy, out, "%s=%d", "k", 7) == 3 && memcmp(out, "k=7", 3) == 0 && out[3] == 0x5A);
        CHECK(call_printf(n64c_printf_copy, big, "%1000d", 5) == 1000 && big[999] == '5' && big[0] == ' ');
        CHECK(call_printf(refuse, out, "x") == -1);
        CHECK(call_printf(refuse, out, "") == 0);
        CHECK(strcmp((char *)n64c_string_find((u8 *)"a b", ' '), " b") == 0 && n64c_string_find((u8 *)"ab", 'z') == NULL);
        u8 word[] = "ab";
        CHECK(n64c_string_find(word, 0) == word + 2);
    }
    CHECK(n64c_floatdidf(-4294967298LL)==-4294967298.0 && n64c_floatdidf(0)==0.0 && n64c_floatdidf(INT64_MIN)==-9223372036854775808.0);
    CHECK(n64c_floatdidf(4294967295LL)==4294967295.0 && n64c_floatdidf(1LL<<40)==1099511627776.0);
    {
        u8 source[5]={1,2,3,4,5},target[7];
        memset(target,0xEE,sizeof target);
        CHECK(n64c_copy_bytes(target+1,source,5)==target+1 && memcmp(target+1,source,5)==0 && target[0]==0xEE && target[6]==0xEE);
        CHECK(n64c_copy_bytes(target,source,0)==target && target[0]==0xEE);
        CHECK(n64c_string_length((const u8 *)"")==0 && n64c_string_length((const u8 *)"abc")==3);
        int token;
        CHECK(n64c_dev_handler()==NULL);
        n64c_dev_handler_set(&token);
        CHECK(n64c_dev_handler()==&token);
    }
    CHECK(fesetround(FE_DOWNWARD)==0);
    unsigned host_csr=_mm_getcsr();int host_excepts=fegetexcept();
    memset(buffer,0xA5,sizeof buffer);
    CHECK(!n64c_system_bind(NULL,1,0,0x800000));
    CHECK(!n64c_system_bind(&globals,3,0,0x800000));
    CHECK(!n64c_system_bind(&globals,1,2,0x800000));
    CHECK(!n64c_system_bind(&globals,1,0,0x100000));
    CHECK(n64c_system_bind(&globals,1,0,0x800000));
    CHECK(!n64c_system_bind(&globals,0,1,0x400000));
    CHECK(tv==1 && reset==0 && mem==0x800000);
    n64c_system_initialize();
    CHECK(initialized==1 && rate==46875000 && video==48681812);
    CHECK(buffer[0]==0xA5 && buffer[65]==0xA5);
    for (int i=1;i<65;i++) CHECK(buffer[i]==0);
    CHECK(fegetround()==FE_TONEAREST && fegetexcept()==FE_INVALID);
    CHECK((_mm_getcsr() & ((1u<<15)|(1u<<6)))==((1u<<15)|(1u<<6)));
    volatile float tiny=1.0e-40f,one=1.0f,left=1.0e-30f,right=1.0e-10f;
    volatile float input=tiny*one,output=left*right;
    CHECK(input==0 && output==0);
    failure(1,"boot-missing: system-globals\n",0);
    failure(2,"boot-missing: system-counter-rate\n",0);
    failure(3,"boot-missing: system-ROM-clock\n",0);
    failure(4,"boot-missing: system-boot-values\n",0);
    failure(5,"",SIGFPE);
    n64c_system_reset();
    CHECK(_mm_getcsr()==host_csr && fegetround()==FE_DOWNWARD && fegetexcept()==host_excepts);
    const u32 clocks[]={49656530,48681812,48628316};
    for (u32 type=0;type<3;type++) {
        memset(buffer,0x5A,sizeof buffer);initialized=0;header=62500015;
        CHECK(n64c_system_bind(&globals,type,1,0x400000));
        n64c_system_initialize();
        CHECK(initialized==1 && video==clocks[type] && rate==OS_CPU_COUNTER && mem==0x400000);
        for (int i=0;i<66;i++) CHECK(buffer[i]==0x5A);
        n64c_system_initialize();CHECK(rate==OS_CPU_COUNTER);
        n64c_system_reset();
        CHECK(_mm_getcsr()==host_csr);
    }
    printf("system checks=%u\n",checks);
    return 0;
}
