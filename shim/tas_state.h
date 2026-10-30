/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Canonical game state per symbol, read the same way on both sides of a TAS comparison.
 *
 * The port (a native little-endian program) and the original (a recompiled big-endian game whose RDRAM keeps every
 * 32-bit word in host order and reaches bytes at address ^ 3) hold the same logical values at different byte
 * positions. A state spec lists, per region, the symbols (game variables) and the typed elements to read:
 *     # comment
 *     ram <hex bytes>            RDRAM size (the address mask is 0x3ffffff)
 *     dynamic <lo> <hi>          pointers into [lo, hi) are heap/stack addresses and compare by value; any other
 *                                pointer (a static object, code, a host address) compares as 1, and null as 0
 *     region <name>
 *     symbol <name>              the runs that follow belong to this variable
 *     run <hex address> <b|h|w|d|p> <count>      count consecutive elements: u8, u16, u32, u64/double, pointer
 * Both sides read each element as its logical value (u64: high word first) and fold it into a 64-bit hash per symbol
 * (FNV-1a over 32-bit values). Equal hashes mean equal logical state. A trace holds one line per poll,
 * "<poll> <symbol>:<hash> ...", naming only the symbols whose 32-bit hash changed since the previous poll (all of them
 * at the first poll), so a movie's whole history of ~50 changing symbols per poll stays a few megabytes.
 * CHECKPOINT: both sides sample at TS_CHECKPOINT, one logical point of a poll, defined here once: inside the guest's
 * controller poll N, after the poll counter reads N and the pad is filled, before the poll returns to the game. The
 * frame N-1 display list was submitted before the game came back to read input, and frame N's input is not yet
 * consumed, so a value written and restored inside one frame is never seen half-done. Events (EV|FUNC@N|..) carry the
 * counter of the poll they ran after, so the writers of the change seen at poll N are those tagged N-1.
 * The same text lives in abu-decomp-64 shim/tas_state.h (TS_ORACLE unset: native words) and in cake-taste-64
 * hosts/n64recomp/tas_state.h (TS_ORACLE set: word-swizzled RDRAM); change both together. */
