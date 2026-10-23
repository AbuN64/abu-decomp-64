/* SPDX-License-Identifier: GPL-3.0-or-later */
/* n64lint: keeps N64 names inside the quarantined compat boundary.
 * Rules (each violation prints "file:line: name"):
 *   1 an N64 name outside the shim and the port's --ledger directory that is not an open ledger row
 *   2 more open ledger rows than OPEN_COUNT
 *   3 a removed ledger row is used
 *   4 any N64 name in src/{common,platform,renderer,sound,input,assets,engine} or tools/
 *   5 more VERSION_ preprocessor conditionals than VERSION_DEBT
 * Include guards ending in _H are not names. Compile with N64LINT_NO_MAIN to reuse the core. */
#include <ctype.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    const char *path; /* repo-relative, forward slashes */
    const char *text;
    size_t len;
} LintSource;

typedef struct {
    char *buf;
    size_t len, cap;
    int total;
    int rule[6];
} LintReport;

typedef struct {
    char *name;
    bool open;
} LedgerRow;

typedef struct {
    LedgerRow *rows;
    size_t count, cap;
    long open;
} Ledger;

static void report_add(LintReport *r, int rule, const char *path, long line, const char *name)
{
    char tmp[512];
    int n = snprintf(tmp, sizeof tmp, "%s:%ld: %s\n", path, line, name);
    if (n < 0)
        return;
    size_t add = (size_t)n < sizeof tmp ? (size_t)n : sizeof tmp - 1;
    if (r->len + add + 1 > r->cap) {
        size_t cap = r->cap ? r->cap * 2 : 1024;
        while (cap < r->len + add + 1)
            cap *= 2;
        char *grown = realloc(r->buf, cap);
        if (!grown)
            return;
        r->buf = grown;
        r->cap = cap;
    }
    memcpy(r->buf + r->len, tmp, add);
    r->len += add;
    r->buf[r->len] = 0;
    r->rule[rule]++;
    r->total++;
}

static bool starts(const char *s, const char *prefix)
{
    return strncmp(s, prefix, strlen(prefix)) == 0;
}

/* u8..u64, s8..s64 */
static bool is_sized_int(const char *s)
{
    if (s[0] != 'u' && s[0] != 's')
        return false;
    return !strcmp(s + 1, "8") || !strcmp(s + 1, "16") || !strcmp(s + 1, "32") || !strcmp(s + 1, "64");
}

static bool is_n64_name(const char *s)
{
    if (starts(s, "__os") || starts(s, "gSP") || starts(s, "gDP") || starts(s, "gsSP") ||
        starts(s, "gsDP") || starts(s, "OSMesg"))
        return true;
    if ((starts(s, "os") || starts(s, "al")) && s[2] >= 'A' && s[2] <= 'Z')
        return true;
    if (starts(s, "G_") && s[2] >= 'A' && s[2] <= 'Z')
        return true;
    if (is_sized_int(s) || !strcmp(s, "f32") || !strcmp(s, "f64"))
        return true;
    static const char *const exact[] = {"OSThread", "Gfx", "Mtx", "Vp", "Acmd", "OSContPad"};
    for (size_t i = 0; i < sizeof exact / sizeof exact[0]; i++)
        if (!strcmp(s, exact[i]))
            return true;
    return false;
}

/* 0xA4xxxxxx: RCP register space */
static bool is_rcp_literal(const char *s)
{
    if (s[0] != '0' || (s[1] != 'x' && s[1] != 'X') || (s[2] != 'a' && s[2] != 'A') || s[3] != '4')
        return false;
    int digits = 0;
    const char *p = s + 2;
    while (isxdigit((unsigned char)*p)) {
        digits++;
        p++;
    }
    return digits == 8;
}

static int row_cmp(const void *a, const void *b)
{
    return strcmp(((const LedgerRow *)a)->name, ((const LedgerRow *)b)->name);
}

static const LedgerRow *ledger_find(const Ledger *l, const char *name)
{
    LedgerRow key = {(char *)name, false};
    return bsearch(&key, l->rows, l->count, sizeof key, row_cmp);
}

