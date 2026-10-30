/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Compile this same unit with each native target compiler. The compiler
 * boundary must supply va_list, alignment, forwarding and copying itself. */
#include "../boot/native.h"

static int marker;

static int values(va_list source)
{
    va_list copy;
    int failed = 0;
    va_copy(copy, source);
    failed |= va_arg(copy, int) != -9;
    failed |= va_arg(copy, double) != 1.25;
    failed |= va_arg(copy, long long) != 0x123456789abcdefLL;
    failed |= va_arg(copy, void *) != &marker;
    failed |= va_arg(copy, int) != 37;
    failed |= va_arg(copy, double) != -2.5;
    va_end(copy);
    return failed;
}

static int forward(int tag, ...)
{
    va_list args;
    int failed = tag != 71;
    va_start(args, tag);
    failed |= values(args);
    /* values() must leave the caller's list available for another copy. */
    failed |= values(args);
    va_end(args);
    return failed;
}

int main(void)
{
    return forward(71, (short)-9, 1.25, 0x123456789abcdefLL,
                   (void *)&marker, 37, -2.5);
}
