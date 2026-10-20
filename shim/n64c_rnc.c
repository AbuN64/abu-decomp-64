/* SPDX-License-Identifier: GPL-3.0-or-later */
/* RNC method 1 from the public format description: the stream is a little-endian
 * bit stream, least significant bit first, read in 16-bit words on demand, with
 * raw literal bytes taken from the byte position of the next unread word. A
 * chunk is three Huffman tables (literal run length, match distance, match
 * length), a 16-bit count of literal/match pairs, then literal run, then
 * (match, literal run) pairs. Code lengths are 4 bits, assigned canonically by
 * (length, symbol index), bit-reversed; symbols >= 2 carry symbol-1 extra bits. */
#include "n64c_rnc.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct { u16 mask, code; u8 bits, symbol; } Leaf;
typedef struct { Leaf leaf[16]; int count; } Table;
typedef struct { const u8 *p, *end; u32 buf; int cnt; } Bits;

static _Noreturn void corrupt(const char *why)
{
    fprintf(stderr, "boot-missing: rnc-%s\n", why);
    fflush(stderr);
    _Exit(78);
}
static u32 word(const Bits *b)
{
    return b->p + 2 <= b->end ? (u32)(b->p[0] | (b->p[1] << 8)) : 0u;
}
static u32 get(Bits *b, int n)
{
    while (b->cnt < n) {
        if (b->p + 2 > b->end) corrupt("truncated");
        b->buf |= word(b) << b->cnt;
        b->p += 2;
        b->cnt += 16;
    }
    u32 v = b->buf & ((1u << n) - 1);
    b->buf >>= n;
    b->cnt -= n;
    return v;
}
static u32 peek16(const Bits *b)
{
    u32 w = word(b);
    return (b->buf | (w << b->cnt)) & 0xFFFFu;
}
static void read_table(Bits *b, Table *t)
{
    u8 len[32];
    u32 n = get(b, 5), next = 0, step = 0x80000000u;
    memset(t, 0, sizeof *t);
    if (n > 32) corrupt("table");
    for (u32 i = 0; i < n; i++) len[i] = (u8)get(b, 4);
    for (int bits = 1; bits <= 15; bits++, step >>= 1)
        for (u32 i = 0; i < n; i++) {
            if (len[i] != bits) continue;
            if (t->count == 16) corrupt("table");
            u32 rev = 0;
            for (int k = 0; k < bits; k++) if (next & (0x80000000u >> k)) rev |= 1u << k;
            t->leaf[t->count++] = (Leaf){(u16)((1u << bits) - 1), (u16)rev, (u8)bits, (u8)i};
            next += step;
        }
}
static u32 symbol(Bits *b, const Table *t)
{
    u32 w = peek16(b);
    int i = 0;
    while (i < t->count && (w & t->leaf[i].mask) != t->leaf[i].code) i++;
    if (i == t->count) corrupt("code");
    get(b, t->leaf[i].bits);
    u32 v = t->leaf[i].symbol;
    if (v >= 2) v = (1u << (v - 1)) | get(b, (int)v - 1);
    return v;
}

void n64c_rnc1_unpack(const void *src, void *dst, s32 packed_size, s32 unpacked_size)
{
    if (!src || !dst || packed_size < 0 || unpacked_size < 0) corrupt("arguments");
    /* The counted packed size stops one 16-bit word short of the last refill, which
     * the format needs; the original unpacker reads it too. */
    Bits b = {src, (const u8 *)src + packed_size + 2, 0, 0};
    u8 *out = dst, *const out_end = out + unpacked_size;
    Table t[3];
    get(&b, 2); /* lock and key flags */
    do {
        read_table(&b, &t[0]);
        read_table(&b, &t[1]);
        read_table(&b, &t[2]);
        u32 runs = (get(&b, 16) - 1) & 0xFFFFu;
        for (;;) {
            u32 n = symbol(&b, &t[0]);
            if (n > (u32)(out_end - out) || n > (u32)(b.end - b.p)) corrupt("literal");
            memcpy(out, b.p, n);
            out += n;
            b.p += n;
            if (!runs--) break;
            u32 dist = symbol(&b, &t[1]) + 1, len = symbol(&b, &t[2]) + 2;
            if (dist > (u32)(out - (u8 *)dst) || len > (u32)(out_end - out)) corrupt("match");
            for (; len; len--, out++) *out = out[-(ptrdiff_t)dist];
        }
    } while (out < out_end && b.p < b.end - 2);
    if (out != out_end) corrupt("short");
}
/* RNC method 2 from the public format description (MultimediaWiki "RNC ProPack", version 2): an LZ77
 * stream without Huffman tables. Flag bits are read most significant bit first from whole bytes, a new
 * flag byte taken from the stream when the previous eight bits are used up; literal bytes, offset low
 * bytes and length bytes are read from the stream between them. After two unused bits:
 *   0                      literal byte
 *   10 x [1 y]             match of 4+x bytes, or with the extension (3+x)*2+y bytes (6..8);
 *                          extension value 9 is a raw run of (4 bits + 3) * 4 literal bytes
 *   110                    2-byte match at distance byte+1
 *   1110                   3-byte match
 *   1111 byte              match of byte+8 bytes; byte 0 ends a chunk, then one bit: 1 more chunks, 0 end
 * A match distance (other than the 2-byte form) is a small prefix code giving the high bits, then a byte:
 *   0 -> 0; 1 0 0 b -> 2+b; 1 1 0 -> 1; 1 x 1 y 1 -> 4+2x+y; 1 x 1 y 0 z -> 2*(4+2x+y)+z; distance (hi<<8)+byte+1.
 * The header's two CRC-16s (reflected polynomial 0xA001, initial 0) are checked on both sides. */