static void ledger_free(Ledger *l)
{
    for (size_t i = 0; i < l->count; i++)
        free(l->rows[i].name);
    free(l->rows);
    *l = (Ledger){0};
}

static void ledger_parse(Ledger *l, const char *text)
{
    const char *p = text;
    bool first = true;
    while (*p) {
        const char *eol = strchr(p, '\n');
        size_t n = eol ? (size_t)(eol - p) : strlen(p);
        if (n && p[n - 1] == '\r')
            n--;
        const char *tab[8] = {p};
        size_t fields = 1;
        for (size_t i = 0; i < n && fields < 8; i++)
            if (p[i] == '\t')
                tab[fields++] = p + i + 1;
        if (!first && n && fields >= 7) {
            size_t sym = (size_t)(tab[1] - tab[0]) - 1;
            const char *status = tab[6];
            size_t slen = n - (size_t)(status - p);
            bool is_open = slen == 4 && !strncmp(status, "open", 4);
            bool is_removed = slen == 7 && !strncmp(status, "removed", 7);
            if (sym && (is_open || is_removed)) {
                if (l->count == l->cap) {
                    l->cap = l->cap ? l->cap * 2 : 256;
                    l->rows = realloc(l->rows, l->cap * sizeof *l->rows);
                }
                l->rows[l->count].name = malloc(sym + 1);
                memcpy(l->rows[l->count].name, p, sym);
                l->rows[l->count].name[sym] = 0;
                l->rows[l->count].open = is_open;
                l->count++;
                l->open += is_open;
            }
        }
        first = false;
        if (!eol)
            break;
        p = eol + 1;
    }
    qsort(l->rows, l->count, sizeof *l->rows, row_cmp);
}

/* The port's boundary directory (its --ledger directory, with a slash): N64 names are allowed there. */
static const char *exempt_prefix = "platform/port/";

static bool path_exempt(const char *path)
{
    return starts(path, exempt_prefix);
}

static bool path_banned(const char *path)
{
    static const char *const prefixes[] = {"tools/", "src/common/", "src/platform/", "src/renderer/",
                                           "src/sound/", "src/input/", "src/assets/", "src/engine/"};
    for (size_t i = 0; i < sizeof prefixes / sizeof prefixes[0]; i++)
        if (starts(path, prefixes[i]))
            return true;
    return false;
}

static bool is_cond_directive(const char *d)
{
    static const char *const names[] = {"if", "ifdef", "ifndef", "elif", "elifdef", "elifndef"};
    for (size_t i = 0; i < sizeof names / sizeof names[0]; i++)
        if (!strcmp(d, names[i]))
            return true;
    return false;
}

static bool ident_start(int c)
{
    return isalpha(c) || c == '_';
}

static bool ident_char(int c)
{
    return isalnum(c) || c == '_';
}

