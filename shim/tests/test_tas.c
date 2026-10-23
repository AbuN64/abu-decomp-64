/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Movie replay overrides the live pad per guest poll, records what the game read,
 * hands the pad back to the live source after the movie, and exits cleanly a fixed
 * number of polls after the movie ends. N64C_INPUT feeds channel 0 from datagrams: the
 * newest wins, a press released before the poll still counts once, and every poll is recorded. */
#include "../n64c_cont.h"
#include "../n64c_tas.h"
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

static void live(int port, OSContPad *pad, void *user)
{
    (void)port; (void)user;
    pad->button = 0x0020; /* L held on the live device; the movie must win */
}

static void send_pad(const char *path, const char *text)
{
    struct sockaddr_un addr = {.sun_family = AF_UNIX};
    socklen_t size = sizeof addr;
    strcpy(addr.sun_path, path);
    if (path[0] == '@') {
        addr.sun_path[0] = 0;
        size = (socklen_t)(offsetof(struct sockaddr_un, sun_path) + strlen(path));
    }
    int fd = socket(AF_UNIX, SOCK_DGRAM, 0);
    CHECK(fd >= 0 && sendto(fd, text, strlen(text), 0, (struct sockaddr *)&addr, size) == (ssize_t)strlen(text));
    close(fd);
}

/* A child with only N64C_INPUT + N64C_TAS_RECORD: the parent drives it through the socket, a pipe paces the polls. */
static void live_input(void)
{
    const char *sock = "n64c_test_tas.sock", *log = "n64c_test_tas.live";
    remove(log);
    int go[2], ready[2];
    CHECK(pipe(go) == 0 && pipe(ready) == 0);
    pid_t child = fork();
    if (child == 0) {
        unsetenv("N64C_TAS_PLAY");
        unsetenv("N64C_TAS_EXIT_AFTER");
        unsetenv("N64C_TAS_HASH");
        setenv("N64C_INPUT", sock, 1);
        setenv("N64C_TAS_RECORD", log, 1);
        n64c_cont_set_input(NULL, NULL);
        OSContPad pads[MAXCONTROLLERS];
        char c;
        int code = 0;
        for (int i = 0; i < 4 && !code; i++) {
            osContStartReadData(NULL);
            osContGetReadData(pads);
            if (i == 0 && (pads[0].button || n64c_tas_live_seq() != 0)) code = 4; /* nothing sent yet: neutral */
            if (i == 1 && !(pads[0].button == 0x9000 && pads[0].stick_x == 10 && n64c_tas_live_seq() == 2)) code = 5;
            if (i == 2 && !(pads[0].button == 0x1000 && pads[0].stick_y == -20)) code = 6; /* A was released */
            if (write(ready[1], "r", 1) != 1 || read(go[0], &c, 1) != 1) code = 7;
        }
        _Exit(code);
    }
    char c;
    CHECK(read(ready[0], &c, 1) == 1);
    send_pad(sock, "1 8000 0 0");     /* A tapped and let go before the next poll ... */
    send_pad(sock, "2 1000 10 0");    /* ... while START is held: the poll sees both */
    CHECK(write(go[1], "g", 1) == 1 && read(ready[0], &c, 1) == 1);
    send_pad(sock, "3 1000 0 -20");
    send_pad(sock, "garbage");        /* refused, the pad stays */
    CHECK(write(go[1], "g", 1) == 1 && read(ready[0], &c, 1) == 1);
    CHECK(write(go[1], "g", 1) == 1 && read(ready[0], &c, 1) == 1);
    CHECK(write(go[1], "g", 1) == 1);
    int status = 0;
    CHECK(waitpid(child, &status, 0) == child);
    CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0);
    char got[256] = {0};
    FILE *f = fopen(log, "r");
    CHECK(f != NULL);
    if (f) { CHECK(fread(got, 1, sizeof got - 1, f) > 0); fclose(f); }
    CHECK(strcmp(got, "0000 0 0\n9000 10 0\n1000 0 -20\n1000 0 -20\n") == 0);
    remove(log);
    remove(sock);
    close(go[0]); close(go[1]); close(ready[0]); close(ready[1]);
}

