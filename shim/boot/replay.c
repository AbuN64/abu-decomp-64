/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Function replay (`decomp behave --replay`): run one native function on recorded inputs, in isolation.
 *
 * With N64C_REPLAY=PLAN the game executable opens its address windows and loads the data image as for a boot, then
 * calls n64c_replay_run instead of starting the game: no scheduler, no frame, no game thread. The plan is a text file
 * (the recorder's inputs already laid out in this build's native shape by the tool that writes it):
 *     out PATH              result file (appended; one write per line)
 *     timeout SECONDS       wall clock one case may take (default 5)
 *     case ID               start a case
 *     fn ADDR RET           native entry address (hex) and result kind: v void, i 32-bit, q 64-bit, f float, d double
 *     args W...             hex 32-bit words pushed in order (cdecl: the callee reads what its prototype names)
 *     in PHYS WIDTH VALUE   store VALUE (hex, 1/2/4/8 bytes, little-endian) at that RDRAM physical address
 *     want PHYS WIDTH       read it back after the call
 *     go                    run the case
 * Every case runs in a forked child of the loaded image, so one case never sees another's stores and a crash costs
 * one case. Results, one line each:
 *     res ID ok EAX EDX RETBITS      the call returned; RETBITS are the float/double bits (hex) for kinds f and d
 *     got PHYS WIDTH VALUE           a wanted location after the call (only after res ok)
 *     res ID crash SIGNAL ADDR EIP   a fault; EIP is the faulting instruction
 *     res ID exit CODE               the call ended the program (a stand-in for assembly stops with 78)
 *     res ID timeout                 over the wall clock
 *     res ID nomem PHYS              an input outside RDRAM
 * The function's own callees run natively too; shim functions they reach run against an unstarted shim, so a case
 * that faults inside the shim is the tool's to classify (EIP), never the game's. */
#define _GNU_SOURCE
#include "n64c_memory.h"
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <ucontext.h>
#include <unistd.h>

typedef struct {
    u32 phys;
    unsigned width;
    uint64_t value;
} ReplayAccess;

typedef struct {
    char id[64];
    uintptr_t entry;
    char kind;
    u32 args[16];
    unsigned nargs;
    ReplayAccess *in, *want;
    size_t nin, nwant;
} ReplayCase;

static int result_fd = -1;
static char current_id[64];
static unsigned timeout_seconds = 5;

static void say(const char *format, ...) __attribute__((format(printf, 1, 2)));
static void say(const char *format, ...)
{
    char line[512];
    va_list ap;
    va_start(ap, format);
    int n = vsnprintf(line, sizeof line, format, ap);
    va_end(ap);
    if (n > 0 && result_fd >= 0)
        (void)!write(result_fd, line, (size_t)(n < (int)sizeof line ? n : (int)sizeof line - 1));
}

static void on_fault(int signo, siginfo_t *info, void *context)
{
    const ucontext_t *uc = context;
    say("res %s crash %d %lx %lx\n", current_id, signo, (unsigned long)(uintptr_t)info->si_addr,
        (unsigned long)uc->uc_mcontext.gregs[REG_EIP]);
    _exit(0);
}

static void on_alarm(int signo)
{
    (void)signo;
    say("res %s timeout\n", current_id);
    _exit(0);
}

static uint64_t load(const void *p, unsigned width)
{
    uint64_t v = 0;
    memcpy(&v, p, width);
    return v;
}

typedef u32 (*CallI)(u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32);
typedef uint64_t (*CallQ)(u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32);
typedef float (*CallF)(u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32);
typedef double (*CallD)(u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32, u32);

static void child(const ReplayCase *c)
{
    snprintf(current_id, sizeof current_id, "%s", c->id);
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    static char alt[65536];
    stack_t stack = {.ss_sp = alt, .ss_size = sizeof alt};
    sigaltstack(&stack, NULL);
    sa.sa_sigaction = on_fault;
    sa.sa_flags = SA_SIGINFO | SA_ONSTACK;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGSEGV, &sa, NULL);
    sigaction(SIGBUS, &sa, NULL);
    sigaction(SIGFPE, &sa, NULL);
    sigaction(SIGILL, &sa, NULL);
    sigaction(SIGABRT, &sa, NULL);
    signal(SIGALRM, on_alarm);
    alarm(timeout_seconds);
    for (size_t i = 0; i < c->nin; i++) {
        void *p = n64c_memory_ram(c->in[i].phys, c->in[i].width);
        if (!p) {
            say("res %s nomem %x\n", c->id, c->in[i].phys);
            _exit(0);
        }
        memcpy(p, &c->in[i].value, c->in[i].width);
    }
    u32 a[16];
    memset(a, 0, sizeof a);
    memcpy(a, c->args, sizeof(u32) * c->nargs);
    u32 eax = 0, edx = 0;
    uint64_t bits = 0;
    if (c->kind == 'q') {
        uint64_t r = ((CallQ)c->entry)(a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7], a[8], a[9], a[10], a[11], a[12],
                                       a[13], a[14], a[15]);
        eax = (u32)r;
        edx = (u32)(r >> 32);
    } else if (c->kind == 'f') {
        float r = ((CallF)c->entry)(a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7], a[8], a[9], a[10], a[11], a[12],
                                    a[13], a[14], a[15]);
        memcpy(&bits, &r, sizeof r);
    } else if (c->kind == 'd') {
        double r = ((CallD)c->entry)(a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7], a[8], a[9], a[10], a[11], a[12],
                                     a[13], a[14], a[15]);
        memcpy(&bits, &r, sizeof r);
    } else {
        eax = ((CallI)c->entry)(a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7], a[8], a[9], a[10], a[11], a[12], a[13],
                                a[14], a[15]);
    }
    alarm(0);
    say("res %s ok %x %x %llx\n", c->id, eax, edx, (unsigned long long)bits);
    for (size_t i = 0; i < c->nwant; i++) {
        const void *p = n64c_memory_ram(c->want[i].phys, c->want[i].width);
        say("got %x %u %llx\n", c->want[i].phys, c->want[i].width,
            (unsigned long long)(p ? load(p, c->want[i].width) : 0));
    }
    _exit(0);
}

