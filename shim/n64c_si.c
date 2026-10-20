/* SPDX-License-Identifier: GPL-3.0-or-later */
/* n64c_si: the serial interface access lock, a one-slot message queue used as a binary semaphore (the public
 * libultra arrangement: the slot holds a token while the SI is free). */
#include "n64c_si.h"
static OSMesgQueue access_queue;
static OSMesg access_slot[1];
static int access_ready;
void n64c_si_reset(void) { access_ready = 0; }
void __osSiGetAccess(void)
{
    OSMesg m;
    if (!access_ready) {
        OSMesg token;
        token.ptr = NULL;
        access_ready = 1;
        osCreateMesgQueue(&access_queue, access_slot, 1);
        osSendMesg(&access_queue, token, OS_MESG_NOBLOCK); /* free */
    }
    osRecvMesg(&access_queue, &m, OS_MESG_BLOCK);
}
void __osSiRelAccess(void)
{
    OSMesg token;
    token.ptr = NULL;
    if (access_ready)
        osSendMesg(&access_queue, token, OS_MESG_NOBLOCK);
}