/* Counts VERSION_ conditionals; versions_only reports just those, otherwise rules 1, 3 and 4. */
static void scan_source(const LintSource *src, const Ledger *ledger, LintReport *rep, long *version_hits,
                        bool versions_only)
{
    const char *s = src->text;
    size_t n = src->len, i = 0;
    long line = 1;
    bool exempt = path_exempt(src->path), banned = path_banned(src->path);
    bool line_start = true;
    char directive[16] = "";
    bool in_cond = false, in_guard = false;
    while (i < n) {
        char c = s[i];
        if (c == '\n') {
            line++;
            line_start = true;
            in_cond = in_guard = false;
            i++;
            continue;
        }
        if (c == '\\' && i + 1 < n && s[i + 1] == '\n') { /* line continuation keeps the directive */
            line++;
            i += 2;
            continue;
        }
        if (c == ' ' || c == '\t' || c == '\r') {
            i++;
            continue;
        }
        if (c == '/' && i + 1 < n && s[i + 1] == '/') {
            while (i < n && s[i] != '\n')
                i++;
            continue;
        }
        if (c == '/' && i + 1 < n && s[i + 1] == '*') {
            i += 2;
            while (i + 1 < n && !(s[i] == '*' && s[i + 1] == '/')) {
                if (s[i] == '\n')
                    line++;
                i++;
            }
            i = i + 2 < n ? i + 2 : n;
            continue;
        }
        if (c == '"' || c == '\'') {
            char q = c;
            i++;
            while (i < n && s[i] != q && s[i] != '\n') {
                if (s[i] == '\\' && i + 1 < n)
                    i++;
                i++;
            }
            if (i < n && s[i] == q)
                i++;
            line_start = false;
            continue;
        }
        if (c == '#' && line_start) {
            size_t j = i + 1;
            while (j < n && (s[j] == ' ' || s[j] == '\t'))
                j++;
            size_t k = 0;
            while (j < n && isalpha((unsigned char)s[j]) && k + 1 < sizeof directive)
                directive[k++] = s[j++];
            directive[k] = 0;
            in_cond = is_cond_directive(directive);
            in_guard = !strcmp(directive, "ifndef") || !strcmp(directive, "define");
            line_start = false;
            i = j;
            continue;
        }
        line_start = false;
        if (isdigit((unsigned char)c) || (c == '.' && i + 1 < n && isdigit((unsigned char)s[i + 1]))) {
            char num[64];
            size_t k = 0;
            while (i < n && (ident_char(s[i]) || s[i] == '.' || s[i] == '\'' ||
                             ((s[i] == '+' || s[i] == '-') && i > 0 && strchr("eEpP", s[i - 1])))) {
                if (k + 1 < sizeof num)
                    num[k++] = s[i];
                i++;
            }
            num[k] = 0;
            if (!exempt && !versions_only && is_rcp_literal(num))
                report_add(rep, banned ? 4 : 1, src->path, line, num);
            continue;
        }
        if (ident_start((unsigned char)c)) {
            char name[128];
            size_t k = 0;
            while (i < n && ident_char((unsigned char)s[i])) {
                if (k + 1 < sizeof name)
                    name[k++] = s[i];
                i++;
            }
            name[k] = 0;
            if (exempt)
                continue;
            if (in_cond && starts(name, "VERSION_")) {
                (*version_hits)++;
                if (versions_only)
                    report_add(rep, 5, src->path, line, name);
                in_cond = false; /* one conditional counts once */
            }
            if (versions_only)
                continue;
            size_t nl = strlen(name);
            if (in_guard && nl > 2 && !strcmp(name + nl - 2, "_H"))
                continue;
            const LedgerRow *row = ledger_find(ledger, name);
            if (row && !row->open)
                report_add(rep, 3, src->path, line, name);
            if (is_n64_name(name)) {
                if (banned)
                    report_add(rep, 4, src->path, line, name);
                else if (!row || !row->open)
                    report_add(rep, 1, src->path, line, name);
            }
            continue;
        }
        i++;
    }
}

/* Runs every rule; returns the violation count. rep->buf holds the "file:line: name" lines. */
static int n64lint_check(const char *ledger_tsv, long open_count, long version_debt, const LintSource *sources,
                         size_t nsources, LintReport *rep)
{
    Ledger ledger = {0};
    ledger_parse(&ledger, ledger_tsv);
    if (ledger.open > open_count) {
        char msg[160];
        snprintf(msg, sizeof msg, "open rows %ld > OPEN_COUNT %ld", ledger.open, open_count);
        report_add(rep, 2, "ENTRY_POINTS.tsv", 1, msg);
    }
    long version_hits = 0;
    for (size_t i = 0; i < nsources; i++)
        scan_source(&sources[i], &ledger, rep, &version_hits, false);
    if (version_hits > version_debt) {
        long ignored = 0;
        for (size_t i = 0; i < nsources; i++)
            scan_source(&sources[i], &ledger, rep, &ignored, true);
        char msg[160];
        snprintf(msg, sizeof msg, "VERSION_ conditionals %ld > VERSION_DEBT %ld", version_hits, version_debt);
        report_add(rep, 5, "src", 1, msg);
    }
    ledger_free(&ledger);
    return rep->total;
}

