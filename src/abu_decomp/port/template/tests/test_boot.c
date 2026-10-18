#include "boot/boot.h"

#include <stdio.h>
#include <string.h>

int main(void) {
    const char *expected =
        "{{boot_subsystem}}: original {{boot_label}} owner {{boot_owner}} has no native implementation";
    if (strcmp(Boot_StopMessage(), expected) != 0) {
        fprintf(stderr, "stop message: %s\n", Boot_StopMessage());
        return 1;
    }
    return 0;
}
