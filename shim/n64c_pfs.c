/* SPDX-License-Identifier: GPL-3.0-or-later */
/* n64c_pfs: Controller Pak file system and Rumble Pak. There is no Pak in the oracle machine, and the port
 * has no Pak in any slot unless the host configures an image, so:
 *  - a slot with nothing answers PFS_ERR_NOPACK to the probe, exactly as the hardware reports an empty slot;
 *  - every file call on a handle that never initialised answers PFS_ERR_INVALID, the documented result;
 *  - a configured image is a memory Pak: it is not a Rumble Pak (PFS_ERR_DEVICE), and the file system layer is
 *    not provided, so osPfsInitPak still answers PFS_ERR_NOPACK and the game proceeds as without a Pak. The
 *    image stays reachable through osContRamRead and osContRamWrite.
 * No Pak data is ever invented. */
#include "n64c_pfs.h"
#include "n64c_si.h"
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
s32 osPfsInitPak(OSMesgQueue *mq, OSPfs *pfs, int channel)
{
    s32 ret;
    bind(pfs, mq, channel);
    ret = __osPfsGetStatus(mq, channel);
    if (ret != 0)
        return ret;
    return PFS_ERR_NOPACK; /* image present, file system layer not provided */
}
/* A handle that never reached PFS_INITIALIZED cannot name files. */
static s32 uninitialised(const OSPfs *pfs) { return (pfs->status & PFS_INITIALIZED) ? 0 : PFS_ERR_INVALID; }
s32 osPfsFileState(OSPfs *pfs, int file_no, OSPfsState *state)
{
    (void)file_no; (void)state;
    return uninitialised(pfs) ? PFS_ERR_INVALID : PFS_ERR_NOPACK;
}
s32 osPfsFreeBlocks(OSPfs *pfs, s32 *bytes_not_used)
{
    (void)bytes_not_used;
    return uninitialised(pfs) ? PFS_ERR_INVALID : PFS_ERR_NOPACK;
}
s32 osPfsReadWriteFile(OSPfs *pfs, s32 file_no, u8 flag, int offset, int nbytes, u8 *buffer)
{
    (void)file_no; (void)flag; (void)offset; (void)nbytes; (void)buffer;
    return uninitialised(pfs) ? PFS_ERR_INVALID : PFS_ERR_NOPACK;
}
s32 osPfsDeleteFile(OSPfs *pfs, u16 company_code, u32 game_code, u8 *game_name, u8 *ext_name)
{
    (void)company_code; (void)game_code; (void)game_name; (void)ext_name;
    return uninitialised(pfs) ? PFS_ERR_INVALID : PFS_ERR_NOPACK;
}
s32 osPfsAllocateFile(OSPfs *pfs, u16 company_code, u32 game_code, u8 *game_name, u8 *ext_name, int nbytes,
                      s32 *file_no)
{
    (void)company_code; (void)game_code; (void)game_name; (void)ext_name; (void)nbytes; (void)file_no;
    return uninitialised(pfs) ? PFS_ERR_INVALID : PFS_ERR_NOPACK;
}
s32 osPfsFindFile(OSPfs *pfs, u16 company_code, u32 game_code, u8 *game_name, u8 *ext_name, s32 *file_no)
{
    (void)company_code; (void)game_code; (void)game_name; (void)ext_name; (void)file_no;
    return uninitialised(pfs) ? PFS_ERR_INVALID : PFS_ERR_NOPACK;
}
s32 n64c_pfs_check_id(OSPfs *pfs)
{
    /* The id area is read through the slot: nothing to read from an empty slot. */
    s32 ret = __osPfsGetStatus(pfs->queue, pfs->channel);
    return ret ? ret : PFS_ERR_NOPACK;
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
