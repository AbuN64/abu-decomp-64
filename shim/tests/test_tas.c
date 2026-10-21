/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Movie replay overrides the live pad per guest poll, records what the game read,
 * and exits cleanly a fixed number of polls after the movie ends. */
#include "../n64c_cont.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

static void live(int port, OSContPad *pad, void *user)
{
    (void)port; (void)user;
    pad->button = 0x0020; /* L held on the live device; the movie must win */
}

int main(void)
{
    const char *movie = "n64c_test_tas.movie", *log = "n64c_test_tas.log";
    FILE *f = fopen(movie, "w");
    CHECK(f != NULL);
    fputs("# raw movie\n8000 0 0\n1000 -80 80\n\n0400 5 -5\n", f);
    fclose(f);
    remove(log);
    setenv("N64C_TAS_PLAY", movie, 1);
    setenv("N64C_TAS_RECORD", log, 1);
    setenv("N64C_TAS_EXIT_AFTER", "2", 1);
    n64c_cont_set_input(live, NULL);
    pid_t child = fork();
    if (child == 0) {
        OSContPad pads[MAXCONTROLLERS];
        for (int i = 0; i < 10; i++) {
            osContStartReadData(NULL);
            osContGetReadData(pads);
            if (i == 1 && !(pads[0].button == 0x1000 && pads[0].stick_x == -80 && pads[0].stick_y == 80)) _Exit(4);
            if (i == 3 && pads[0].button != 0) _Exit(5); /* neutral after the movie, not the live pad */
        }
        _Exit(3); /* EXIT_AFTER never fired */
    }
    int status = 0;
    CHECK(waitpid(child, &status, 0) == child);
    CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0);
    char got[256] = {0};
    f = fopen(log, "r");
    CHECK(f != NULL);
    if (f) { CHECK(fread(got, 1, sizeof got - 1, f) > 0); fclose(f); }
    CHECK(strcmp(got, "8000 0 0\n1000 -80 80\n0400 5 -5\n0000 0 0\n0000 0 0\n") == 0);
    remove(movie);
    remove(log);
    if (!fails) puts("n64c_tas ok");
    return fails != 0;
}
