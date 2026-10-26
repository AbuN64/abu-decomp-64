/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "n64c_dltrace.h"
#include <string.h>

enum { TILES = 8, SOURCES = 16, MAX_VIEW = 6 };
enum {
    OP_TRI1 = 0x05, OP_TRI2 = 0x06, OP_QUAD = 0x07, OP_TEXTURE = 0xD7, OP_SETOTHERMODE_L = 0xE2,
    OP_SETOTHERMODE_H = 0xE3, OP_TEXRECT = 0xE4, OP_TEXRECTFLIP = 0xE5,
    OP_RDPSETOTHERMODE = 0xEF, OP_LOADTLUT = 0xF0, OP_SETTILESIZE = 0xF2, OP_LOADBLOCK = 0xF3, OP_LOADTILE = 0xF4,
    OP_SETTILE = 0xF5, OP_SETFILLCOLOR = 0xF7, OP_SETFOGCOLOR = 0xF8, OP_SETBLENDCOLOR = 0xF9,
    OP_SETPRIMCOLOR = 0xFA, OP_SETENVCOLOR = 0xFB, OP_SETCOMBINE = 0xFC, OP_SETTIMG = 0xFD
};
typedef struct {
    uint32_t addr, width;
    int fmt, siz, set;
} Image;
typedef struct {
    int fmt, siz, line, tmem, pal;
    int uls, ult, lrs, lrt, sized;
} Tile;
typedef struct {
    uint32_t tmem, addr, width;
    int fmt, siz;
} Source;

static FILE *out;
static int paused;
static long draws;
static Image timg;
static Tile tile[TILES];
static Source source[SOURCES];
static int sources;
static struct { uint32_t addr; int count, set; } tlut;
static uint32_t prim, env, fill, blend, fog, comb0, comb1, other_h, other_l;
static int have_prim, have_env, have_fill, have_blend, have_fog, have_comb, have_other;
static int cur_tile;
static uint32_t tex_s, tex_t;
static int tex_on;

static const char *const FMT[] = {"RGBA", "YUV", "CI", "IA", "I", "5", "6", "7"};
static const char *const SIZ[] = {"4b", "8b", "16b", "32b"};

void n64c_dltrace_begin(FILE *file)
{
    out = file;
    paused = 0;
    draws = 0;
    memset(&timg, 0, sizeof timg);
    memset(tile, 0, sizeof tile);
    memset(source, 0, sizeof source);
    memset(&tlut, 0, sizeof tlut);
    sources = 0;
    have_prim = have_env = have_fill = have_blend = have_fog = have_comb = have_other = 0;
    cur_tile = tex_on = 0;
    tex_s = tex_t = 0;
    if (out)
        fprintf(out, "# draw trace: one line per triangle command or rectangle, in RSP order\n");
}

int n64c_dltrace_active(void) { return out != NULL && paused == 0; }
void n64c_dltrace_pause(int delta) { paused += delta; }

static void load_source(int t, const char *what)
{
    (void)what;
    if (t < 0 || t >= TILES)
        return;
    Source *s = NULL;
    for (int i = 0; i < sources; i++)
        if (source[i].tmem == (uint32_t)tile[t].tmem)
            s = &source[i];
    if (!s && sources < SOURCES)
        s = &source[sources++];
    if (!s)
        s = &source[0];
    *s = (Source){(uint32_t)tile[t].tmem, timg.addr, timg.width, timg.fmt, timg.siz};
}

static void print_texture(int t)
{
    if (t < 0 || t >= TILES)
        return;
    const Tile *k = &tile[t];
    const Source *s = NULL;
    for (int i = 0; i < sources; i++)
        if (source[i].tmem == (uint32_t)k->tmem)
            s = &source[i];
    fprintf(out, " tile=%d tilefmt=%s/%s tmem=0x%x pal=%d", t, FMT[k->fmt & 7], SIZ[k->siz & 3], k->tmem, k->pal);
    if (k->sized)
        fprintf(out, " texels=%dx%d", (k->lrs - k->uls) / 4 + 1, (k->lrt - k->ult) / 4 + 1);
    if (s)
        fprintf(out, " img=0x%08x imgfmt=%s/%s imgwidth=%u", s->addr, FMT[s->fmt & 7], SIZ[s->siz & 3], s->width);
    else
        fprintf(out, " img=unknown");
    if (tlut.set)
        fprintf(out, " tlut=0x%08x/%d", tlut.addr, tlut.count);
}

static void print_state(void)
{
    if (have_comb)
        fprintf(out, " cc=%06x:%08x rgb0=%u,%u,%u,%u a0=%u,%u,%u,%u rgb1=%u,%u,%u,%u a1=%u,%u,%u,%u", comb0, comb1,
                comb0 >> 20 & 15, comb1 >> 28 & 15, comb0 >> 15 & 31, comb1 >> 15 & 7,
                comb0 >> 12 & 7, comb1 >> 12 & 7, comb0 >> 9 & 7, comb1 >> 9 & 7,
                comb0 >> 5 & 15, comb1 >> 24 & 15, comb0 & 31, comb1 >> 6 & 7,
                comb1 >> 21 & 7, comb1 >> 3 & 7, comb1 >> 18 & 7, comb1 & 7);
    if (have_prim)
        fprintf(out, " prim=%08x", prim);
    if (have_env)
        fprintf(out, " env=%08x", env);
    if (have_fill)
        fprintf(out, " fill=%08x", fill);
    if (have_blend)
        fprintf(out, " blend=%08x", blend);
    if (have_fog)
        fprintf(out, " fog=%08x", fog);
    if (have_other)
        fprintf(out, " other=%06x:%08x", other_h, other_l);
}

