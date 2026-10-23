/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Controller Pak: the card image, its serial-bus packets (address and data CRCs) and the file system over it,
 * including that a card persists in its file across runs and a damaged file is refused. */
#include "../n64c_os.h"
#include "../n64c_cont.h"
#include "../n64c_cpak.h"
#include "../n64c_pfs.h"
#include "../n64c_si.h"
#include <stdio.h>
#include <string.h>
static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)
static const char *path = "n64c_test_cpak.mpk";
/* One serial transaction: the packet the game builds for the SI, run through the raw DMA entry. */
static int exchange(u8 *packet)
{
    OSMesgQueue mq; OSMesg buf[4];
    osCreateMesgQueue(&mq, buf, 4);
    osSetEventMesg(OS_EVENT_SI, &mq, (OSMesg){0});
    if (__osSiRawStartDma(OS_WRITE, packet) != 0)
        return -1;
    return __osSiRawStartDma(OS_READ, packet);
}
static void field(u16 block, u8 *hi, u8 *lo)
{
    u16 f = (u16)((block << 5) | n64c_cpak_address_crc(block));
    *hi = (u8)(f >> 8); *lo = (u8)f;
}
static u64 fnv(const u8 *p, size_t n)
{
    u64 h = 1469598103934665603ull;
    while (n--) { h ^= *p++; h *= 1099511628211ull; }
    return h;
}
static void run(void)
{
    N64cConfig cfg = {.pak_path = path};
    OSMesgQueue mq; OSMesg buf[4];
    OSPfs pfs;
    OSPfsState st;
    u8 name[16] = "SAVE ONE", ext[4] = "ABC", data[256], back[256];
    s32 file = -1, free_bytes = 0;
    n64c_init(&cfg);
    osCreateMesgQueue(&mq, buf, 4);
    CHECK(n64c_cont_pak_present(0));
    /* the formatted card reads as a free card with 123 data pages */
    CHECK(osPfsInitPak(&mq, &pfs, 0) == 0);
    CHECK(osPfsFreeBlocks(&pfs, &free_bytes) == 0 && free_bytes == 123 * 256);
    CHECK(osPfsFileState(&pfs, 0, &st) == PFS_ERR_INVALID);
    CHECK(osPfsFindFile(&pfs, 0x3031, 0x4E525745, name, ext, &file) == PFS_ERR_INVALID);
    for (int i = 0; i < 256; i++) data[i] = (u8)(i * 7 + 1);
    CHECK(osPfsAllocateFile(&pfs, 0x3031, 0x4E525745, name, ext, 100, &file) == PFS_ERR_INVALID); /* not whole blocks */
    CHECK(osPfsAllocateFile(&pfs, 0x3031, 0x4E525745, name, ext, 512, &file) == 0 && file == 0);
    CHECK(osPfsAllocateFile(&pfs, 0x3031, 0x4E525745, name, ext, 512, &file) == PFS_ERR_EXIST);
    CHECK(osPfsFreeBlocks(&pfs, &free_bytes) == 0 && free_bytes == 121 * 256);
    CHECK(osPfsFileState(&pfs, 0, &st) == 0 && st.file_size == 512 && st.game_code == 0x4E525745 &&
          st.company_code == 0x3031 && !memcmp(st.game_name, name, 16) && !memcmp(st.ext_name, ext, 4));
    CHECK(osPfsReadWriteFile(&pfs, file, 1, 256, 256, data) == 0);
    CHECK(osPfsReadWriteFile(&pfs, file, 0, 256, 256, back) == 0 && !memcmp(data, back, 256));
    CHECK(osPfsReadWriteFile(&pfs, file, 0, 0, 256, back) == 0 && back[0] == 0 && back[255] == 0); /* new pages are zero */
    CHECK(osPfsReadWriteFile(&pfs, file, 1, 480, 64, data) == PFS_ERR_INVALID);                      /* past the end */
    CHECK(osPfsReadWriteFile(&pfs, file, 1, 16, 32, data) == PFS_ERR_INVALID);                       /* unaligned */
    CHECK(osPfsAllocateFile(&pfs, 1, 2, (u8 *)"BIG", NULL, 123 * 256, &file) == PFS_ERR_DATA_FULL);
    CHECK(osPfsAllocateFile(&pfs, 1, 2, (u8 *)"BIG", NULL, 121 * 256, &file) == 0 && file == 1);
    CHECK(osPfsDeleteFile(&pfs, 1, 2, (u8 *)"BIG", NULL) == 0);
    CHECK(osPfsFindFile(&pfs, 1, 2, (u8 *)"BIG", NULL, &file) == PFS_ERR_INVALID);
    for (unsigned i = 0; i < 16; i++) { /* the note table fills before the pages do */
        u8 n[16] = {0};
        n[0] = (u8)('a' + i);
        s32 r = osPfsAllocateFile(&pfs, 5, 6, n, NULL, 32, &file);
        CHECK(i < 15 ? r == 0 : r == PFS_ERR_DIR_FULL);
    }
    n64c_shutdown();
    /* the card is the file: a new run finds the same files */
    n64c_init(&cfg);
    CHECK(osPfsInitPak(&mq, &pfs, 0) == 0);
    CHECK(osPfsFindFile(&pfs, 0x3031, 0x4E525745, name, ext, &file) == 0 && file == 0);
    CHECK(osPfsReadWriteFile(&pfs, file, 0, 256, 256, back) == 0 && !memcmp(data, back, 256));
    n64c_shutdown();
}
static void serial(void)
{
    N64cConfig cfg = {.pak_path = path};
    u8 packet[64], block[32], hi, lo;
    n64c_init(&cfg);
    /* controller status: a card is announced */
    memset(packet, 0, sizeof packet);
    packet[0] = 1; packet[1] = 3; packet[2] = 0; packet[6] = 0xFE; packet[63] = 1;
    CHECK(exchange(packet) == 0 && packet[3] == 5 && packet[4] == 0 && packet[5] == 1);
    /* write a block to the card, read it back */
    for (int i = 0; i < 32; i++) block[i] = (u8)(0x40 + i);
    field(0x20, &hi, &lo);
    memset(packet, 0, sizeof packet);
    packet[0] = 35; packet[1] = 1; packet[2] = 3; packet[3] = hi; packet[4] = lo;
    memcpy(packet + 5, block, 32); packet[38] = 0xFE; packet[63] = 1;
    CHECK(exchange(packet) == 0 && packet[37] == n64c_cpak_data_crc(block));
    memset(packet, 0, sizeof packet);
    packet[0] = 3; packet[1] = 33; packet[2] = 2; packet[3] = hi; packet[4] = lo; packet[38] = 0xFE; packet[63] = 1;
    CHECK(exchange(packet) == 0 && !memcmp(packet + 5, block, 32) && packet[37] == n64c_cpak_data_crc(block));
    /* a wrong address CRC is a failed transfer: the data CRC does not match */
    memset(packet, 0, sizeof packet);
    packet[0] = 3; packet[1] = 33; packet[2] = 2; packet[3] = hi; packet[4] = (u8)(lo ^ 1); packet[38] = 0xFE; packet[63] = 1;
    CHECK(exchange(packet) == 0 && packet[37] != n64c_cpak_data_crc(packet + 5));
    /* the accessory area answers zeros and takes writes without effect */
    field(0x400, &hi, &lo);
    CHECK(hi == 0x80 && lo == 0x01); /* the well-known address of the accessory control block */
    memset(packet, 0, sizeof packet);
    packet[0] = 3; packet[1] = 33; packet[2] = 2; packet[3] = hi; packet[4] = lo; packet[38] = 0xFE; packet[63] = 1;
    CHECK(exchange(packet) == 0 && packet[5] == 0 && packet[36] == 0 && packet[37] == n64c_cpak_data_crc(packet + 5));
    n64c_shutdown();
}
int main(void)
{
    u8 image[N64C_CPAK_BYTES], image2[N64C_CPAK_BYTES];
    FILE *f;
    remove(path);
    /* a fresh card is always the same bytes (replays start from identical images) and is internally consistent */
    n64c_cpak_format(image);
    n64c_cpak_format(image2);
    CHECK(!memcmp(image, image2, sizeof image) && n64c_cpak_id_ok(image + 0x20) && n64c_cpak_id_ok(image + 0xC0));
    CHECK(!memcmp(image + 0x100, image + 0x200, 0x100));
    CHECK(fnv(image, sizeof image) == 0x5731d45387193563ull); /* the same constant tests/test_tas.py holds */
    run();
    serial();
    /* a file that is not a whole card is refused; the slot stays empty */
    f = fopen(path, "wb"); fputs("short", f); fclose(f);
    {
        N64cConfig cfg = {.pak_path = path};
        n64c_init(&cfg);
        CHECK(!n64c_cont_pak_present(0));
        n64c_shutdown();
    }
    remove(path);
    if (fails) { printf("%d failures\n", fails); return 1; }
    printf("test_cpak ok\n");
    return 0;
}
