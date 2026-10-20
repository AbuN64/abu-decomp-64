/* SPDX-License-Identifier: GPL-3.0-or-later
 * Golden proof: replays command lists captured from the oracle against the oracle's own sample
 * memory and compares the PCM written by save-buffer commands. Black-box: only the list, the
 * pre-task RAM image and the post-task RAM diff are used. Usage: test_n64a_golden MIN_CORR DIR...
 * DIR holds tNNNN.json (task words), tNNNN.rd0.raw (RAM before), tNNNN.post (regions after). */
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "n64a_abi.h"

enum { RDSZ = 8 << 20 };

static uint8_t *slurp(const char *path, size_t *len)
{
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *b = malloc((size_t)n + 1);
    if (b && fread(b, 1, (size_t)n, f) != (size_t)n) { free(b); b = NULL; }
    fclose(f);
    if (len) *len = (size_t)n;
    return b;
}

static uint32_t rd32(const uint8_t *ram, uint32_t a)
{
    return (uint32_t)ram[(a) ^ 3u] << 24 | (uint32_t)ram[(a + 1) ^ 3u] << 16 | (uint32_t)ram[(a + 2) ^ 3u] << 8 | ram[(a + 3) ^ 3u];
}

typedef struct { uint32_t addr, bytes; } Save;

