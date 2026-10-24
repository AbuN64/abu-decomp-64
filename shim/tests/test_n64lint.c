/* SPDX-License-Identifier: GPL-3.0-or-later */
#define N64LINT_NO_MAIN
#include "../tools/n64lint.c"
#include "test_check.h"

static const char *const ledger =
    "symbol\tkind\tsubsystem\tshim_component\tnative_replacement\tremoval_lane\tstatus\n"
    "osCreateMesgQueue\tfunc\tOS\tLUS\tgame loop\tport-fork\topen\n"
    "gDPPipeSync\tmacro\tGBI\tLUS\tR_ batches\tport-fork\topen\n"
    "osViBlack\tfunc\tOS\tLUS\tSys_\tport-fork\tremoved\n";

/* Runs one fixture; returns the count for the given rule and the total. */
static int run(const char *path, const char *text, long open_count, long debt, int rule,
               int *total) {
    LintSource src = {path, text, strlen(text)};
    LintReport rep = {0};
    *total = n64lint_check(ledger, open_count, debt, &src, 1, &rep);
    int n = rep.rule[rule];
    free(rep.buf);
    return n;
}

static bool names(const char *path, const char *text, const char *expect) {
    LintSource src = {path, text, strlen(text)};
    LintReport rep = {0};
    n64lint_check(ledger, 2, 0, &src, 1, &rep);
    bool ok = rep.buf && strstr(rep.buf, expect);
    free(rep.buf);
    return ok;
}

int main(void) {
    int total;
    /* green: open names are allowed in game code, boundary code is exempt, comments and strings are
     * ignored */
    Test_Check(run("src/app/ok.c",
                   "void f(void){ osCreateMesgQueue(); gDPPipeSync(); }\n/* osViBlack u8 */ const "
                   "char *s = \"Gfx\";\n",
                   2, 0, 1, &total) == 0 &&
                   total == 0,
               "open names pass in game code");
    Test_Check(run("platform/port/shim.c", "u32 x; osViBlack(); Gfx g; int a = 0xA4040000;\n",
                   2, 0, 1, &total) == 0 &&
                   total == 0,
               "the port boundary directory is exempt");
    Test_Check(run("src/common/guard.h", "#ifndef G_GAME_H\n#define G_GAME_H\n#endif\n", 2, 0, 4,
                   &total) == 0 &&
                   total == 0,
               "include guards ending _H are not names");
    /* rule 1 */
    Test_Check(run("src/app/a.c", "u32 x;\nosSetTimer();\n", 2, 0, 1, &total) == 2,
               "rule 1: unlisted N64 names");
    Test_Check(run("src/app/a.c", "int a = 0xA4040010;\n", 2, 0, 1, &total) == 1,
               "rule 1: RCP address literal");
    Test_Check(names("src/app/a.c", "Mtx m;\n", "src/app/a.c:1: Mtx"),
               "rule 1 prints file:line: name");
    /* rule 2 */
    Test_Check(run("src/app/a.c", "int x;\n", 1, 0, 2, &total) == 1,
               "rule 2: open rows exceed OPEN_COUNT");
    /* rule 3 */
    Test_Check(run("src/app/a.c", "void f(void){ osViBlack(); }\n", 2, 0, 3, &total) == 1,
               "rule 3: removed row used");
    /* rule 4 */
    Test_Check(run("src/renderer/a.c", "gDPPipeSync();\n", 2, 0, 4, &total) == 1,
               "rule 4: open name in native dir");
    Test_Check(run("tools/x.c", "f32 x;\n", 2, 0, 4, &total) == 1, "rule 4: N64 name in tools");
    Test_Check(run("src/assets/a.c", "int a = 0xa4300000;\n", 2, 0, 4, &total) == 1,
               "rule 4: RCP literal in native dir");
    Test_Check(run("src/game/a.c", "f32 x;\n", 2, 0, 4, &total) == 1,
               "rule 4: N64 name in src/game");
    Test_Check(run("src/install/a.c", "f32 x;\n", 2, 0, 4, &total) == 1,
               "rule 4: N64 name in src/install");
    Test_Check(run("src/native/a.c", "f32 x;\n", 2, 0, 4, &total) == 1,
               "rule 4: N64 name in src/native");
    /* rule 5 */
    Test_Check(
        run("src/app/a.c",
            "#ifdef VERSION_US\nint x;\n#endif\n#if defined(VERSION_EU)\n#endif\nint VERSION_X;\n",
            2, 1, 5, &total) == 3,
        "rule 5: VERSION_ conditionals exceed debt (two sites and the summary)");
    Test_Check(run("src/app/a.c", "#ifdef VERSION_US\n#endif\n", 2, 1, 5, &total) == 0 &&
                   total == 0,
               "rule 5: within debt passes");
    return 0;
}
