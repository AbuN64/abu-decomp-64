/* SPDX-License-Identifier: GPL-3.0-or-later */
/* n64c_pfs: Controller Pak file system and Rumble Pak, over the card image in the slot (n64c_cpak.h for the
 * layout). With no card in the slot every call answers what the hardware reports then:
 *  - an empty slot answers PFS_ERR_NOPACK to the probe, exactly as the hardware reports an empty slot;
 *  - every file call on a handle that never initialised answers PFS_ERR_INVALID, the documented result;
 *  - a card is a memory card, not a Rumble Pak: osMotorInit answers PFS_ERR_DEVICE.
 * Files are chains of 256-byte pages named by the index table; a note names a file by company code, game code,
 * game name and extension. The card is the user's save: nothing is ever invented. */
#include "n64c_pfs.h"
#include "n64c_cpak.h"
#include "n64c_si.h"
#include <string.h>
static void bind(OSPfs *pfs, OSMesgQueue *mq, int channel)
{
    pfs->queue = mq;
    pfs->channel = channel;
    pfs->activebank = 0xFF;
    pfs->status = 0;
}
s32 __osPfsGetStatus(OSMesgQueue *mq, int channel)
{
    (void)mq;
    if (channel < 0 || channel >= MAXCONTROLLERS)
        return PFS_ERR_NOPACK;
    return n64c_cont_pak_present(channel) ? 0 : PFS_ERR_NOPACK;
}
static u16 get16(const u8 *p) { return (u16)((p[0] << 8) | p[1]); }
static u32 get32(const u8 *p) { return ((u32)p[0] << 24) | ((u32)p[1] << 16) | ((u32)p[2] << 8) | p[3]; }
static void put16(u8 *p, u16 v) { p[0] = (u8)(v >> 8); p[1] = (u8)v; }
static void put32(u8 *p, u32 v) { put16(p, (u16)(v >> 16)); put16(p + 2, (u16)v); }
#define INDEX(img) ((img) + N64C_CPAK_PAGE)
#define NOTE(img, n) ((img) + 3 * N64C_CPAK_PAGE + (n) * N64C_CPAK_NOTE_BYTES)
/* Note: game code u32 @0, company u16 @4, first page u16 @6, status @8, extension @0xC (4), name @0x10 (16). */
static int note_used(const u8 *note)
{
    u16 first = get16(note + 6);
    return first >= N64C_CPAK_FIRST_DATA_PAGE && first < N64C_CPAK_PAGES;
}
static u8 *card(const OSPfs *pfs) { return n64c_cont_pak_image(pfs->channel); }
/* A handle that never reached PFS_INITIALIZED cannot name files; a card that left the slot answers NOPACK. */
static s32 usable(const OSPfs *pfs, u8 **image)
{
    if (!(pfs->status & PFS_INITIALIZED))
        return PFS_ERR_INVALID;
    *image = card(pfs);
    return *image ? 0 : PFS_ERR_NOPACK;
}
/* Write the index tables and the changed range back to the file. */
static s32 commit(u8 *image, u32 offset, u32 length)
{
    n64c_cpak_index_seal(image);
    if (n64c_cont_pak_commit(N64C_CPAK_PAGE, 2 * N64C_CPAK_PAGE) != 0)
        return PFS_ERR_CONTRFAIL;
    if (length && n64c_cont_pak_commit(offset, length) != 0)
        return PFS_ERR_CONTRFAIL;
    return 0;
}
static int id_block(const u8 *image)
{
    static const unsigned at[4] = {0x20, 0x60, 0x80, 0xC0};
    for (int i = 0; i < 4; i++)
        if (n64c_cpak_id_ok(image + at[i]))
            return (int)at[i];
    return -1;
}
/* The index table is trusted only when its checksum holds; page 2 is a second copy. */
static int index_ok(const u8 *table)
{
    u8 sum = 0;
    for (unsigned i = N64C_CPAK_FIRST_DATA_PAGE; i < N64C_CPAK_PAGES; i++)
        sum = (u8)(sum + table[i * 2 + 1]);
    return table[0] == 0 && table[1] == sum;
}
s32 osPfsInitPak(OSMesgQueue *mq, OSPfs *pfs, int channel)
{
    s32 ret;
    u8 *image;
    int id;
    bind(pfs, mq, channel);
    ret = __osPfsGetStatus(mq, channel);
    if (ret != 0)
        return ret;
    image = card(pfs);
    if (!image)
        return PFS_ERR_NOPACK;
    id = id_block(image);
    if (id < 0)
        return PFS_ERR_ID_FATAL;
    if (!index_ok(INDEX(image))) {
        if (!index_ok(INDEX(image) + N64C_CPAK_PAGE))
            return PFS_ERR_INCONSISTENT;
        memcpy(INDEX(image), INDEX(image) + N64C_CPAK_PAGE, N64C_CPAK_PAGE); /* repair from the copy */
        if (commit(image, 0, 0) != 0)
            return PFS_ERR_CONTRFAIL;
    }
    memcpy(pfs->id, image + id, 32);
    memcpy(pfs->label, image + 0xE0, 32);
    pfs->version = image[id + 0x1B];
    pfs->banks = image[id + 0x1A];
    pfs->activebank = 0;
    pfs->inode_table = 1;
    pfs->minode_table = 2;
    pfs->dir_table = 3;
    pfs->dir_size = N64C_CPAK_NOTES;
    pfs->inode_start_page = N64C_CPAK_FIRST_DATA_PAGE;
    pfs->status = PFS_INITIALIZED;
    return 0;
}
static unsigned count_free(const u8 *image)
{
    unsigned n = 0;
    for (unsigned i = N64C_CPAK_FIRST_DATA_PAGE; i < N64C_CPAK_PAGES; i++)
        n += get16(INDEX(image) + i * 2) == N64C_CPAK_INDEX_FREE;
    return n;
}
/* Pages of a file, following the chain; stops on a damaged chain (loop or out-of-range link). */
static unsigned chain_length(const u8 *image, unsigned first)
{
    unsigned n = 0, page = first;
    while (page >= N64C_CPAK_FIRST_DATA_PAGE && page < N64C_CPAK_PAGES && n <= N64C_CPAK_PAGES) {
        n++;
        page = get16(INDEX(image) + page * 2);
    }
    return n;
}
s32 osPfsFileState(OSPfs *pfs, int file_no, OSPfsState *state)
{
    u8 *image;
    s32 ret = usable(pfs, &image);
    if (ret)
        return ret;
    if (file_no < 0 || file_no >= (int)N64C_CPAK_NOTES || !note_used(NOTE(image, file_no)))
        return PFS_ERR_INVALID;
    const u8 *note = NOTE(image, file_no);
    state->file_size = chain_length(image, get16(note + 6)) * N64C_CPAK_PAGE;
    state->game_code = get32(note);
    state->company_code = get16(note + 4);
    memcpy(state->ext_name, note + 0xC, 4);
    memcpy(state->game_name, note + 0x10, 16);
    return 0;
}
s32 osPfsFreeBlocks(OSPfs *pfs, s32 *bytes_not_used)
{
    u8 *image;
    s32 ret = usable(pfs, &image);
    if (ret)
        return ret;
    *bytes_not_used = (s32)(count_free(image) * N64C_CPAK_PAGE);
    return 0;
}
/* The note of a file, -1 when there is none. A missing extension is four zero bytes. */
static int find_note(const u8 *image, u16 company, u32 game, const u8 *name, const u8 *ext)
{
    u8 n[16] = {0}, e[4] = {0};
    if (name)
        memcpy(n, name, 16);
    if (ext)
        memcpy(e, ext, 4);
    for (unsigned i = 0; i < N64C_CPAK_NOTES; i++) {
        const u8 *note = NOTE(image, i);
        if (note_used(note) && get16(note + 4) == company && get32(note) == game && !memcmp(note + 0x10, n, 16) &&
            !memcmp(note + 0xC, e, 4))
            return (int)i;
    }
    return -1;
}
s32 osPfsFindFile(OSPfs *pfs, u16 company_code, u32 game_code, u8 *game_name, u8 *ext_name, s32 *file_no)
{
    u8 *image;
    s32 ret = usable(pfs, &image);
    if (ret)
        return ret;
    int n = find_note(image, company_code, game_code, game_name, ext_name);
    if (n < 0)
        return PFS_ERR_INVALID;
    *file_no = n;
    return 0;
}
static void free_chain(u8 *image, unsigned first)
{
    unsigned page = first, steps = 0;
    while (page >= N64C_CPAK_FIRST_DATA_PAGE && page < N64C_CPAK_PAGES && steps++ < N64C_CPAK_PAGES) {
        unsigned next = get16(INDEX(image) + page * 2);
        put16(INDEX(image) + page * 2, N64C_CPAK_INDEX_FREE);
        page = next;
    }
}
s32 osPfsDeleteFile(OSPfs *pfs, u16 company_code, u32 game_code, u8 *game_name, u8 *ext_name)
{
    u8 *image;
    s32 ret = usable(pfs, &image);
    if (ret)
        return ret;
    int n = find_note(image, company_code, game_code, game_name, ext_name);
    if (n < 0)
        return PFS_ERR_INVALID;
    free_chain(image, get16(NOTE(image, n) + 6));
    memset(NOTE(image, n), 0, N64C_CPAK_NOTE_BYTES);
    return commit(image, 3 * N64C_CPAK_PAGE, 2 * N64C_CPAK_PAGE);
}
s32 osPfsAllocateFile(OSPfs *pfs, u16 company_code, u32 game_code, u8 *game_name, u8 *ext_name, int nbytes,
                      s32 *file_no)
{
    u8 *image;
    s32 ret = usable(pfs, &image);
    if (ret)
        return ret;
    if (nbytes <= 0 || nbytes % 32)
        return PFS_ERR_INVALID;
    if (find_note(image, company_code, game_code, game_name, ext_name) >= 0)
        return PFS_ERR_EXIST;
    unsigned pages = ((unsigned)nbytes + N64C_CPAK_PAGE - 1) / N64C_CPAK_PAGE;
    if (pages > count_free(image))
        return PFS_ERR_DATA_FULL;
    int slot = -1;
    for (unsigned i = 0; i < N64C_CPAK_NOTES && slot < 0; i++)
        if (!note_used(NOTE(image, i)))
            slot = (int)i;
    if (slot < 0)
        return PFS_ERR_DIR_FULL;
    unsigned prev = 0, first = 0, got = 0;
    for (unsigned p = N64C_CPAK_FIRST_DATA_PAGE; p < N64C_CPAK_PAGES && got < pages; p++) {
        if (get16(INDEX(image) + p * 2) != N64C_CPAK_INDEX_FREE)
            continue;
        if (prev)
            put16(INDEX(image) + prev * 2, (u16)p);
        else
            first = p;
        memset(image + p * N64C_CPAK_PAGE, 0, N64C_CPAK_PAGE);
        put16(INDEX(image) + p * 2, N64C_CPAK_INDEX_END);
        prev = p;
        got++;
    }
    u8 *note = NOTE(image, slot);
    memset(note, 0, N64C_CPAK_NOTE_BYTES);
    put32(note, game_code);
    put16(note + 4, company_code);
    put16(note + 6, (u16)first);
    note[8] = 2;
    if (ext_name)
        memcpy(note + 0xC, ext_name, 4);
    if (game_name)
        memcpy(note + 0x10, game_name, 16);
    *file_no = slot;
    ret = commit(image, 3 * N64C_CPAK_PAGE, 2 * N64C_CPAK_PAGE);
    if (ret == 0 && n64c_cont_pak_commit(N64C_CPAK_FIRST_DATA_PAGE * N64C_CPAK_PAGE,
                                         (N64C_CPAK_PAGES - N64C_CPAK_FIRST_DATA_PAGE) * N64C_CPAK_PAGE) != 0)
        ret = PFS_ERR_CONTRFAIL;
    return ret;
}
/* flag 0 reads, 1 writes; offset and size in bytes, both whole 32-byte blocks inside the file. */
s32 osPfsReadWriteFile(OSPfs *pfs, s32 file_no, u8 flag, int offset, int nbytes, u8 *buffer)
{
    u8 *image;
    s32 ret = usable(pfs, &image);
    if (ret)
        return ret;
    if (file_no < 0 || file_no >= (int)N64C_CPAK_NOTES || !note_used(NOTE(image, file_no)) || offset < 0 ||
        nbytes <= 0 || offset % 32 || nbytes % 32 || !buffer)
        return PFS_ERR_INVALID;
    unsigned first = get16(NOTE(image, file_no) + 6), size = chain_length(image, first) * N64C_CPAK_PAGE;
    if ((unsigned)offset + (unsigned)nbytes > size)
        return PFS_ERR_INVALID;
    while (nbytes > 0) {
        unsigned skip = (unsigned)offset / N64C_CPAK_PAGE, page = first;
        while (skip--)
            page = get16(INDEX(image) + page * 2);
        unsigned at = page * N64C_CPAK_PAGE + (unsigned)offset % N64C_CPAK_PAGE;
        if (flag) {
            memcpy(image + at, buffer, 32);
            if (n64c_cont_pak_commit(at, 32) != 0)
                return PFS_ERR_CONTRFAIL;
        } else {
            memcpy(buffer, image + at, 32);
        }
        buffer += 32;
        offset += 32;
        nbytes -= 32;
    }
    return 0;
}
s32 n64c_pfs_check_id(OSPfs *pfs)
{
    /* The id area is read through the slot: nothing to read from an empty slot. */
    s32 ret = __osPfsGetStatus(pfs->queue, pfs->channel);
    if (ret)
        return ret;
    u8 *image = card(pfs);
    if (!image || id_block(image) < 0)
        return PFS_ERR_ID_FATAL;
    return 0;
}
/* ---- Rumble Pak ---- */
s32 osMotorInit(OSMesgQueue *mq, OSPfs *pfs, int channel)
{
    s32 ret;
    bind(pfs, mq, channel);
    ret = __osPfsGetStatus(mq, channel);
    if (ret != 0)
        return ret;
    return PFS_ERR_DEVICE; /* a memory Pak answers, not a Rumble Pak */
}
/* A motor that never initialised is not driven; the documented result is PFS_ERR_INVALID. */
s32 osMotorStart(OSPfs *pfs) { (void)pfs; return PFS_ERR_INVALID; }
s32 osMotorStop(OSPfs *pfs) { (void)pfs; return PFS_ERR_INVALID; }