static int run_case(const ReplayCase *c)
{
    fflush(NULL);
    pid_t pid = fork();
    if (pid < 0)
        return -1;
    if (!pid)
        child(c);
    int status = 0;
    while (waitpid(pid, &status, 0) < 0 && errno == EINTR) {
    }
    if (WIFSIGNALED(status))  /* killed without a line of its own */
        say("res %s crash %d 0 0\n", c->id, WTERMSIG(status));
    else if (WEXITSTATUS(status))  /* the program ended itself, as a boot-missing stop does with 78 */
        say("res %s exit %d\n", c->id, WEXITSTATUS(status));
    return 0;
}

static void push(ReplayAccess **list, size_t *count, ReplayAccess item)
{
    ReplayAccess *grown = realloc(*list, (*count + 1) * sizeof **list);
    if (!grown)
        abort();
    *list = grown;
    (*list)[(*count)++] = item;
}

static void clear(ReplayCase *c)
{
    free(c->in);
    free(c->want);
    memset(c, 0, sizeof *c);
}

int n64c_replay_run(const char *plan)
{
    FILE *file = fopen(plan, "r");
    if (!file) {
        fprintf(stderr, "replay: cannot read %s: %s\n", plan, strerror(errno));
        return 2;
    }
    ReplayCase c;
    memset(&c, 0, sizeof c);
    char line[4096];
    unsigned cases = 0;
    while (fgets(line, sizeof line, file)) {
        char word[16], text[1024];
        unsigned long a, b;
        unsigned long long v;
        if (sscanf(line, "%15s", word) != 1)
            continue;
        const char *rest = line + strlen(word);
        if (!strcmp(word, "out") && sscanf(rest, " %1023s", text) == 1) {
            if (result_fd >= 0)
                close(result_fd);
            result_fd = open(text, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
            if (result_fd < 0) {
                fprintf(stderr, "replay: cannot write %s: %s\n", text, strerror(errno));
                return 2;
            }
        } else if (!strcmp(word, "timeout") && sscanf(rest, " %lu", &a) == 1) {
            timeout_seconds = (unsigned)a;
        } else if (!strcmp(word, "case")) {
            clear(&c);
            if (sscanf(rest, " %63s", c.id) != 1)
                c.id[0] = 0;
        } else if (!strcmp(word, "fn") && sscanf(rest, " %lx %c", &a, &text[0]) == 2) {
            c.entry = (uintptr_t)a;
            c.kind = text[0];
        } else if (!strcmp(word, "args")) {
            const char *p = rest;
            char *end;
            for (c.nargs = 0; c.nargs < 16; c.nargs++) {
                unsigned long w = strtoul(p, &end, 16);
                if (end == p)
                    break;
                c.args[c.nargs] = (u32)w;
                p = end;
            }
        } else if (!strcmp(word, "in") && sscanf(rest, " %lx %lu %llx", &a, &b, &v) == 3 && (b == 1 || b == 2 || b == 4 || b == 8)) {
            push(&c.in, &c.nin, (ReplayAccess){(u32)a, (unsigned)b, v});
        } else if (!strcmp(word, "want") && sscanf(rest, " %lx %lu", &a, &b) == 2 && (b == 1 || b == 2 || b == 4 || b == 8)) {
            push(&c.want, &c.nwant, (ReplayAccess){(u32)a, (unsigned)b, 0});
        } else if (!strcmp(word, "go")) {
            if (!c.entry) {
                say("res %s nofn\n", c.id);
                continue;
            }
            if (run_case(&c)) {
                fprintf(stderr, "replay: fork failed: %s\n", strerror(errno));
                return 1;
            }
            cases++;
        }
    }
    clear(&c);
    fclose(file);
    if (result_fd >= 0)
        close(result_fd);
    fprintf(stderr, "replay: %u cases\n", cases);
    return 0;
}
