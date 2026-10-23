/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef TEST_CHECK_H
#define TEST_CHECK_H
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
static inline void Test_Check(bool condition, const char *description) {
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", description);
        exit(EXIT_FAILURE);
    }
}
#endif
