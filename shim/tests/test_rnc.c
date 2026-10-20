/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "n64c_rnc.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "FAIL:%d:%s\n", __LINE__, #x); exit(1); } } while (0)

/* Test-only RNC1 writer: bits fill 16-bit little-endian words allocated on first use,
 * raw literals are appended at the current byte position. */
static unsigned char stream[64];
static int pos, slot, wcnt;
static void emit(unsigned v, int n)
{
    for (int i = 0; i < n; i++) {
        if (!wcnt) { slot = pos; pos += 2; stream[slot] = stream[slot + 1] = 0; }
        if ((v >> i) & 1) stream[slot + wcnt / 8] |= (unsigned char)(1u << (wcnt % 8));
        wcnt = (wcnt + 1) % 16;
    }
}
static void literal(const char *s) { memcpy(stream + pos, s, strlen(s)); pos += (int)strlen(s); }

static void expect_exit(int packed, int unpacked, const char *what)
{
    pid_t pid = fork();
    CHECK(pid >= 0);
    if (!pid) {
        unsigned char out[16];
        close(2);
        n64c_rnc1_unpack(stream, out, packed, unpacked);
        _Exit(0);
    }
    int status;
    CHECK(waitpid(pid, &status, 0) == pid);
    if (!(WIFEXITED(status) && WEXITSTATUS(status) == 78)) { fprintf(stderr, "FAIL: %s\n", what); exit(1); }
}

/* Test-only RNC2 writer: flag bits fill bytes most significant bit first, a flag byte allocated at the
 * current position when its first bit is written; bytes are appended at the current position. */
