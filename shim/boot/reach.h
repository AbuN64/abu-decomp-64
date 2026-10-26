/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_DECOMP_REACH_H
#define N64C_DECOMP_REACH_H
/* Reach records: which functions the run has entered (GAME-RUNNER 10a). The live page lights its grid from them.
 * Pure logic over a table of per-row entry counts, so a test can feed counts and decode the stream; trace.c owns the
 * counts (its entry hook) and the socket. Little-endian wire record, at most N64C_REACH_MAX bytes:
 *     u8 type (1 full, 2 delta), u32 poll,
 *     full:  the bitset over rows (bit r = byte r>>3, mask 1<<(r&7)), (rows+7)/8 bytes;
 *     delta: varint n + n delta-coded ascending rows entered for the first time (fresh),
 *            then varint n + n delta-coded ascending rows whose count grew (hot, fresh rows not repeated). */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
enum { N64C_REACH_FULL = 1, N64C_REACH_DELTA = 2, N64C_REACH_MAX = 4096, N64C_REACH_HOT_CAP = 1024, N64C_REACH_RESYNC = 256 };

typedef struct {
    unsigned rows;
    const unsigned *entered; /* live per-row entry counts, written by the entry hook */
    unsigned *seen;          /* the counts at the last record */
    unsigned char *bits;     /* the rows already sent: what the receiver's bitset holds */
    uint32_t *fresh, *hot;
    unsigned *grew;          /* per hot row, how much its count grew */
    unsigned since_full;
    int started;
} N64cReach;

static inline unsigned char *n64c_reach_put_varint(unsigned char *o, uint32_t v)
{
    while (v > 127) {
        *o++ = (unsigned char)(v & 127) | 128;
        v >>= 7;
    }
    *o++ = (unsigned char)v;
    return o;
}
static inline size_t n64c_reach_varint_size(uint32_t v)
{
    size_t n = 1;
    for (; v > 127; v >>= 7)
        n++;
    return n;
}
static inline unsigned char *n64c_reach_put_header(unsigned char *o, int type, uint32_t poll)
{
    *o++ = (unsigned char)type;
    for (int i = 0; i < 4; i++)
        *o++ = (unsigned char)(poll >> (8 * i));
    return o;
}
/* 0 when out of memory */
static inline int n64c_reach_init(N64cReach *r, unsigned rows, const unsigned *entered)
{
    memset(r, 0, sizeof *r);
    r->rows = rows;
    r->entered = entered;
    r->seen = calloc(rows ? rows : 1, sizeof *r->seen);
    r->bits = calloc((rows + 7) / 8 + 1, 1);
    r->fresh = malloc((rows ? rows : 1) * sizeof *r->fresh);
    r->hot = malloc((rows ? rows : 1) * sizeof *r->hot);
    r->grew = malloc((rows ? rows : 1) * sizeof *r->grew);
    return r->seen && r->bits && r->fresh && r->hot && r->grew;
}
static inline void n64c_reach_free(N64cReach *r)
{
    free(r->seen);
    free(r->bits);
    free(r->fresh);
    free(r->hot);
    free(r->grew);
    memset(r, 0, sizeof *r);
}
/* Keep the cap rows that grew most, in ascending row order. */
static inline unsigned n64c_reach_top(N64cReach *r, unsigned n, unsigned cap)
{
    if (n <= cap)
        return n;
    unsigned histogram[256] = {0}, floor = 255, above = 0;
    for (unsigned i = 0; i < n; i++)
        histogram[r->grew[i] > 255 ? 255 : r->grew[i]]++;
    while (floor > 0 && above + histogram[floor] < cap)
        above += histogram[floor--];
    unsigned room = cap - above, kept = 0; /* rows at exactly the floor fill what the larger ones leave */
    for (unsigned i = 0; i < n; i++) {
        const unsigned g = r->grew[i] > 255 ? 255 : r->grew[i];
        if (g > floor || (g == floor && room && room--)) {
            r->hot[kept] = r->hot[i];
            r->grew[kept++] = r->grew[i];
        }
    }
    return kept;
}
static inline size_t n64c_reach_full(N64cReach *r, uint32_t poll, unsigned char *out)
{
    unsigned char *o = n64c_reach_put_header(out, N64C_REACH_FULL, poll);
    memcpy(o, r->bits, (r->rows + 7) / 8);
    r->since_full = 0;
    return (size_t)(o - out) + (r->rows + 7) / 8;
}
/* The next record for the counts as they stand now, into out (N64C_REACH_MAX bytes); 0 when nothing changed. The first
 * record, every N64C_REACH_RESYNC-th one (a datagram dropped on the way heals) and a burst of new rows that does not fit
 * the cap are full records. */
static inline size_t n64c_reach_next(N64cReach *r, uint32_t poll, unsigned char *out)
{
    unsigned nf = 0, nh = 0;
    for (unsigned i = 0; i < r->rows; i++) {
        const unsigned now = __atomic_load_n(&r->entered[i], __ATOMIC_RELAXED), was = r->seen[i];
        if (now == was)
            continue;
        r->seen[i] = now;
        if (!(r->bits[i >> 3] & (1u << (i & 7)))) {
            r->bits[i >> 3] |= (unsigned char)(1u << (i & 7));
            r->fresh[nf++] = i;
        } else {
            r->hot[nh] = i;
            r->grew[nh++] = now - was;
        }
    }
    if (!r->started) {
        r->started = 1;
        return n64c_reach_full(r, poll, out);
    }
    if (!nf && !nh)
        return 0;
    if (++r->since_full >= N64C_REACH_RESYNC)
        return n64c_reach_full(r, poll, out);
    unsigned char *o = n64c_reach_put_header(out, N64C_REACH_DELTA, poll);
    size_t fresh_bytes = n64c_reach_varint_size(nf);
    for (unsigned i = 0, p = 0; i < nf; p = r->fresh[i++])
        fresh_bytes += n64c_reach_varint_size(r->fresh[i] - p);
    if ((size_t)(o - out) + fresh_bytes + 1 > N64C_REACH_MAX) /* the 1 is the hot count, at least a byte */
        return n64c_reach_full(r, poll, out);
    o = n64c_reach_put_varint(o, nf);
    for (unsigned i = 0, p = 0; i < nf; p = r->fresh[i++])
        o = n64c_reach_put_varint(o, r->fresh[i] - p);
    nh = n64c_reach_top(r, nh, N64C_REACH_HOT_CAP);
    unsigned fit = 0;
    size_t hot_bytes = 0;
    for (unsigned i = 0, p = 0; i < nh; p = r->hot[i++]) {
        const size_t b = n64c_reach_varint_size(r->hot[i] - p);
        if ((size_t)(o - out) + 2 + hot_bytes + b > N64C_REACH_MAX) /* the 2 is the hot count of up to 1024 rows */
            break;
        hot_bytes += b;
        fit++;
    }
    o = n64c_reach_put_varint(o, fit);
    for (unsigned i = 0, p = 0; i < fit; p = r->hot[i++])
        o = n64c_reach_put_varint(o, r->hot[i] - p);
    return (size_t)(o - out);
}
#endif