/* A movie run taken over at its third poll: two movie polls, then the socket's pads; exit_after no longer ends it. */
static void take_over(const char *movie)
{
    char sock[64];
    snprintf(sock, sizeof sock, "@n64c-test-tas-%ld", (long)getpid()); /* the abstract namespace the page uses */
    const char *log = "n64c_test_tas.taken";
    remove(log);
    int go[2], ready[2];
    CHECK(pipe(go) == 0 && pipe(ready) == 0);
    pid_t child = fork();
    if (child == 0) {
        setenv("N64C_TAS_PLAY", movie, 1);
        setenv("N64C_TAS_EXIT_AFTER", "0", 1);
        unsetenv("N64C_TAS_HASH");
        setenv("N64C_INPUT", sock, 1);
        setenv("N64C_TAS_RECORD", log, 1);
        n64c_cont_set_input(NULL, NULL);
        OSContPad pads[MAXCONTROLLERS];
        char c;
        int code = 0;
        for (int i = 0; i < 6 && !code; i++) {
            osContStartReadData(NULL);
            osContGetReadData(pads);
            if (i < 2 && n64c_tas_live_taken()) code = 4;
            if (i == 2 && !(n64c_tas_live_taken() && pads[0].button == 0x0010)) code = 5;
            if (write(ready[1], "r", 1) != 1 || read(go[0], &c, 1) != 1) code = 7;
        }
        _Exit(code);
    }
    char c;
    CHECK(read(ready[0], &c, 1) == 1);
    send_pad(sock, "7 0001 0 0"); /* before the take: dropped */
    CHECK(write(go[1], "g", 1) == 1 && read(ready[0], &c, 1) == 1);
    send_pad(sock, "take");
    send_pad(sock, "8 0010 0 0");
    for (int i = 0; i < 4; i++) CHECK(write(go[1], "g", 1) == 1 && read(ready[0], &c, 1) == 1);
    CHECK(write(go[1], "g", 1) == 1);
    int status = 0;
    CHECK(waitpid(child, &status, 0) == child);
    CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0); /* the movie is 3 polls with EXIT_AFTER 0: only the take kept it going */
    char got[256] = {0};
    FILE *f = fopen(log, "r");
    CHECK(f != NULL);
    if (f) { CHECK(fread(got, 1, sizeof got - 1, f) > 0); fclose(f); }
    CHECK(strcmp(got, "8000 0 0\n1000 -80 80\n0010 0 0\n0010 0 0\n0010 0 0\n0010 0 0\n") == 0);
    remove(log);
    close(go[0]); close(go[1]); close(ready[0]); close(ready[1]);
}

/* N64C_TAS_TAKE_AT=2: two movie polls, then the socket's pad from poll 3 with no "take" sent; the pad sent before
 * that poll counts, exit_after no longer ends the run. */
