/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Controller Pak file system and Rumble Pak surface. Written from the public libultra API documentation. This port
 * models the oracle machine: no Pak in any slot, so every Pak operation reports what the hardware reports then. */
#ifndef N64C_PFS_H
#define N64C_PFS_H
#include "n64c_os.h"
#ifdef __cplusplus
extern "C" {
#endif
#define PFS_ERR_NOPACK 1
#define PFS_ERR_NEW_PACK 2
#define PFS_ERR_INCONSISTENT 3
#define PFS_ERR_CONTRFAIL 4
#define PFS_ERR_INVALID 5
#define PFS_ERR_BAD_DATA 6
#define PFS_ERR_DATA_FULL 7
#define PFS_ERR_DIR_FULL 8
#define PFS_ERR_EXIST 9
#define PFS_ERR_ID_FATAL 10
#define PFS_ERR_DEVICE 11
#define PFS_INITIALIZED 1
/* The handle the game keeps per slot. 0x68 bytes on a 32-bit build. */
typedef struct OSPfs {
    s32 status;
    OSMesgQueue *queue;
    s32 channel;
    u8 id[32];
    u8 label[32];
    s32 version;
    s32 dir_size;
    s32 inode_table;
    s32 minode_table;
    s32 dir_table;
    s32 inode_start_page;
    u8 banks;
    u8 activebank;
} OSPfs;
typedef struct OSPfsState {
    u32 file_size;
    u32 game_code;
    u16 company_code;
    char ext_name[4];
    char game_name[16];
} OSPfsState;
s32 osPfsInitPak(OSMesgQueue *mq, OSPfs *pfs, int channel);
/* Is anything in the slot: 0 when a Pak answers, PFS_ERR_NOPACK when not. */
s32 __osPfsGetStatus(OSMesgQueue *mq, int channel);
s32 osPfsFileState(OSPfs *pfs, int file_no, OSPfsState *state);
s32 osPfsFreeBlocks(OSPfs *pfs, s32 *bytes_not_used);
s32 osPfsReadWriteFile(OSPfs *pfs, s32 file_no, u8 flag, int offset, int nbytes, u8 *buffer);
s32 osPfsDeleteFile(OSPfs *pfs, u16 company_code, u32 game_code, u8 *game_name, u8 *ext_name);
s32 osPfsAllocateFile(OSPfs *pfs, u16 company_code, u32 game_code, u8 *game_name, u8 *ext_name, int nbytes,
                      s32 *file_no);
s32 osPfsFindFile(OSPfs *pfs, u16 company_code, u32 game_code, u8 *game_name, u8 *ext_name, s32 *file_no);
/* Validate the id area of the Pak, repair it when it is damaged and load the file system layout into the handle. */
s32 n64c_pfs_check_id(OSPfs *pfs);
/* Rumble Pak. osMotorStart and osMotorStop are the public macros over one access routine; they are functions here. */
s32 osMotorInit(OSMesgQueue *mq, OSPfs *pfs, int channel);
s32 osMotorStart(OSPfs *pfs);
s32 osMotorStop(OSPfs *pfs);
#ifdef __cplusplus
}
#endif
#endif