typedef struct { const u8 *p, *end; u32 buf; int cnt; } Bits2;
static _Noreturn void corrupt2(const char *why)
{
    fprintf(stderr, "boot-missing: rnc2-%s\n", why);
    fflush(stderr);
    _Exit(78);
}
static u32 byte2(Bits2 *b)
{
    if (b->p >= b->end) corrupt2("truncated");
    return *b->p++;
}
static u32 bit2(Bits2 *b)
{
    if (!b->cnt) {
        b->buf = byte2(b);
        b->cnt = 8;
    }
    b->cnt--;
    return (b->buf >> b->cnt) & 1u;
}
static u32 distance2(Bits2 *b)
{
    u32 high = 0;
    if (bit2(b)) {
        high = bit2(b);
        if (bit2(b)) {
            high = high * 2 + 4 + bit2(b);
            if (!bit2(b)) high = high * 2 + bit2(b);
        } else if (!high) {
            high = bit2(b) + 2;
        }
    }
    return (high << 8) + byte2(b) + 1;
}
u16 n64c_rnc_crc16(const void *data, u32 size)
{
    const u8 *p = data;
    u32 crc = 0;
    while (size--) {
        crc ^= *p++;
        for (int i = 0; i < 8; i++) crc = crc & 1u ? (crc >> 1) ^ 0xA001u : crc >> 1;
    }
    return (u16)crc;
}
static u32 be(const u8 *p, int n)
{
    u32 v = 0;
    while (n--) v = v << 8 | *p++;
    return v;
}
void n64c_rnc2_unpack(const void *header, void *dst, s32 packed_size, s32 unpacked_size)
{
    const u8 *h = header;
    if (!h || !dst || packed_size < 0 || unpacked_size < 0) corrupt2("arguments");
    if (h[0] != 'R' || h[1] != 'N' || h[2] != 'C' || h[3] != 2) corrupt2("header");
    if (be(h + 4, 4) != (u32)unpacked_size || be(h + 8, 4) != (u32)packed_size) corrupt2("sizes");
    if (n64c_rnc_crc16(h + 18, (u32)packed_size) != be(h + 14, 2)) corrupt2("packed-crc");
    Bits2 b = {h + 18, h + 18 + packed_size, 0, 0};
    u8 *out = dst, *const out_end = out + unpacked_size;
    u32 len, dist;
    bit2(&b); /* lock and key flags */
    bit2(&b);
    while (out < out_end) {
        if (!bit2(&b)) {
            *out++ = (u8)byte2(&b);
            continue;
        }
        if (!bit2(&b)) {
            len = 4 + bit2(&b);
            if (bit2(&b)) {
                len = (len - 1) * 2 + bit2(&b);
                if (len == 9) {
                    len = 0;
                    for (int i = 0; i < 4; i++) len = len * 2 + bit2(&b);
                    for (len = (len + 3) * 4; len; len--) {
                        if (out == out_end) corrupt2("run");
                        *out++ = (u8)byte2(&b);
                    }
                    continue;
                }
            }
            dist = distance2(&b);
        } else if (bit2(&b)) {
            if (bit2(&b)) {
                len = byte2(&b) + 8;
                if (len == 8) {
                    if (!bit2(&b)) break; /* last chunk */
                    continue;
                }
            } else {
                len = 3;
            }
            dist = distance2(&b);
        } else {
            len = 2;
            dist = byte2(&b) + 1;
        }
        if (dist > (u32)(out - (u8 *)dst) || len > (u32)(out_end - out)) corrupt2("match");
        for (; len; len--, out++) *out = out[-(ptrdiff_t)dist];
    }
    if (out != out_end) corrupt2("short");
    if (n64c_rnc_crc16(dst, (u32)unpacked_size) != be(h + 12, 2)) corrupt2("unpacked-crc");
}