static unsigned char s2[2048];
static int p2, slot2, n2;
static void bits2(unsigned v, int n)
{
    for (int i = n - 1; i >= 0; i--) {
        if (!n2) { slot2 = p2++; s2[slot2] = 0; }
        if ((v >> i) & 1) s2[slot2] |= (unsigned char)(0x80u >> n2);
        n2 = (n2 + 1) % 8;
    }
}
static void byte2w(unsigned v) { s2[p2++] = (unsigned char)v; }
static void header2(const unsigned char *expect, int unpacked)
{
    int packed = p2 - 18;
    unsigned crc_un = n64c_rnc_crc16(expect, (u32)unpacked), crc_pk = n64c_rnc_crc16(s2 + 18, (u32)packed);
    memcpy(s2, "RNC\2", 4);
    for (int i = 0; i < 4; i++) {
        s2[4 + i] = (unsigned char)(unpacked >> (24 - 8 * i));
        s2[8 + i] = (unsigned char)(packed >> (24 - 8 * i));
    }
    s2[12] = (unsigned char)(crc_un >> 8); s2[13] = (unsigned char)crc_un;
    s2[14] = (unsigned char)(crc_pk >> 8); s2[15] = (unsigned char)crc_pk;
    s2[16] = s2[17] = 0;
}
static void start2(void) { p2 = 18; n2 = 0; bits2(0, 2); /* lock and key flags */ }
static void expect_exit2(const unsigned char *stream, int packed, int unpacked, const char *what)
{
    pid_t pid = fork();
    CHECK(pid >= 0);
    if (!pid) {
        static unsigned char out[1024];
        close(2);
        n64c_rnc2_unpack(stream, out, packed, unpacked);
        _Exit(0);
    }
    int status;
    CHECK(waitpid(pid, &status, 0) == pid);
    if (!(WIFEXITED(status) && WEXITSTATUS(status) == 78)) { fprintf(stderr, "FAIL: %s\n", what); exit(1); }
}
static void test_rnc2(void)
{
    /* CRC-16 reference value of the RNC/ARC CRC for "123456789". */
    CHECK(n64c_rnc_crc16("123456789", 9) == 0xBB3D);
    static const char want[] = "abcdddcddabcddddddddd0123456789AB0123456789z";
    const int size = (int)sizeof want - 1;
    start2();
    for (const char *c = "abcd"; *c; c++) { bits2(0, 1); byte2w((unsigned char)*c); } /* literals */
    bits2(6, 3); byte2w(0);                /* 110: 2 bytes at distance 1 */
    bits2(14, 4); bits2(0, 1); byte2w(3);  /* 1110: 3 bytes; distance 0 + byte 3 + 1 */
    bits2(2, 2); bits2(1, 1); bits2(0, 1); bits2(0, 1); byte2w(8); /* 10 x=1, no extension: 5 bytes at 9 */
    bits2(2, 2); bits2(0, 1); bits2(1, 1); bits2(1, 1); bits2(0, 1); byte2w(0); /* 10 x=0 ext y=1: 7 at 1 */
    bits2(2, 2); bits2(1, 1); bits2(1, 1); bits2(1, 1); bits2(0, 4); /* extension value 9: raw run of 12 */
    for (const char *c = "0123456789AB"; *c; c++) byte2w((unsigned char)*c);
    bits2(15, 4); byte2w(2); bits2(0, 1); byte2w(11); /* 1111 byte 2: 10 bytes at distance 12 */
    bits2(15, 4); byte2w(0); bits2(1, 1);  /* end of chunk, more follow */
    bits2(0, 1); byte2w('z');
    bits2(15, 4); byte2w(0); bits2(0, 1);  /* end of the last chunk */
    header2((const unsigned char *)want, size);
    unsigned char out[64] = {0};
    n64c_rnc2_unpack(s2, out, p2 - 18, size);
    CHECK(!memcmp(out, want, (size_t)size) && !out[size]);
    expect_exit2(s2, p2 - 19, size, "packed size differs from the header");
    s2[20] ^= 1;
    expect_exit2(s2, p2 - 18, size, "packed CRC mismatch");
    s2[20] ^= 1;
    s2[12] ^= 1;
    expect_exit2(s2, p2 - 18, size, "unpacked CRC mismatch");
    s2[12] ^= 1;
    s2[3] = 1;
    expect_exit2(s2, p2 - 18, size, "method 1 header");

    /* Long distances: 9 raw runs of 72 bytes, then matches with distance prefixes 110 (high 1) and 1000 (high 2). */
    static unsigned char big[700];
    int n = 0;
    start2();
    for (int r = 0; r < 9; r++) {
        bits2(2, 2); bits2(1, 1); bits2(1, 1); bits2(1, 1); bits2(15, 4);
        for (int i = 0; i < 72; i++, n++) { big[n] = (unsigned char)(n * 7 + 3); byte2w(big[n]); }
    }
    bits2(14, 4); bits2(6, 3); byte2w(0);     /* 3 bytes at distance 0x100 + 0 + 1 */
    for (int i = 0; i < 3; i++, n++) big[n] = big[n - 257];
    bits2(14, 4); bits2(8, 4);
    int far = p2;
    byte2w(4);                                 /* 3 bytes at distance 0x200 + 4 + 1 */
    for (int i = 0; i < 3; i++, n++) big[n] = big[n - 517];
    bits2(15, 4); byte2w(0); bits2(0, 1);
    header2(big, n);
    static unsigned char got[700];
    n64c_rnc2_unpack(s2, got, p2 - 18, n);
    CHECK(!memcmp(got, big, (size_t)n));
    s2[far] = 0xFF; /* the last distance byte now reaches before the output: refused, not read */
    header2(big, n);
    expect_exit2(s2, p2 - 18, n, "distance before the output");
}
int main(void)
{
    emit(0, 2);                       /* lock and key flags */
    emit(4, 5); emit(1, 4); emit(0, 4); emit(0, 4); emit(1, 4); /* literal table: symbols 0 and 3, 1 bit each */
    emit(1, 5); emit(1, 4);           /* distance table: symbol 0 */
    emit(2, 5); emit(0, 4); emit(1, 4); /* length table: symbol 1 */
    emit(2, 16);                      /* literal/match pairs + 1 */
    emit(1, 1); emit(0, 2); literal("abcd"); /* literal run of 4 */
    emit(0, 1); emit(0, 1);           /* match: distance 1, length 3 */
    emit(0, 1);                       /* literal run of 0 */
    unsigned char out[8] = {0};
    n64c_rnc1_unpack(stream, out, pos, 7);
    CHECK(!memcmp(out, "abcdddd", 7) && !out[7]);
    expect_exit(pos - 4, 7, "truncated stream");
    expect_exit(pos, 9, "output longer than the stream");
    test_rnc2();
    puts("ok");
    return 0;
}
