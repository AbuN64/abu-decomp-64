/* SPDX-License-Identifier: GPL-3.0-or-later */
/* SI access lock, controller re-scan, Controller Pak and Rumble Pak answers. Expected values are the documented
 * libultra results for an empty slot (PFS_ERR_NOPACK) and an uninitialised handle (PFS_ERR_INVALID); with a card in
 * the slot the file system answers (tests/test_cpak.c walks it). Each status request and accessory packet is a bus
 * exchange, a write and a read in Count ticks (46.875 MHz): 64 us + 256 us, plus 320 us when the channel's controller
 * answers. */
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
enum { TALKING = 3000 + 27000, SILENT = 3000 + 12000 };
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
/* libultra's pfs packets per call on a formatted one-bank card, one exchange each. The first three are the counts the
 * oracle's SI log of the wl1 movie shows: osPfsInitPak 85 (status, bank select, id, label, osPfsChecker 81),
 * osPfsFileState on an empty note 2, osPfsFreeBlocks 17. */
static void pfs_packets(OSMesgQueue *mq)
{
    OSPfs pfs;
    OSPfsState state;
    u8 name[16] = "RAGEWARS", ext[4] = {0}, block[64] = {1};
    s32 n = -1;
#define COSTS(call, want, packets) do { OSTime t0 = osGetTime(); CHECK((call) == (want)); \
        CHECK(osGetTime() - t0 == (OSTime)(packets) * TALKING); } while (0)
    COSTS(osPfsInitPak(mq, &pfs, 0), 0, 85);
    COSTS(osPfsFileState(&pfs, 3, &state), PFS_ERR_INVALID, 2);
    COSTS(osPfsFreeBlocks(&pfs, &n), 0, 17);
    /* id check, search 17, free blocks 17, free note 2, inode read 16, two pages cleared 20, inode write 16, note */
    COSTS(osPfsAllocateFile(&pfs, 0x3031, 0x4E525745, name, ext, 512, &n), 0, 90);
    COSTS(osPfsFindFile(&pfs, 0x3031, 0x4E525745, name, ext, &n), 0, 2);
    COSTS(osPfsReadWriteFile(&pfs, n, 1, 0, 64, block), 0, 4);    /* id, note, two blocks */
    COSTS(osPfsReadWriteFile(&pfs, n, 0, 224, 64, block), 0, 20); /* crosses a page: the inode read */
    COSTS(osPfsFileState(&pfs, n, &state), 0, 18);
    COSTS(osPfsInitPak(mq, &pfs, 0), 0, 117); /* a named note walks its chain: its inode read and the cache's */
    COSTS(n64c_pfs_check_id(&pfs), 0, 2);
    /* id check, search 2, note, inode read, two pages summed, inode write, note cleared */
    COSTS(osPfsDeleteFile(&pfs, 0x3031, 0x4E525745, name, ext), 0, 57);
    COSTS(osPfsDeleteFile(&pfs, 0x3031, 0x4E525745, name, ext), PFS_ERR_INVALID, 18);
#undef COSTS
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
        OSTime t = osGetTime();
        CHECK(__osPfsGetStatus(&mq, 0) == PFS_ERR_NOPACK && osGetTime() - t == TALKING);
        CHECK(osPfsInitPak(&mq, &pfs, 0) == PFS_ERR_NOPACK);
        CHECK(pfs.queue == &mq && pfs.channel == 0 && pfs.status == 0 && pfs.activebank == 0xFF);
        /* the empty slot's bad CRC on the first packet, then its status request */
        t = osGetTime();
        CHECK(osMotorInit(&mq, &pfs, 0) == PFS_ERR_NOPACK && osGetTime() - t == 2 * TALKING);
    } else {
        CHECK(__osPfsGetStatus(&mq, 0) == 0);
        CHECK(osPfsInitPak(&mq, &pfs, 0) == 0 && pfs.status == PFS_INITIALIZED && pfs.dir_size == 16);
        OSTime t = osGetTime();
        CHECK(osMotorInit(&mq, &pfs, 0) == PFS_ERR_DEVICE && osGetTime() - t == 4 * TALKING); /* the four probe packets */
        pfs_packets(&mq);
    }
    /* a silent channel: one packet nobody answers; a channel out of range: no exchange */
    OSTime t = osGetTime();
    CHECK(osMotorInit(&mq, &pfs, 1) == PFS_ERR_NOPACK && osGetTime() - t == SILENT);
    t = osGetTime();
    CHECK(__osPfsGetStatus(&mq, 1) == PFS_ERR_NOPACK && __osPfsGetStatus(&mq, 7) == PFS_ERR_NOPACK);
    CHECK(osGetTime() - t == SILENT);
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
