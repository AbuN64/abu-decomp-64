/* The port links the shim and the shim answers: a matrix identity through the n64 graphics utilities. */
#include "n64c_gu.h"

#include <stdio.h>

int main(void) {
    u32 matrix[16];
    guMtxIdent(matrix);
    if (matrix[0] != 0x00010000u || matrix[8] != 0u) {
        fprintf(stderr, "shim link: unexpected identity words\n");
        return 1;
    }
    return 0;
}