static const char *name(uint32_t op)
{
    switch (op) {
    case OP_TRI1: return "tri1";
    case OP_TRI2: return "tri2";
    case OP_QUAD: return "quad";
    case OP_TEXRECT: return "texrect";
    default: return "texrectflip";
    }
}

void n64c_dltrace_cmd(uint32_t w0, uint32_t w1, uint32_t timg_phys, uint32_t half1, uint32_t half2)
{
    if (!n64c_dltrace_active())
        return;
    const uint32_t op = w0 >> 24;
    switch (op) {
    case OP_SETTIMG:
        timg = (Image){timg_phys, (w0 & 0xFFF) + 1, w0 >> 21 & 7, w0 >> 19 & 3, 1};
        break;
    case OP_SETTILE: {
        Tile *k = &tile[w1 >> 24 & 7];
        k->fmt = (int)(w0 >> 21 & 7);
        k->siz = (int)(w0 >> 19 & 3);
        k->line = (int)(w0 >> 9 & 0x1FF);
        k->tmem = (int)(w0 & 0x1FF);
        k->pal = (int)(w1 >> 20 & 15);
        break;
    }
    case OP_SETTILESIZE: {
        Tile *k = &tile[w1 >> 24 & 7];
        k->uls = (int)(w0 >> 12 & 0xFFF);
        k->ult = (int)(w0 & 0xFFF);
        k->lrs = (int)(w1 >> 12 & 0xFFF);
        k->lrt = (int)(w1 & 0xFFF);
        k->sized = 1;
        break;
    }
    case OP_LOADBLOCK:
    case OP_LOADTILE:
        load_source((int)(w1 >> 24 & 7), "image");
        break;
    case OP_LOADTLUT:
        tlut.addr = timg.addr;
        tlut.count = (int)(w1 >> 14 & 0x3FF) + 1;
        tlut.set = 1;
        break;
    case OP_TEXTURE:
        cur_tile = (int)(w0 >> 8 & 7);
        tex_on = (int)(w0 & 0x7F) != 0;
        tex_s = w1 >> 16;
        tex_t = w1 & 0xFFFF;
        break;
    case OP_SETPRIMCOLOR: prim = w1; have_prim = 1; break;
    case OP_SETENVCOLOR: env = w1; have_env = 1; break;
    case OP_SETFILLCOLOR: fill = w1; have_fill = 1; break;
    case OP_SETBLENDCOLOR: blend = w1; have_blend = 1; break;
    case OP_SETFOGCOLOR: fog = w1; have_fog = 1; break;
    case OP_SETCOMBINE: comb0 = w0 & 0xFFFFFF; comb1 = w1; have_comb = 1; break;
    case OP_RDPSETOTHERMODE: other_h = w0 & 0xFFFFFF; other_l = w1; have_other = 1; break;
    case OP_SETOTHERMODE_L:
    case OP_SETOTHERMODE_H: { /* F3DEX2: len - 1 in the low byte, 32 - shift - len above it */
        const uint32_t len = (w0 & 0xFF) + 1, shift = 32 - (w0 >> 8 & 0xFF) - len;
        const uint32_t mask = (uint32_t)(((uint64_t)1 << len) - 1) << shift;
        uint32_t *word = op == OP_SETOTHERMODE_L ? &other_l : &other_h;
        *word = (*word & ~mask) | (w1 & mask);
        other_h &= 0xFFFFFF;
        have_other = 1;
        break;
    }
    case OP_TEXRECT:
    case OP_TEXRECTFLIP: {
        const int t = (int)(w1 >> 24 & 7);
        fprintf(out, "draw %ld %s rect=(%.2f,%.2f)-(%.2f,%.2f) s=%.2f t=%.2f dsdx=%.9g dtdy=%.9g", draws++, name(op),
                (w1 >> 12 & 0xFFF) / 4.0, (w1 & 0xFFF) / 4.0, (w0 >> 12 & 0xFFF) / 4.0, (w0 & 0xFFF) / 4.0,
                (int16_t)(half1 >> 16) / 32.0, (int16_t)(half1 & 0xFFFF) / 32.0, (int16_t)(half2 >> 16) / 1024.0,
                (int16_t)(half2 & 0xFFFF) / 1024.0);
        print_texture(t);
        print_state();
        fputc('\n', out);
        break;
    }
    default:
        break;
    }
}

static void print_vertex(const N64cTraceVtx *v)
{
    if (!v->loaded) {
        fprintf(out, " v=unloaded");
        return;
    }
    fprintf(out, " v=(%d,%d,%d st=%.9g,%.9g", v->ob[0], v->ob[1], v->ob[2], v->st[0] / 32.0, v->st[1] / 32.0);
    if (v->clip_known)
        fprintf(out, " clip=%.9g,%.9g,%.9g,%.9g", v->clip[0], v->clip[1], v->clip[2], v->clip[3]);
    fputc(')', out);
}

void n64c_dltrace_tris(uint32_t w0, int count, const N64cTraceVtx *v, const float *mv)
{
    if (!n64c_dltrace_active())
        return;
    fprintf(out, "draw %ld %s", draws++, name(w0 >> 24));
    if (mv) {
        fputs(" mv=(", out);
        for (int i = 0; i < 16; i++)
            fprintf(out, "%s%.9g", i ? "," : "", mv[i]);
        fputc(')', out);
    }
    if (tex_on) {
        fprintf(out, " texscale=%u,%u", tex_s, tex_t);
        print_texture(cur_tile);
    } else {
        fprintf(out, " untextured");
    }
    print_state();
    for (int i = 0; i < count && i < MAX_VIEW; i++)
        print_vertex(&v[i]);
    fputc('\n', out);
}
