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
/* Re-scan the controller ports (reset command on every channel, then the status poll) and fill status[4]. No
 * completion message is left behind. Returns 0. */
s32 n64c_cont_rescan(OSMesgQueue *mq, OSContStatus *status);
/* n64c_si.c: forget the lock (runs with n64c_cont_reset). */
void n64c_si_reset(void);
#ifdef __cplusplus
}
#endif
#endif
