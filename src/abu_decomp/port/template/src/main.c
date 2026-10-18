#include "boot/boot.h"
#include "platform/sys_public.h"

#include <stdio.h>
#include <string.h>

int main(int argc, char **argv) {
    Sys_Options options = {0};
    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--headless") == 0) {
            options.headless = true;
        } else if (strcmp(argv[i], "--help") == 0) {
            puts("usage: {{executable}} [--headless]");
            return 0;
        } else {
            fprintf(stderr, "{{executable}}: unknown option %s\n", argv[i]);
            return 2;
        }
    }
    if (!Sys_Init(&options))
        return 1;
    Boot_Run();
}