#ifndef N64LINT_NO_MAIN
static char *read_file(const char *path, size_t *len)
{
    FILE *f = fopen(path, "rb");
    if (!f)
        return NULL;
    size_t cap = 4096, n = 0;
    char *b = malloc(cap + 1);
    size_t got;
    while (b && (got = fread(b + n, 1, cap - n, f)) > 0) {
        n += got;
        if (n == cap) {
            cap *= 2;
            b = realloc(b, cap + 1);
        }
    }
    fclose(f);
    if (!b)
        return NULL;
    b[n] = 0;
    *len = n;
    return b;
}

static void add_path(char ***list, size_t *count, size_t *cap, const char *path)
{
    if (*count == *cap) {
        *cap = *cap ? *cap * 2 : 256;
        *list = realloc(*list, *cap * sizeof **list);
    }
    char *copy = malloc(strlen(path) + 1);
    strcpy(copy, path);
    for (char *p = copy; *p; p++)
        if (*p == '\\')
            *p = '/';
    (*list)[(*count)++] = copy;
}

static char *join_path(const char *a, const char *b, const char *c)
{
    int n = snprintf(NULL, 0, "%s/%s%s%s", a, b, *c ? "/" : "", c);
    char *s = malloc((size_t)n + 1);
    if (s)
        snprintf(s, (size_t)n + 1, "%s/%s%s%s", a, b, *c ? "/" : "", c);
    return s;
}

int main(int argc, char **argv)
{
    const char *root = ".", *ledger_dir = "platform/port", *list = NULL;
    char **paths = NULL;
    size_t npaths = 0, cap = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--root") && i + 1 < argc)
            root = argv[++i];
        else if (!strcmp(argv[i], "--ledger") && i + 1 < argc)
            ledger_dir = argv[++i];
        else if (!strcmp(argv[i], "--files-from") && i + 1 < argc)
            list = argv[++i];
        else
            add_path(&paths, &npaths, &cap, argv[i]);
    }
    char exempt[512];
    snprintf(exempt, sizeof exempt, "%s/", ledger_dir);
    exempt_prefix = exempt;
    if (list) {
        size_t len;
        char *text = read_file(list, &len);
        if (!text) {
            fprintf(stderr, "n64lint: cannot read %s\n", list);
            return 2;
        }
        for (char *line = strtok(text, "\r\n"); line; line = strtok(NULL, "\r\n"))
            add_path(&paths, &npaths, &cap, line);
        free(text);
    }
    size_t len;
    char *tsv = NULL, *oc = NULL, *vd = NULL;
    char *p1 = join_path(root, ledger_dir, "ENTRY_POINTS.tsv");
    char *p2 = join_path(root, ledger_dir, "OPEN_COUNT");
    char *p3 = join_path(root, ledger_dir, "VERSION_DEBT");
    if (p1 && p2 && p3) {
        tsv = read_file(p1, &len);
        oc = read_file(p2, &len);
        vd = read_file(p3, &len);
    }
    if (!tsv || !oc || !vd) {
        fprintf(stderr, "n64lint: ledger files missing under %s/%s\n", root, ledger_dir);
        return 2;
    }
    LintSource *sources = calloc(npaths ? npaths : 1, sizeof *sources);
    size_t nsrc = 0;
    for (size_t i = 0; i < npaths; i++) {
        char *full = join_path(root, paths[i], "");
        size_t flen;
        char *text = full ? read_file(full, &flen) : NULL;
        if (!text) {
            fprintf(stderr, "n64lint: cannot read %s\n", full ? full : paths[i]);
            return 2;
        }
        free(full);
        sources[nsrc++] = (LintSource){paths[i], text, flen};
    }
    LintReport rep = {0};
    int bad = n64lint_check(tsv, atol(oc), atol(vd), sources, nsrc, &rep);
    if (rep.buf)
        fputs(rep.buf, stderr);
    if (bad)
        fprintf(stderr, "n64lint: %d violation(s)\n", bad);
    return bad ? 1 : 0;
}
#endif
