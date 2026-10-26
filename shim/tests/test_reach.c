/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Reach records (boot/reach.h): a stream encoded from changing per-row entry counts decodes, with an independent decoder
 * written here from the wire format, back to the same bitset; first record full, deltas small, bursts and caps bounded. */
#include "../boot/reach.h"
#include "test_check.h"
#include <stdint.h>
#include <stdio.h>
#include <string.h>
enum { ROWS = 4353 };
static unsigned entered[ROWS];
static unsigned char got[(ROWS + 7) / 8]; /* the receiver's bitset */
static unsigned fresh_seen[ROWS], hot_seen[ROWS], nfresh, nhot;
static unsigned long total_bytes, records, max_record;
static uint32_t last_poll;
static int strict = 1; /* off while records are being lost on purpose */
static unsigned read_varint(const unsigned char *u, size_t n, size_t *i)
{
    unsigned v = 0, shift = 0, c;
    do {
        Test_Check(*i < n, "varint inside the record");
        c = u[(*i)++];
        v |= (c & 127u) << shift;
        shift += 7;
    } while (c & 128u);
    return v;
}
/* The page's decoder (runner.html applyReach), in C. */
static void decode(const unsigned char *u, size_t n)
{
    Test_Check(n >= 5 && n <= N64C_REACH_MAX, "record size in bounds");
    last_poll = u[1] | u[2] << 8 | u[3] << 16 | (uint32_t)u[4] << 24;
    nfresh = nhot = 0;
    if (u[0] == N64C_REACH_FULL) {
        Test_Check(n == 5 + sizeof got, "a full record is the header and the bitset");
        memcpy(got, u + 5, sizeof got);
    } else {
        Test_Check(u[0] == N64C_REACH_DELTA, "known record type");
        size_t i = 5;
        for (int pass = 0; pass < 2; pass++) {
            unsigned count = read_varint(u, n, &i), row = 0;
            while (count--) {
                row += read_varint(u, n, &i);
                Test_Check(row < ROWS, "row inside the table");
                if (pass == 0) {
                    Test_Check(!strict || !(got[row >> 3] & (1u << (row & 7))), "a fresh row was not set before");
                    got[row >> 3] |= (unsigned char)(1u << (row & 7));
                    fresh_seen[nfresh++] = row;
                } else {
                    Test_Check(!strict || got[row >> 3] & (1u << (row & 7)), "a hot row is already set");
                    hot_seen[nhot++] = row;
                }
            }
        }
        Test_Check(i == n, "no bytes left over");
    }
    records++;
    total_bytes += n;
    if (n > max_record)
        max_record = n;
}
static int truth(unsigned r) { return entered[r] != 0; }
static void check_equal(void)
{
    for (unsigned r = 0; r < ROWS; r++)
        Test_Check(!!(got[r >> 3] & (1u << (r & 7))) == truth(r), "decoded bitset equals the rows entered");
}
static size_t step(N64cReach *reach, uint32_t poll, unsigned char *out)
{
    const size_t n = n64c_reach_next(reach, poll, out);
    if (n)
        decode(out, n);
    return n;
}
int main(void)
{
    N64cReach reach;
    unsigned char out[N64C_REACH_MAX];
    Test_Check(n64c_reach_init(&reach, ROWS, entered), "init");
    /* nothing entered yet: the first record is still full (the page needs the set so far), and empty */
    Test_Check(step(&reach, 7, out) == 5 + sizeof got && last_poll == 7, "first record is full");
    check_equal();
    Test_Check(step(&reach, 8, out) == 0, "no change, no record");
    /* a small delta: two new rows, one of them again */
    entered[3] = 1;
    entered[100] = 5;
    Test_Check(step(&reach, 9, out) > 0 && out[0] == N64C_REACH_DELTA, "delta record");
    Test_Check(nfresh == 2 && fresh_seen[0] == 3 && fresh_seen[1] == 100 && nhot == 0, "fresh rows");
    entered[100] += 2;
    entered[3] = 1;
    entered[4000] = 1;
    Test_Check(step(&reach, 10, out) > 0 && nfresh == 1 && fresh_seen[0] == 4000 && nhot == 1 && hot_seen[0] == 100, "fresh and hot");
    check_equal();
    /* a burst of every row at once cannot fit a delta of 4 KB with hot rows: it still arrives, whole */
    for (unsigned r = 0; r < ROWS; r++)
        entered[r] += 1 + r % 3;
    Test_Check(step(&reach, 11, out) > 0, "burst record");
    check_equal();
    /* hot rows are capped at the 1024 that grew most, in ascending order */
    for (unsigned r = 0; r < ROWS; r++)
        entered[r] += 1 + (r < 2000 ? 1 : 50 + r % 7);
    Test_Check(step(&reach, 12, out) > 0 && out[0] == N64C_REACH_DELTA, "all rows hot");
    Test_Check(nhot == N64C_REACH_HOT_CAP, "hot rows capped");
    for (unsigned i = 0; i < nhot; i++) {
        Test_Check(hot_seen[i] >= 2000, "the rows that grew most are kept");
        Test_Check(i == 0 || hot_seen[i] > hot_seen[i - 1], "ascending rows");
    }
    /* a random run: counts grow, the stream decodes to the same set, a full record comes every N64C_REACH_RESYNC */
    unsigned seed = 12345, fulls = 0;
    memset(entered, 0, sizeof entered);
    n64c_reach_free(&reach);
    Test_Check(n64c_reach_init(&reach, ROWS, entered), "init again");
    memset(got, 0, sizeof got);
    total_bytes = records = max_record = 0;
    for (uint32_t poll = 1; poll <= 3000; poll++) {
        const unsigned touched = (seed = seed * 1103515245u + 12345u) >> 16 & 15;
        for (unsigned k = 0; k < touched; k++) {
            seed = seed * 1103515245u + 12345u;
            entered[(seed >> 8) % (poll < 500 ? 1200 : ROWS)] += 1 + (seed >> 4 & 3);
        }
        if (step(&reach, poll, out) && out[0] == N64C_REACH_FULL)
            fulls++;
        check_equal();
    }
    Test_Check(fulls >= 3 && fulls < records / 8, "periodic full records heal a dropped datagram");
    Test_Check(max_record <= N64C_REACH_MAX, "records within the cap");
    /* a record dropped on the way: the receiver misses it and the next full record repairs the set */
    strict = 0;
    memset(got, 0, sizeof got);
    for (unsigned r = 0; r < 50; r++)
        entered[r] += 1;
    for (int tries = 0; tries < 400 && memcmp(got, reach.bits, sizeof got); tries++) {
        entered[tries % 9] += 1;
        const size_t n = n64c_reach_next(&reach, 4000 + tries, out);
        if (n && tries % 2)
            decode(out, n); /* every second record is lost */
        else if (n && out[0] == N64C_REACH_FULL)
            decode(out, n);
    }
    Test_Check(!memcmp(got, reach.bits, sizeof got), "a full record repairs a lost datagram");
    printf("reach: %lu records, %lu bytes, average %lu, max %lu\n", records, total_bytes, total_bytes / records, max_record);
    n64c_reach_free(&reach);
    return 0;
}
