/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Serial interface access lock and the cross-file hooks of the SI, controller and Pak adapters. */
#ifndef N64C_SI_H
#define N64C_SI_H
#include "n64c_os.h"
#ifdef __cplusplus
extern "C" {
#endif
/* The SI is one device: a thread takes it before it starts a packet exchange and gives it back when done. A second
 * taker blocks until the first gives it back. */
void __osSiGetAccess(void);
void __osSiRelAccess(void);
/* n64c_cont.c: nonzero when a Controller Pak image is configured and sits in this port's slot. */
int n64c_cont_pak_present(int channel);
/* The card image (N64C_CPAK_BYTES) in the slot, NULL when none; after changing bytes of it, commit that range to
 * the file (0, or -1 when the file cannot be written). */
u8 *n64c_cont_pak_image(int channel);
int n64c_cont_pak_commit(u32 offset, u32 length);
/* One one-channel Joybus exchange (a status request or an accessory packet) waited out on mq; none when mq is NULL. */
void n64c_cont_channel_exchange(OSMesgQueue *mq, int channel);
/* libultra's accessory packet on channel, with the status request that follows an empty slot's bad CRC: 1 when a
 * card answered. */
int n64c_cont_accessory(OSMesgQueue *mq, int channel);
/* Re-scan the controller ports (reset command on every channel, then the status poll) and fill status[4]. No
 * completion message is left behind. Returns 0. */
s32 n64c_cont_rescan(OSMesgQueue *mq, OSContStatus *status);
/* The queue the adapter last answered a request on: the controller reply queue. A controller read ends in the SI
 * interrupt a read time after it started (n64c_cont.c), so a blocking receive on this queue waits for it. A receive
 * that finds the queue empty with no exchange running and no SI interrupt pending cannot be released by anyone: threads
 * switch only at blocking calls and no other exchange is on the bus. The wait then returns as the idle-bus round trip
 * the console's own timing would have had outstanding. */
OSMesgQueue *n64c_si_queue(void);
/* n64c_si.c: forget the lock (runs with n64c_cont_reset). */
void n64c_si_reset(void);
#ifdef __cplusplus
}
#endif
#endif
