/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "n64c_cpak.h"
#include <string.h>
static void put16(u8 *p, u16 v) { p[0] = (u8)(v >> 8); p[1] = (u8)v; }
static u16 get16(const u8 *p) { return (u16)((p[0] << 8) | p[1]); }
void n64c_cpak_id_sums(const u8 *id, u16 *sum, u16 *inverted)
{
    u16 s = 0;
    for (int i = 0; i < 28; i += 2)
        s = (u16)(s + get16(id + i));
    *sum = s;
    *inverted = (u16)(0xFFF2 - s);
}
int n64c_cpak_id_ok(const u8 *id)
{
    u16 sum, inverted;
    n64c_cpak_id_sums(id, &sum, &inverted);
    return get16(id + 0x1C) == sum && get16(id + 0x1E) == inverted && (get16(id + 0x18) & 1) && id[0x1A] == 1;
}
void n64c_cpak_index_seal(u8 *image)
{
    u8 *index = image + N64C_CPAK_PAGE;
    u8 sum = 0;
    for (unsigned i = N64C_CPAK_FIRST_DATA_PAGE; i < N64C_CPAK_PAGES; i++)
        sum = (u8)(sum + index[i * 2 + 1]);
    index[0] = 0;
    index[1] = sum;
    memcpy(image + 2 * N64C_CPAK_PAGE, index, N64C_CPAK_PAGE);
}
void n64c_cpak_format(u8 *image)
{
    static const unsigned id_at[4] = {0x20, 0x60, 0x80, 0xC0};
    memset(image, 0, N64C_CPAK_BYTES);
    for (int b = 0; b < 4; b++) {
        u8 *id = image + id_at[b];
        for (int i = 0; i < 24; i++)
            id[i] = (u8)(0xA5 ^ (i * 29)); /* a fixed serial: the same card every time */
        put16(id + 0x18, 0x0001);          /* device: memory card */
        id[0x1A] = 1;                      /* banks */
        id[0x1B] = 1;                      /* version */
        u16 sum, inverted;
        n64c_cpak_id_sums(id, &sum, &inverted);
        put16(id + 0x1C, sum);
        put16(id + 0x1E, inverted);
    }
    u8 *index = image + N64C_CPAK_PAGE;
    for (unsigned i = 1; i < N64C_CPAK_FIRST_DATA_PAGE; i++)
        put16(index + i * 2, N64C_CPAK_INDEX_END);
    for (unsigned i = N64C_CPAK_FIRST_DATA_PAGE; i < N64C_CPAK_PAGES; i++)
        put16(index + i * 2, N64C_CPAK_INDEX_FREE);
    n64c_cpak_index_seal(image);
}
u8 n64c_cpak_data_crc(const u8 *bytes)
{
    u8 crc = 0;
    for (unsigned i = 0; i < 32; i++) {
        crc ^= bytes[i];
        for (unsigned bit = 0; bit < 8; bit++)
            crc = (u8)((crc << 1) ^ ((crc & 0x80) ? 0x85 : 0));
    }
    return crc;
}
u8 n64c_cpak_address_crc(u16 block)
{
    unsigned reg = 0;
    for (int bit = 10; bit >= -5; bit--) {
        unsigned in = bit >= 0 ? (block >> bit) & 1u : 0u, top = (reg >> 4) & 1u;
        reg = ((reg << 1) | in) & 0x1Fu;
        if (top)
            reg ^= 0x15u;
    }
    return (u8)reg;
}
