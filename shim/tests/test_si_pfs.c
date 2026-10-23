/* SPDX-License-Identifier: GPL-3.0-or-later */
/* SI access lock, controller re-scan, Controller Pak and Rumble Pak answers. Expected values are the documented
 * libultra results for an empty slot (PFS_ERR_NOPACK) and an uninitialised handle (PFS_ERR_INVALID); with a card in
 * the slot the file system answers (tests/test_cpak.c walks it). */
#include "../n64c_os.h"
#include "../n64c_cont.h"
#include "../n64c_pfs.h"
#include "../n64c_si.h"
#include <stdio.h>
#include <string.h>
static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)
static char trace[64];
static void note(char c) { size_t n = strlen(trace); trace[n] = c; trace[n + 1] = 0; }
static OSThread th[2];
static void holder(void *p)
{
    (void)p;
    __osSiGetAccess();
    note('a');
    osYieldThread(); /* the other thread is runnable and must still be blocked on the lock */
    note('b');
    __osSiRelAccess();
}
static void waiter(void *p)
{
    (void)p;
    __osSiGetAccess();
    note('w');
    __osSiRelAccess();
}
static void run_pak(const char *path)
{
    N64cConfig cfg = {.pak_path = path};
    OSMesgQueue mq; OSMesg buf[4];
    OSPfs pfs;
    OSContStatus st[MAXCONTROLLERS];
    u8 name[16] = {0}, ext[4] = {0};
    s32 n = 77;
    OSPfsState state;
    n64c_init(&cfg);
    osCreateMesgQueue(&mq, buf, 4);
    memset(&pfs, 0xAB, sizeof pfs);
    CHECK(sizeof(OSPfs) == 0x68 || sizeof(void *) != 4);
    if (!path) {
        CHECK(__osPfsGetStatus(&mq, 0) == PFS_ERR_NOPACK);
        CHECK(osPfsInitPak(&mq, &pfs, 0) == PFS_ERR_NOPACK);
        CHECK(pfs.queue == &mq && pfs.channel == 0 && pfs.status == 0 && pfs.activebank == 0xFF);
        CHECK(osMotorInit(&mq, &pfs, 0) == PFS_ERR_NOPACK);
    } else {
        CHECK(__osPfsGetStatus(&mq, 0) == 0);
        CHECK(osPfsInitPak(&mq, &pfs, 0) == 0 && pfs.status == PFS_INITIALIZED && pfs.dir_size == 16);
        CHECK(osMotorInit(&mq, &pfs, 0) == PFS_ERR_DEVICE);
    }
    CHECK(__osPfsGetStatus(&mq, 1) == PFS_ERR_NOPACK && __osPfsGetStatus(&mq, 7) == PFS_ERR_NOPACK);
    CHECK(osPfsInitPak(&mq, &pfs, 3) == PFS_ERR_NOPACK && pfs.channel == 3);
    /* a handle that never initialised cannot name files, and the motor is not driven */
    pfs.status = 0;
    CHECK(osPfsFileState(&pfs, 0, &state) == PFS_ERR_INVALID);
    CHECK(osPfsFreeBlocks(&pfs, &n) == PFS_ERR_INVALID && n == 77);
    CHECK(osPfsReadWriteFile(&pfs, 0, 0, 0, 32, (u8 *)&state) == PFS_ERR_INVALID);
    CHECK(osPfsDeleteFile(&pfs, 1, 2, name, ext) == PFS_ERR_INVALID);
    CHECK(osPfsAllocateFile(&pfs, 1, 2, name, ext, 256, &n) == PFS_ERR_INVALID && n == 77);
    CHECK(osPfsFindFile(&pfs, 1, 2, name, ext, &n) == PFS_ERR_INVALID && n == 77);
    CHECK(osMotorStart(&pfs) == PFS_ERR_INVALID && osMotorStop(&pfs) == PFS_ERR_INVALID);
    pfs.queue = &mq; pfs.channel = 0;
    CHECK((n64c_pfs_check_id(&pfs) != 0) == (path == NULL));
    /* controller re-scan: status table, no message left behind */
    memset(st, 0xEE, sizeof st);
    CHECK(n64c_cont_rescan(&mq, st) == 0);
    CHECK(st[0].type == 0x0005 && st[0].errno_ == 0 && ((st[0].status & CONT_CARD_ON) != 0) == (path != NULL));
    CHECK(st[1].errno_ == CONT_ERR_NO_CONTROLLER && st[3].errno_ == CONT_ERR_NO_CONTROLLER);
    {
        OSMesg m;
        CHECK(osRecvMesg(&mq, &m, OS_MESG_NOBLOCK) == -1);
    }
    n64c_shutdown();
}
int main(void)
{
    N64cConfig cfg = {0};
    n64c_init(&cfg);
    /* the lock: a second taker blocks until the first gives it back */
    osCreateThread(&th[0], 1, holder, 0, 0, 10);
    osCreateThread(&th[1], 2, waiter, 0, 0, 10);
    osStartThread(&th[0]); osStartThread(&th[1]);
    n64c_run_idle();
    CHECK(strcmp(trace, "abw") == 0);
    __osSiGetAccess(); __osSiRelAccess(); __osSiGetAccess(); __osSiRelAccess(); /* free again, no hang */
    n64c_shutdown();
    n64c_init(&cfg);
    __osSiGetAccess(); /* shutdown forgets a lock that was held */
    n64c_shutdown();
    n64c_init(&cfg);
    __osSiGetAccess(); __osSiRelAccess();
    n64c_shutdown();
    run_pak(NULL);
    run_pak("n64c_test_si_pak.bin");
    remove("n64c_test_si_pak.bin");
    if (fails) { printf("%d failures\n", fails); return 1; }
    printf("test_si_pfs ok\n");
    return 0;
}