static void take_at(const char *movie)
{
    char sock[64];
    snprintf(sock, sizeof sock, "@n64c-test-tas-at-%ld", (long)getpid());
    const char *log = "n64c_test_tas.at";
    remove(log);
    int go[2], ready[2];
    CHECK(pipe(go) == 0 && pipe(ready) == 0);
    pid_t child = fork();
    if (child == 0) {
        setenv("N64C_TAS_PLAY", movie, 1);
        setenv("N64C_TAS_EXIT_AFTER", "0", 1);
        setenv("N64C_TAS_TAKE_AT", "2", 1);
        unsetenv("N64C_TAS_HASH");
        setenv("N64C_INPUT", sock, 1);
        setenv("N64C_TAS_RECORD", log, 1);
        n64c_cont_set_input(NULL, NULL);
        OSContPad pads[MAXCONTROLLERS];
        char c;
        int code = 0;
        for (int i = 0; i < 5 && !code; i++) {
            osContStartReadData(NULL);
            osContGetReadData(pads);
            if (i < 2 && n64c_tas_live_taken()) code = 4;
            if (i == 2 && !(n64c_tas_live_taken() && pads[0].button == 0x0010)) code = 5;
            if (write(ready[1], "r", 1) != 1 || read(go[0], &c, 1) != 1) code = 7;
        }
        _Exit(code);
    }
    char c;
    CHECK(read(ready[0], &c, 1) == 1);
    CHECK(write(go[1], "g", 1) == 1 && read(ready[0], &c, 1) == 1);
    send_pad(sock, "1 0010 0 0"); /* waiting when poll 3 comes: the take at that poll applies it */
    for (int i = 0; i < 3; i++) CHECK(write(go[1], "g", 1) == 1 && read(ready[0], &c, 1) == 1);
    CHECK(write(go[1], "g", 1) == 1);
    int status = 0;
    CHECK(waitpid(child, &status, 0) == child);
    CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0);
    char got[256] = {0};
    FILE *f = fopen(log, "r");
    CHECK(f != NULL);
    if (f) { CHECK(fread(got, 1, sizeof got - 1, f) > 0); fclose(f); }
    CHECK(strcmp(got, "8000 0 0\n1000 -80 80\n0010 0 0\n0010 0 0\n0010 0 0\n") == 0);
    remove(log);
    close(go[0]); close(go[1]); close(ready[0]); close(ready[1]);
}

int main(void)
{
    const unsigned char a[11] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11}, b[11] = {2, 1, 3, 4, 5, 6, 7, 8, 9, 10, 11};
    CHECK(n64c_tas_hash_bytes(a, sizeof a) == n64c_tas_hash_bytes(a, sizeof a));
    CHECK(n64c_tas_hash_bytes(a, sizeof a) != n64c_tas_hash_bytes(b, sizeof b)); /* order sensitive */
    CHECK(n64c_tas_hash_bytes(a, 10) != n64c_tas_hash_bytes(a, 11));             /* the tail counts */
    const char *movie = "n64c_test_tas.movie", *log = "n64c_test_tas.log", *hashes = "n64c_test_tas.hash";
    FILE *f = fopen(movie, "w");
    CHECK(f != NULL);
    fputs("# raw movie\n8000 0 0\n1000 -80 80\n\n0400 5 -5\n", f);
    fclose(f);
    remove(log);
    setenv("N64C_TAS_PLAY", movie, 1);
    setenv("N64C_TAS_RECORD", log, 1);
    setenv("N64C_TAS_EXIT_AFTER", "2", 1);
    setenv("N64C_TAS_HASH", hashes, 1);
    n64c_cont_set_input(live, NULL);
    pid_t child = fork();
    if (child == 0) {
        OSContPad pads[MAXCONTROLLERS];
        for (int i = 0; i < 10; i++) {
            osContStartReadData(NULL);
            osContGetReadData(pads);
            if (i == 1 && !(pads[0].button == 0x1000 && pads[0].stick_x == -80 && pads[0].stick_y == 80)) _Exit(4);
            if (i == 3 && pads[0].button != 0x0020) _Exit(5); /* the live pad takes over after the movie */
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
    CHECK(strcmp(got, "8000 0 0\n1000 -80 80\n0400 5 -5\n0020 0 0\n0020 0 0\n") == 0);
    f = fopen(hashes, "r");
    CHECK(f != NULL);
    int lines = 0;
    for (int c; f && (c = fgetc(f)) != EOF;) lines += c == '\n';
    if (f) fclose(f);
    CHECK(lines == 5); /* one RAM hash per poll the game made (no RAM here: hash 0) */
    take_over(movie);
    take_at(movie);
    remove(movie);
    remove(log);
    remove(hashes);
    live_input();
    if (!fails) puts("n64c_tas ok");
    return fails != 0;
}