#ifndef TAS_STATE_H
#define TAS_STATE_H
#define TS_CHECKPOINT "poll-entry"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef __cplusplus
extern "C" {
#endif

#define TS_MASK 0x3ffffffu
/* The hot loops read hundreds of thousands of elements per poll: keep -finstrument-functions (a call per helper) and
 * --coverage (a counter per iteration) of an instrumented build out of them. */
#if defined(__GNUC__) && !defined(__clang__) && __GNUC__ >= 7
#define TS_HOT __attribute__((no_instrument_function, no_profile_instrument_function))
#elif defined(__GNUC__)
#define TS_HOT __attribute__((no_instrument_function))
#else
#define TS_HOT
#endif

struct ts_run { uint32_t address, count; char kind; unsigned symbol; };
struct ts_spec {
    struct ts_run *runs;
    size_t nruns;
    char **regions;
    size_t nregions, nsymbols;
    uint32_t ram_bytes, dynamic_lo, dynamic_hi;
};

static inline int ts_load(const char *path, struct ts_spec *spec)
{
    FILE *f = fopen(path, "r");
    char line[256];
    size_t cap = 0;
    memset(spec, 0, sizeof *spec);
    spec->ram_bytes = 0x800000;
    if (!f) return -1;
    while (fgets(line, sizeof line, f)) {
        unsigned a, b, n;
        char kind, name[128];
        if (line[0] == '#' || line[0] == '\n') continue;
        if (sscanf(line, "ram %x", &a) == 1) spec->ram_bytes = a;
        else if (sscanf(line, "dynamic %x %x", &a, &b) == 2) spec->dynamic_lo = a, spec->dynamic_hi = b;
        else if (sscanf(line, "region %127s", name) == 1) {
            spec->regions = (char **)realloc(spec->regions, (spec->nregions + 1) * sizeof *spec->regions);
            spec->regions[spec->nregions++] = strdup(name);
        } else if (sscanf(line, "symbol %127s", name) == 1 && spec->nregions) {
            spec->nsymbols++;
        } else if (sscanf(line, "run %x %c %u", &a, &kind, &n) == 3 && spec->nsymbols && strchr("bhwdp", kind)) {
            if (spec->nruns == cap) {
                cap = cap ? cap * 2 : 4096;
                spec->runs = (struct ts_run *)realloc(spec->runs, cap * sizeof *spec->runs);
            }
            struct ts_run *r = &spec->runs[spec->nruns++];
            r->address = a & TS_MASK, r->count = n, r->kind = kind, r->symbol = (unsigned)(spec->nsymbols - 1);
        } else {
            fclose(f);
            return -1;
        }
    }
    fclose(f);
    return 0;
}

#ifdef TS_ORACLE
#define TS_B(ram, o) ((uint32_t)(ram)[(o) ^ 3u])
#define TS_H(ram, o) ts_ld16((ram) + ((o) ^ 2u))
#define TS_W(ram, o) ts_ld32((ram) + (o))
#define TS_HI(ram, o) ts_ld32((ram) + (o))
#define TS_LO(ram, o) ts_ld32((ram) + (o) + 4)
#else
#define TS_B(ram, o) ((uint32_t)(ram)[(o)])
#define TS_H(ram, o) ts_ld16((ram) + (o))
#define TS_W(ram, o) ts_ld32((ram) + (o))
#define TS_HI(ram, o) ts_ld32((ram) + (o) + 4)
#define TS_LO(ram, o) ts_ld32((ram) + (o))
#endif
static inline TS_HOT uint32_t ts_ld16(const uint8_t *p) { uint16_t v; memcpy(&v, p, 2); return v; }
static inline TS_HOT uint32_t ts_ld32(const uint8_t *p) { uint32_t v; memcpy(&v, p, 4); return v; }
static inline TS_HOT uint32_t ts_pointer(const struct ts_spec *s, uint32_t v)
{
    return v == 0 ? 0 : (v >= s->dynamic_lo && v < s->dynamic_hi) ? v : 1;
}
static inline TS_HOT uint64_t ts_mix(uint64_t h, uint32_t v) { return (h ^ v) * 0x100000001B3ull; }

/* The element at physical offset o of a run, as canonical 32-bit values: returns how many (1 or 2) were stored. */
static inline TS_HOT int ts_element(const struct ts_spec *s, const uint8_t *ram, char kind, uint32_t o, uint32_t out[2])
{
    switch (kind) {
    case 'b': out[0] = TS_B(ram, o); return 1;
    case 'h': out[0] = TS_H(ram, o); return 1;
    case 'w': out[0] = TS_W(ram, o); return 1;
    case 'p': out[0] = ts_pointer(s, TS_W(ram, o)); return 1;
    default: out[0] = TS_HI(ram, o); out[1] = TS_LO(ram, o); return 2;
    }
}
static inline uint32_t ts_size(char kind) { return kind == 'b' ? 1 : kind == 'h' ? 2 : kind == 'd' ? 8 : 4; }

/* One 64-bit hash per symbol, in spec order, into out[nsymbols]. */
static inline TS_HOT void ts_hash(const struct ts_spec *s, const uint8_t *ram, uint64_t *out)
{
    size_t i;
    for (i = 0; i < s->nsymbols; i++) out[i] = 0xcbf29ce484222325ull;
    for (i = 0; i < s->nruns; i++) {
        const struct ts_run *run = &s->runs[i];
        uint64_t h = out[run->symbol];
        uint32_t o = run->address, step = ts_size(run->kind), v[2];
        if (o + (uint64_t)step * run->count > s->ram_bytes) continue;
        if (run->kind == 'w') {
            for (uint32_t k = 0; k < run->count; k++, o += 4) h = ts_mix(h, TS_W(ram, o));
        } else {
            for (uint32_t k = 0; k < run->count; k++, o += step) {
                int n = ts_element(s, ram, run->kind, o, v);
                h = ts_mix(h, v[0]);
                if (n == 2) h = ts_mix(h, v[1]);
            }
        }
        out[run->symbol] = h;
    }
}

/* The trace line of one poll: "<poll>" then " <index>:<8 hex digits>" for each symbol whose folded hash differs from
 * prev[] (all of them while *started is 0); prev[] is updated. */
static inline void ts_trace_line(FILE *f, long poll, const struct ts_spec *s, const uint64_t *now, uint32_t *prev,
                                 int *started)
{
    fprintf(f, "%ld", poll);
    for (size_t i = 0; i < s->nsymbols; i++) {
        uint32_t folded = (uint32_t)(now[i] ^ (now[i] >> 32));
        if (*started && folded == prev[i]) continue;
        prev[i] = folded;
        fprintf(f, " %zu:%08x", i, folded);
    }
    *started = 1;
    fputc('\n', f);
    fflush(f);
}

/* Canonical big-endian bytes of every element, region after region, to f; returns 0 on success. */
static inline TS_HOT int ts_dump(const struct ts_spec *s, const uint8_t *ram, FILE *f)
{
    for (size_t i = 0; i < s->nruns; i++) {
        const struct ts_run *run = &s->runs[i];
        uint32_t o = run->address, step = ts_size(run->kind), v[2];
        if (o + (uint64_t)step * run->count > s->ram_bytes) return -1;
        for (uint32_t k = 0; k < run->count; k++, o += step) {
            int n = ts_element(s, ram, run->kind, o, v);
            uint8_t b[8];
            unsigned len = 0;
            if (run->kind == 'b') b[len++] = (uint8_t)v[0];
            else if (run->kind == 'h') b[len++] = (uint8_t)(v[0] >> 8), b[len++] = (uint8_t)v[0];
            else
                for (int j = 0; j < n; j++)
                    for (int sh = 24; sh >= 0; sh -= 8) b[len++] = (uint8_t)(v[j] >> sh);
            if (fwrite(b, 1, len, f) != len) return -1;
        }
    }
    return 0;
}

/* Poll numbers "3,17,400": ts_wanted() asks membership. */
struct ts_polls { long *at; size_t n; };
static inline void ts_polls_parse(const char *text, struct ts_polls *p)
{
    p->at = NULL, p->n = 0;
    while (text && *text) {
        char *end;
        long v = strtol(text, &end, 10);
        if (end == text) break;
        p->at = (long *)realloc(p->at, (p->n + 1) * sizeof *p->at);
        p->at[p->n++] = v;
        text = *end == ',' ? end + 1 : end;
    }
}
static inline int ts_wanted(const struct ts_polls *p, long poll)
{
    for (size_t i = 0; i < p->n; i++) if (p->at[i] == poll) return 1;
    return 0;
}
#ifdef __cplusplus
}
#endif
#endif
