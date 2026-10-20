/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Timed event queue shared by the sound and sequence players: a fixed pool of events, a free list and a
 * waiting list ordered by due time on a microsecond clock that the owning player advances. */
#include "n64c_al.h"
#include <string.h>

int n64c_evtq_init(N64cEvtq *q, ALHeap *heap, s32 maxEvents)
{
    memset(q, 0, sizeof *q);
    if (maxEvents <= 0) return -1;
    q->pool = alHeapDBAlloc(NULL, 0, heap, maxEvents, (s32)sizeof(N64cEvt));
    if (!q->pool) return -1;
    q->count = maxEvents;
    for (s32 i = 0; i < maxEvents; i++) alLink(&q->pool[i].node, &q->freeList);
    return 0;
}

int n64c_evtq_post(N64cEvtq *q, const N64cEvt *e, s32 delta)
{
    ALLink *slot = q->freeList.next;
    if (!slot) return -1;
    alUnlink(slot);
    N64cEvt *n = (N64cEvt *)slot;
    n->type = e->type;
    n->a = e->a;
    n->b = e->b;
    n->c = e->c;
    n->p = e->p;
    n->due = q->now + (delta > 0 ? delta : 0);
    /* After the last queued event that is due no later than this one (stable for equal times). */
    ALLink *at = &q->waiting;
    while (at->next && ((N64cEvt *)at->next)->due <= n->due) at = at->next;
    alLink(&n->node, at);
    return 0;
}

int n64c_evtq_pop(N64cEvtq *q, N64cEvt *e)
{
    ALLink *head = q->waiting.next;
    if (!head || ((N64cEvt *)head)->due > q->now) return 0;
    N64cEvt *n = (N64cEvt *)head;
    alUnlink(head);
    *e = *n;
    e->node.next = e->node.prev = NULL;
    alLink(head, &q->freeList);
    return 1;
}

s32 n64c_evtq_next_delta(const N64cEvtq *q)
{
    const ALLink *head = q->waiting.next;
    if (!head) return -1;
    s64 d = ((const N64cEvt *)head)->due - q->now;
    return d < 0 ? 0 : (d > 0x7fffffff ? 0x7fffffff : (s32)d);
}

void n64c_evtq_advance(N64cEvtq *q, s32 micros)
{
    q->now += micros;
}

void n64c_evtq_flush(N64cEvtq *q, s32 type, s32 a)
{
    ALLink *l = q->waiting.next;
    while (l) {
        ALLink *next = l->next;
        N64cEvt *n = (N64cEvt *)l;
        if (n->type == type && n->a == a) {
            alUnlink(l);
            alLink(l, &q->freeList);
        }
        l = next;
    }
}
