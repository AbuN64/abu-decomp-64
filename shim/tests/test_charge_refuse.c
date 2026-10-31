/* SPDX-License-Identifier: GPL-3.0-or-later */
/* A link with no work declaration (no charged table) refuses at startup instead of charging zero. */
#include "../n64c_charge.h"
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

int main(void)
{
    fflush(NULL);
    pid_t child = fork();
    if (child == 0) {
        freopen("/dev/null", "w", stderr);
        n64c_charge_init();
        _exit(0); /* reached only if the missing declaration was accepted */
    }
    int status = 0;
    waitpid(child, &status, 0);
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 7) {
        fprintf(stderr, "charge_refuse: init of a link with no declaration exited %d, wanted 7\n", status);
        return 1;
    }
    printf("charge_refuse: ok\n");
    return 0;
}