int main(int argc, char **argv)
{
    if (argc == 2) return 77; /* no golden directory configured: SKIPPED */
    if (argc < 3) { fprintf(stderr, "usage: %s MIN_CORR DIR...\n", argv[0]); return 2; }
    const double min_corr = atof(argv[1]);
    const char *flav_env = getenv("N64A_FLAVOUR");
    N64aFlavour flav = flav_env ? (N64aFlavour)atoi(flav_env) : N64A_FLAVOUR_AUDIO;
    int tasks = 0, failed = 0, silent = 0;
    for (int di = 2; di < argc; di++)
    for (int n = 0; n < 10000; n++) {
        char p[512];
        const char *dir = argv[di];
        snprintf(p, sizeof p, "%s/t%04d.dp", dir, n); /* "data_ptr data_size" text */
        FILE *f = fopen(p, "r");
        if (!f) continue;
        unsigned dp, ds;
        if (fscanf(f, "%u %u", &dp, &ds) != 2) { fclose(f); continue; }
        fclose(f);
        snprintf(p, sizeof p, "%s/t%04d.rd0.raw", dir, n);
        uint8_t *pre = slurp(p, NULL);
        snprintf(p, sizeof p, "%s/t%04d.post", dir, n);
        size_t plen = 0;
        uint8_t *post = slurp(p, &plen);
        if (!pre || !post) { fprintf(stderr, "task %d: missing files\n", n); failed++; free(pre); free(post); continue; }
        uint8_t *oracle = malloc(RDSZ);
        memcpy(oracle, pre, RDSZ);
        for (size_t o = 0; o + 8 <= plen;) {
            uint32_t a, l;
            memcpy(&a, post + o, 4); memcpy(&l, post + o + 4, 4);
            memcpy(oracle + a, post + o + 8, l);
            o += 8 + l;
        }
        if (getenv("N64A_LIMIT")) ds = (unsigned)atoi(getenv("N64A_LIMIT"));
        if (getenv("N64A_ORACLE")) { size_t ol; uint8_t *o2 = slurp(getenv("N64A_ORACLE"), &ol); if (o2 && ol == RDSZ) { free(oracle); oracle = o2; } }
        if (getenv("N64A_ONLY") && atoi(getenv("N64A_ONLY")) != n) { free(pre); free(post); free(oracle); continue; }
        dp &= 0xffffff;
        Save saves[64]; int ns = 0;
        /* resolve each save's DRAM address + byte count by replaying the buffer-count state (opcode 8) */
        ns = 0; int after_il = 0; uint32_t cnt = 0; uint32_t seg[16] = {0};
        for (uint32_t c = 0; c < ds; c += 8) {
            uint32_t w1 = rd32(pre, dp + c), w2 = rd32(pre, dp + c + 4);
            unsigned op = w1 >> 24;
            if (op == 7) seg[(w2 >> 24) & 15] = w2 & 0xffffff;
            if (op == 8 && !((w1 >> 16) & 8)) cnt = w2 & 0xffff;
            if (op == 13) after_il = 3;
            else if (after_il > 0) after_il--;
            if (op == 6 && ns < 64 && (after_il > 0 || getenv("N64A_ALLSAVES")))
                saves[ns++] = (Save){seg[(w2 >> 24) & 15] + (w2 & 0xffffff), cnt};
        }
        N64aAbi *abi = n64a_abi_create(flav);
        n64a_abi_run(abi, pre, dp, ds); /* pre becomes the mixer's result */
        double sxy = 0, sxx = 0, syy = 0; size_t total = 0; unsigned long long energy = 0, exact = 0; int maxdiff = 0;
        for (int s = 0; s < ns; s++) {
            double axy = 0, axx = 0, ayy = 0; unsigned aex = 0;
            for (uint32_t b = 0; b + 2 <= saves[s].bytes; b += 2) {
                double x = n64a_ram_s16(pre, saves[s].addr + b), y = n64a_ram_s16(oracle, saves[s].addr + b);
                sxy += x * y; sxx += x * x; syy += y * y; total++; energy += (unsigned long long)(y * y > 0);
                exact += (x == y);
                if (fabs(x - y) > maxdiff) maxdiff = (int)fabs(x - y);
                axy += x * y; axx += x * x; ayy += y * y; aex += (x == y);
            }
            if (getenv("N64A_VERBOSE"))
                printf("  save %d @%06x %u bytes: corr %.4f exact %u/%u rms_o %.1f rms_m %.1f\n", s, saves[s].addr, saves[s].bytes,
                       (axx > 0 && ayy > 0) ? axy / sqrt(axx * ayy) : 0.0, aex, saves[s].bytes / 2,
                       sqrt(ayy / (saves[s].bytes / 2.0 + 1e-9)), sqrt(axx / (saves[s].bytes / 2.0 + 1e-9)));
        }
        double corr = (sxx > 0 && syy > 0) ? sxy / sqrt(sxx * syy) : (sxx == 0 && syy == 0 ? 1.0 : 0.0);
        int quiet = syy < 1.0;
        printf("task %d: %zu samples, corr %.6f, exact %.2f%%, maxdiff %d, oracle_rms %.1f, warnings %u%s\n", n, total, corr,
               total ? 100.0 * (double)exact / (double)total : 0.0, maxdiff, total ? sqrt(syy / (double)total) : 0.0,
               n64a_abi_warnings(abi), quiet ? " (silent)" : "");
        if (getenv("N64A_DMEM")) {
            snprintf(p, sizeof p, "%s/t%04d.dm1", dir, n);
            uint8_t *dm = slurp(p, NULL);
            if (dm) {
                const uint8_t *mine = n64a_abi_scratch(abi);
                printf("  dmem match (0x40-byte chunks, 1=equal):\n  ");
                for (int c = 0; c < 0x1000; c += 0x40) printf("%c", memcmp(dm + c, mine + c, 0x40) ? '.' : '1');
                printf("\n");
                for (int sh = -0x800; sh <= 0x800; sh += 2) {
                    int hit = 0;
                    for (int c = 0x100; c < 0xf00; c += 2) {
                        int16_t a, b;
                        if (c + sh < 0 || c + sh >= 0x1000) continue;
                        memcpy(&a, mine + (c ^ 2), 2); memcpy(&b, dm + ((c + sh) ^ 2), 2);
                        hit += (a == b && a != 0);
                    }
                    if (hit > 40) printf("  shift %+d: %d equal nonzero samples\n", sh, hit);
                }
                free(dm);
            }
        }
        tasks++; silent += quiet;
        if (!quiet && corr < min_corr) failed++;
        (void)energy; (void)sxx;
        n64a_abi_destroy(abi);
        free(pre); free(post); free(oracle);
    }
    printf("golden: %d tasks, %d silent, %d failed (min corr %.2f)\n", tasks, silent, failed, min_corr);
    if (tasks == 0) return 77;
    if (tasks == silent) { fprintf(stderr, "golden: all captured output silent\n"); return 1; }
    return failed != 0;
}
