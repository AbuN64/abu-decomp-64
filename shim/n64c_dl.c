/* SPDX-License-Identifier: GPL-3.0-or-later */
/* n64c_dl: RSP-addressed display lists -> host-pointer copies (see n64c_dl.h).
 * Command encodings are the public F3DEX2 GBI ones (n64brew wiki "F3DEX2"): opcode in bits 24-31 of
 * word 0; G_VTX count in bits 12-19; G_MOVEMEM length ((bits 19-23) + 1) * 8 and index in bits 0-7;
 * G_MOVEWORD index in bits 16-23 and offset in bits 0-15; G_DL no-push flag in bit 16; G_BRANCH_Z
 * takes its target from the preceding G_RDPHALF_1; G_SETTIMG size in bits 19-20, width - 1 in
 * bits 0-11; load commands carry 10.2 tile coordinates; G_MTX parameters are stored with G_MTX_PUSH
 * inverted; G_LINE3D vertex indices (times two) in bits 16-23 and 8-15 and the width in bits 0-7. */
#include "n64c_dl.h"
#include <math.h>
#include <stdlib.h>
#include <string.h>

enum {
    OP_NOOP = 0x00, OP_VTX = 0x01, OP_MODIFYVTX = 0x02, OP_BRANCH_Z = 0x04, OP_TRI1 = 0x05, OP_LINE3D = 0x08, OP_FIRST_SP = 0xD3,
    OP_DMA_IO = 0xD6, OP_POPMTX = 0xD8, OP_GEOMETRYMODE = 0xD9, OP_MTX = 0xDA, OP_MOVEWORD = 0xDB,
    OP_MOVEMEM = 0xDC, OP_LOAD_UCODE = 0xDD, OP_DL = 0xDE, OP_ENDDL = 0xDF, OP_RDPHALF_1 = 0xE1,
    OP_LOADTLUT = 0xF0, OP_LOADBLOCK = 0xF3, OP_LOADTILE = 0xF4, OP_SETTIMG = 0xFD, OP_SETZIMG = 0xFE,
    OP_SETCIMG = 0xFF,
};
enum { MW_SEGMENT = 0x06 };
enum { MV_MMTX = 2, MV_PMTX = 6, MV_VIEWPORT = 8, MV_MATRIX = 14 };
enum { MAX_DEPTH = 64, SIZ_16B = 2 };
enum { MTX_PUSH = 1, MTX_LOAD = 2, MTX_PROJECTION = 4 };
enum { CULL_FRONT = 0x200, CULL_BACK = 0x400, CULL_BOTH = CULL_FRONT | CULL_BACK, LIGHTING = 0x20000 };
enum { MWO_RGBA = 0x10, MWO_ST = 0x14, MWO_XYSCREEN = 0x18, MWO_ZSCREEN = 0x1C };
/* Modelview stack depth and vertex slots of the host renderer (Fast3D: 11 and 64); lines use the
 * last four slots, past the 32 vertices of the RSP microcode. */
enum { MV_STACK = 11, VERTICES = 64, LINE_SLOT = VERTICES - 4, P_CHAIN = 16 };

typedef float M4[4][4];
/* One RSP vertex slot: the record G_VTX loaded and what G_MODIFYVTX changed since. */
typedef struct {
    unsigned char rec[16];   /* host-layout Vtx: ob[3], flag, tc[2], cn[4] (zero when never loaded) */
    float clip[4];
    float screen[2];         /* G_MWO_POINT_XYSCREEN, pixels */
    float zndc;              /* G_MWO_POINT_ZSCREEN, as NDC depth */
    uint32_t rgba, st;       /* G_MWO_POINT_RGBA (0xRRGGBBAA), G_MWO_POINT_ST */
    uint8_t loaded, clip_known, screen_set, z_set, rgba_set, st_set;
} Slot;
/* What the RSP holds when a G_LINE3D or G_MODIFYVTX runs (see n64c_dl.h). */
typedef struct {
    M4 mv[MV_STACK], p;
    uint8_t mv_known[MV_STACK];
    int mv_size, p_known, p_count;     /* p_count: projection commands since the load, -1 when too many */
    uintptr_t p_cmds[P_CHAIN][2];
    float vp_scale[3], vp_trans[3];    /* x, y in pixels; z in screen depth units */
    int vp_known;
    Slot vtx[VERTICES];
    uint32_t mode, mode_known;         /* geometry mode bits and which of them are known */
    uint32_t zimg_phys;
    int zimg_known;
} Geo;

struct N64cDlBlock {
    N64cDlBlock *next;
    size_t size, used;
    _Alignas(8) unsigned char data[];
};

typedef struct {
    const N64cGfxMem *mem;
    N64cDlArena *arena;
    N64cDlStats *stats;
    uint32_t seg[16];
    /* Texture image of the latest G_SETTIMG (replay copies grow to the furthest load). */
    uintptr_t *timg_cmd;
    uint32_t timg_phys, timg_width, timg_siz, timg_bytes;
    int failed;
    const N64cDlHooks *hooks;
    Geo geo;
} Ctx;

static void *arena_alloc(N64cDlArena *a, size_t bytes)
{
    bytes = (bytes + 7) & ~(size_t)7;
    for (;;) {
        N64cDlBlock *b = a->cur;
        if (b && b->size - b->used >= bytes) {
            void *p = b->data + b->used;
            b->used += bytes;
            a->used += bytes;
            return p;
        }
        if (b && b->next) { /* reuse a block kept by n64c_dl_arena_reset */
            a->cur = b->next;
            a->cur->used = 0;
            continue;
        }
        size_t size = bytes > (1u << 20) ? bytes : (1u << 20);
        N64cDlBlock *n = malloc(sizeof *n + size);
        if (!n)
            return NULL;
        n->next = NULL;
        n->size = size;
        n->used = 0;
        if (b)
            b->next = n;
        else
            a->blocks = n;
        a->cur = n;
    }
}

void n64c_dl_arena_reset(N64cDlArena *a)
{
    a->cur = a->blocks;
    if (a->cur)
        a->cur->used = 0;
    a->used = 0;
}

void n64c_dl_arena_free(N64cDlArena *a)
{
    for (N64cDlBlock *b = a->blocks, *n; b; b = n) {
        n = b->next;
        free(b);
    }
    *a = (N64cDlArena){0};
}

/* RSP address -> physical address: segment of bits 24-27 plus the 24-bit offset (KSEG bits drop).
 * A replay image mirrors addresses past its end, as the capturing host and n64c_gfx do. */
static uint32_t physical(const Ctx *c, uint32_t a)
{
    uint32_t phys = (c->seg[(a >> 24) & 0x0F] + (a & 0x00FFFFFF)) & 0x1FFFFFFF;
    return c->mem->replay ? phys & c->mem->mask : phys;
}

static int in_memory(const Ctx *c, uint32_t phys, uint32_t bytes)
{
    if (!c->mem->base && !c->mem->replay)
        return 1; /* native pointers: RDRAM is mapped at KSEG0 */
    return phys <= c->mem->mask && bytes <= c->mem->mask + 1 - phys;
}

/* Where the record at phys can be read in host layout without conversion. */
static const unsigned char *host(const Ctx *c, uint32_t phys)
{
    if (c->mem->base)
        return c->mem->base + phys;
    return (const unsigned char *)(uintptr_t)(0x80000000u + phys);
}

static uint32_t word_at(const Ctx *c, uint32_t phys)
{
    uint32_t w;
    memcpy(&w, host(c, phys), 4);
    return w;
}

static uint32_t rot16(uint32_t w) { return w << 16 | w >> 16; }
static uint32_t swap32(uint32_t w) { return __builtin_bswap32(w); }
static uint32_t keep(uint32_t w) { return w; }

/* Host-layout copy of a replay record: each 32-bit word converted by convert[i % period]. */
static const void *convert_copy(Ctx *c, uint32_t phys, uint32_t bytes, uint32_t (*const *convert)(uint32_t),
                                uint32_t period)
{
    uint32_t start = phys & ~3u, words = (phys - start + bytes + 3) / 4;
    uint32_t *out = arena_alloc(c->arena, (size_t)words * 4);
    if (!out) {
        c->failed = 1;
        return NULL;
    }
    for (uint32_t i = 0; i < words; i++)
        out[i] = convert[i % period](word_at(c, start + i * 4));
    return (const unsigned char *)out + (phys - start);
}

static uint32_t (*const vertex_words[4])(uint32_t) = {rot16, rot16, rot16, swap32};
static uint32_t (*const halfword_words[1])(uint32_t) = {rot16};
static uint32_t (*const byte_words[1])(uint32_t) = {swap32};
static uint32_t (*const plain_words[1])(uint32_t) = {keep};

/* Host pointer for an operand of bytes length (0 when it lies outside memory); replay records are
 * converted by their word pattern. */
static uintptr_t operand(Ctx *c, uint32_t a, uint32_t bytes, uint32_t (*const *convert)(uint32_t), uint32_t period)
{
    uint32_t phys = physical(c, a);
    if (!in_memory(c, phys, bytes ? bytes : 1)) {
        c->stats->bad++;
        return 0;
    }
    if (c->mem->replay && convert && convert != plain_words)
        return (uintptr_t)convert_copy(c, phys, bytes, convert, period);
    return (uintptr_t)host(c, phys);
}

/* Replay texture images: make the copy of the latest G_SETTIMG reach texel end (exclusive). */
static void texture_reach(Ctx *c, uint32_t texels, uint32_t siz)
{
    if (!c->mem->replay || !c->timg_cmd)
        return;
    uint32_t bytes = siz == 0 ? (texels + 1) / 2 : texels << (siz - 1);
    if (bytes <= c->timg_bytes)
        return;
    if (!in_memory(c, c->timg_phys, bytes)) {
        c->stats->bad++;
        return;
    }
    const void *copy = convert_copy(c, c->timg_phys, bytes, byte_words, 1);
    if (!copy)
        return;
    c->timg_cmd[1] = (uintptr_t)copy;
    c->timg_bytes = bytes;
}

static const uintptr_t *translate(Ctx *c, uint32_t dl, int depth);

/* Fixed-point Mtx (16.16, integer halves then fraction halves, row-major pairs) as the renderer reads it. */
static void matrix_read(M4 m, const int32_t *a)
{
    for (int i = 0; i < 4; i++)
        for (int j = 0; j < 4; j += 2) {
            uint32_t hi = (uint32_t)a[i * 2 + j / 2], lo = (uint32_t)a[8 + i * 2 + j / 2];
            m[i][j] = (float)(int32_t)((hi & 0xFFFF0000u) | lo >> 16) / 65536.0f;
            m[i][j + 1] = (float)(int32_t)(hi << 16 | (lo & 0xFFFFu)) / 65536.0f;
        }
}

static void matrix_mul(M4 r, const M4 a, const M4 b)
{
    M4 t;
    for (int i = 0; i < 4; i++)
        for (int j = 0; j < 4; j++)
            t[i][j] = a[i][0] * b[0][j] + a[i][1] * b[1][j] + a[i][2] * b[2][j] + a[i][3] * b[3][j];
    memcpy(r, t, sizeof t);
}

static void geo_matrix(Geo *g, const uintptr_t *cmd)
{
    const uint32_t params = ((uint32_t)cmd[0] & 0xFF) ^ MTX_PUSH;
    M4 m;
    matrix_read(m, (const int32_t *)cmd[1]);
    if (params & MTX_PROJECTION) {
        if (params & MTX_LOAD) {
            memcpy(g->p, m, sizeof m);
            g->p_known = 1;
            g->p_count = 0;
        } else
            matrix_mul(g->p, m, g->p);
        if (g->p_count >= 0 && g->p_count < P_CHAIN) {
            g->p_cmds[g->p_count][0] = cmd[0];
            g->p_cmds[g->p_count][1] = cmd[1];
            g->p_count++;
        } else
            g->p_count = -1;
        return;
    }
    if ((params & MTX_PUSH) && g->mv_size < MV_STACK) {
        g->mv_size++;
        memcpy(g->mv[g->mv_size - 1], g->mv[g->mv_size - 2], sizeof m);
        g->mv_known[g->mv_size - 1] = g->mv_known[g->mv_size - 2];
    }
    if (g->mv_size == 0)
        g->mv_size = 1;
    if (params & MTX_LOAD) {
        memcpy(g->mv[g->mv_size - 1], m, sizeof m);
        g->mv_known[g->mv_size - 1] = 1;
    } else
        matrix_mul(g->mv[g->mv_size - 1], m, g->mv[g->mv_size - 1]);
}

/* Clip-space positions of the vertices a G_VTX loads (rows: x' = x * m[0] + y * m[1] + z * m[2] + m[3]). */
static void geo_vertices(Geo *g, uint32_t w0, const unsigned char *rec)
{
    const uint32_t n = w0 >> 12 & 0xFF, end = w0 >> 1 & 0x7F;
    const int known = g->p_known && g->mv_size > 0 && g->mv_known[g->mv_size - 1];
    M4 mp;
    if (known)
        matrix_mul(mp, g->mv[g->mv_size - 1], g->p);
    for (uint32_t i = 0, slot = end - n; i < n && slot < VERTICES; i++, slot++) {
        Slot *v = &g->vtx[slot];
        *v = (Slot){0};
        if (!rec)
            continue;
        memcpy(v->rec, rec + i * 16, 16);
        v->loaded = 1;
        v->clip_known = (uint8_t)known;
        int16_t ob[3];
        memcpy(ob, v->rec, sizeof ob);
        for (int k = 0; known && k < 4; k++)
            v->clip[k] = ob[0] * mp[0][k] + ob[1] * mp[1][k] + ob[2] * mp[2][k] + mp[3][k];
    }
}

/* Clip-space position of a slot (a screen position set by G_MODIFYVTX has w = 1); 0 when unknown. */
static int slot_clip(const Geo *g, const Slot *v, float out[4])
{
    if (v->z_set && !v->screen_set) { /* depth set, position from the load */
        if (!v->clip_known || v->clip[3] == 0)
            return 0;
        out[0] = v->clip[0] / v->clip[3];
        out[1] = v->clip[1] / v->clip[3];
        out[2] = v->zndc;
        out[3] = 1;
        return 1;
    }
    if (v->screen_set) {
        if (g->vp_scale[0] == 0 || g->vp_scale[1] == 0)
            return 0;
        const float z = v->z_set ? v->zndc : v->clip_known && v->clip[3] != 0 ? v->clip[2] / v->clip[3] : 0;
        out[0] = (v->screen[0] - g->vp_trans[0]) / g->vp_scale[0];
        out[1] = (g->vp_trans[1] - v->screen[1]) / g->vp_scale[1];
        out[2] = z;
        out[3] = 1;
        return 1;
    }
    memcpy(out, v->clip, sizeof v->clip);
    return v->clip_known;
}

/* Cut the segment a-b to the side of a clip plane where w + s * z >= 0 (s = 1 near, -1 far). */
static int clip_to_plane(float a[4], float b[4], float s)
{
    const float da = a[3] + s * a[2], db = b[3] + s * b[2];
    if (da < 0 && db < 0)
        return 0;
    float *out = da < 0 ? a : db < 0 ? b : NULL;
    if (out) {
        const float t = da / (da - db);
        for (int k = 0; k < 4; k++)
            out[k] = a[k] + t * (b[k] - a[k]);
    }
    return 1;
}

static int16_t ndc_unit(float v)
{
    const float u = roundf(v * 16384.0f);
    return (int16_t)(u > 32767.0f ? 32767.0f : u < -32767.0f ? -32767.0f : u);
}

/* Vertex record for a slot placed at NDC position (x, y, z), with the colour G_MODIFYVTX set. */
static void ndc_record(unsigned char out[16], const Slot *v, float x, float y, float z)
{
    const int16_t ob[3] = {ndc_unit(x), ndc_unit(y), ndc_unit(z)};
    memcpy(out, v->rec, 16);
    memcpy(out, ob, sizeof ob);
    if (v->rgba_set)
        for (int k = 0; k < 4; k++)
            out[12 + k] = (unsigned char)(v->rgba >> (24 - 8 * k));
}

/* Modelview and projection that turn vertex positions in 1/16384 units into normalized device
 * coordinates: diag(2^-7, 2^-7, 2^-7, 1) each, exact in 16.16. */
static const int32_t ndc_scale[16] = {0, 0, 0, 1, 0, 0, 0, 0, 0x02000000, 0, 0x00000200, 0, 0, 0x02000000, 0, 0};

/* Generated lists that load vertices placed in NDC: prologue, the caller's commands, epilogue. */
typedef struct {
    uintptr_t *out;
    size_t n, cap;
    uint32_t lift; /* geometry mode bits cleared for the vertices and set again after */
} Emit;

static void emit(Emit *e, uintptr_t w0, uintptr_t w1)
{
    if (e->n < e->cap) {
        e->out[e->n * 2] = w0;
        e->out[e->n * 2 + 1] = w1;
        e->n++;
    }
}

/* NULL when the state the epilogue restores is unknown. */
static int ndc_begin(Ctx *c, Emit *e, uint32_t extra, uint32_t lift)
{
    const Geo *g = &c->geo;
    if (!g->vp_known || g->p_count <= 0 || g->mv_size >= MV_STACK || g->vp_scale[0] == 0 || g->vp_scale[1] == 0)
        return 0;
    e->cap = 6 + P_CHAIN + extra;
    e->n = 0;
    e->lift = lift & g->mode & g->mode_known;
    if (!(e->out = arena_alloc(c->arena, e->cap * 2 * sizeof *e->out))) {
        c->failed = 1;
        return 0;
    }
    emit(e, (uint32_t)OP_MTX << 24 | 0x38u << 16 | ((MTX_LOAD | MTX_PUSH) ^ MTX_PUSH), (uintptr_t)ndc_scale);
    emit(e, (uint32_t)OP_MTX << 24 | 0x38u << 16 | ((MTX_PROJECTION | MTX_LOAD) ^ MTX_PUSH), (uintptr_t)ndc_scale);
    if (e->lift)
        emit(e, (uint32_t)OP_GEOMETRYMODE << 24 | (~e->lift & 0xFFFFFF), 0);
    return 1;
}

static const uintptr_t *ndc_end(Ctx *c, Emit *e)
{
    const Geo *g = &c->geo;
    if (e->lift)
        emit(e, (uint32_t)OP_GEOMETRYMODE << 24 | 0xFFFFFF, e->lift);
    emit(e, (uint32_t)OP_POPMTX << 24 | 0x38u << 16 | 2, 64);
    for (int k = 0; k < g->p_count; k++)
        emit(e, g->p_cmds[k][0], g->p_cmds[k][1]);
    emit(e, (uint32_t)OP_ENDDL << 24, 0);
    return e->out;
}

/* G_MODIFYVTX (ST stays with the renderer): reload the slot at its new position and colour. */
static const uintptr_t *modify_list(Ctx *c, uint32_t w0, uint32_t w1)
{
    Geo *g = &c->geo;
    const uint32_t where = w0 >> 16 & 0xFF, slot = (w0 & 0xFFFF) / 2;
    if (slot >= LINE_SLOT)
        return NULL;
    Slot *v = &g->vtx[slot];
    if (where == MWO_RGBA) {
        v->rgba = w1;
        v->rgba_set = 1;
    } else if (where == MWO_ZSCREEN) { /* 16.16 screen depth in viewport z units */
        if (g->vp_scale[2] == 0)
            return NULL;
        v->zndc = ((float)(int32_t)w1 / 65536.0f - g->vp_trans[2]) / g->vp_scale[2];
        v->z_set = 1;
    } else { /* XYSCREEN: s13.2 x in the high half, y in the low half */
        v->screen[0] = (float)(int16_t)(w1 >> 16) / 4.0f;
        v->screen[1] = (float)(int16_t)(w1 & 0xFFFF) / 4.0f;
        v->screen_set = 1;
    }
    float p[4];
    Emit e;
    unsigned char *rec = arena_alloc(c->arena, 16);
    if (!rec) {
        c->failed = 1;
        return NULL;
    }
    if (!g->vp_known || !slot_clip(g, v, p) || p[3] <= 1e-6f || !ndc_begin(c, &e, 2, v->rgba_set ? LIGHTING : 0))
        return NULL;
    ndc_record(rec, v, p[0] / p[3], p[1] / p[3], p[2] / p[3]);
    emit(&e, (uint32_t)OP_VTX << 24 | 1u << 12 | (slot + 1) << 1, (uintptr_t)rec);
    if (v->st_set)
        emit(&e, (uint32_t)OP_MODIFYVTX << 24 | (uint32_t)MWO_ST << 16 | slot * 2, v->st);
    return ndc_end(c, &e);
}

/* The two-triangle list that draws G_LINE3D w0, or NULL (line dropped). */
static const uintptr_t *line_list(Ctx *c, uint32_t w0)
{
    Geo *g = &c->geo;
    const uint32_t i0 = (w0 >> 16 & 0xFF) / 2, i1 = (w0 >> 8 & 0xFF) / 2;
    float a[4], b[4];
    if (i0 >= LINE_SLOT || i1 >= LINE_SLOT || !g->vp_known || !slot_clip(g, &g->vtx[i0], a) ||
        !slot_clip(g, &g->vtx[i1], b))
        return NULL;
    if (!clip_to_plane(a, b, 1) || !clip_to_plane(a, b, -1) || a[3] <= 1e-6f || b[3] <= 1e-6f)
        return NULL;
    const Slot *ends[2] = {&g->vtx[i0], &g->vtx[i1]};
    Emit e;
    if (!ndc_begin(c, &e, 10, ends[0]->rgba_set || ends[1]->rgba_set ? LIGHTING : 0))
        return NULL;
    const float an[3] = {a[0] / a[3], a[1] / a[3], a[2] / a[3]}, bn[3] = {b[0] / b[3], b[1] / b[3], b[2] / b[3]};
    /* screen pixels: x = ndc x * scale + trans, y = trans - ndc y * scale */
    float dx = (bn[0] - an[0]) * g->vp_scale[0], dy = -(bn[1] - an[1]) * g->vp_scale[1];
    const float len = sqrtf(dx * dx + dy * dy), half = (1.5f + (float)(w0 & 0xFF) * 0.5f) / 2;
    if (len < 1e-6f) {
        dx = 1;
        dy = 0;
    } else {
        dx /= len;
        dy /= len;
    }
    const float off[2] = {-dy * half / g->vp_scale[0], -dx * half / g->vp_scale[1]}; /* normal, in NDC */
    const float *end[2] = {an, bn};
    unsigned char *vtx = arena_alloc(c->arena, 4 * 16);
    if (!vtx) {
        c->failed = 1;
        return NULL;
    }
    float q[4][2];
    for (int k = 0; k < 4; k++) { /* 0, 1: first end +- normal; 2, 3: second end */
        const float *p = end[k / 2], s = k & 1 ? -1.0f : 1.0f;
        q[k][0] = p[0] + s * off[0];
        q[k][1] = p[1] + s * off[1];
        ndc_record(vtx + k * 16, ends[k / 2], q[k][0], q[k][1], p[2]);
    }
    emit(&e, (uint32_t)OP_VTX << 24 | 4u << 12 | (uint32_t)VERTICES << 1, (uintptr_t)vtx);
    for (int k = 0; k < 4; k++)
        if (ends[k / 2]->st_set)
            emit(&e, (uint32_t)OP_MODIFYVTX << 24 | (uint32_t)MWO_ST << 16 | (uint32_t)(LINE_SLOT + k) * 2,
                 ends[k / 2]->st);
    /* Lines are not culled: face the viewer under the cull mode in force, or lift a known CULL_BOTH. */
    const uint32_t cull = g->mode & g->mode_known & CULL_BOTH;
    const float facing = cull == CULL_FRONT ? 1.0f : -1.0f; /* sign of the renderer's cross product */
    if (cull == CULL_BOTH)
        emit(&e, (uint32_t)OP_GEOMETRYMODE << 24 | (~(uint32_t)CULL_BOTH & 0xFFFFFF), 0);
    static const int tris[2][3] = {{0, 1, 2}, {1, 3, 2}};
    for (int t = 0; t < 2; t++) {
        int v1 = tris[t][0], v2 = tris[t][1], v3 = tris[t][2];
        const float cross =
            (q[v1][0] - q[v2][0]) * (q[v3][1] - q[v2][1]) - (q[v1][1] - q[v2][1]) * (q[v3][0] - q[v2][0]);
        if (cross * facing < 0) {
            const int swap = v2;
            v2 = v3;
            v3 = swap;
        }
        emit(&e, (uint32_t)OP_TRI1 << 24 | (uint32_t)(LINE_SLOT + v1) * 2 << 16 | (uint32_t)(LINE_SLOT + v2) * 2 << 8 |
                     (uint32_t)(LINE_SLOT + v3) * 2,
             0);
    }
    if (cull == CULL_BOTH)
        emit(&e, (uint32_t)OP_GEOMETRYMODE << 24 | 0xFFFFFF, CULL_BOTH);
    return ndc_end(c, &e);
}

/* A two-command list: the colour image command and the renderer's own command after it. */
static const uintptr_t *color_image_list(Ctx *c, const uintptr_t *cmd, uint32_t phys)
{
    uintptr_t extra[2] = {0, 0};
    if (!c->hooks || !c->hooks->color_image || (c->geo.zimg_known && phys == c->geo.zimg_phys) ||
        !c->hooks->color_image(c->hooks->user, phys, ((uint32_t)cmd[0] & 0xFFF) + 1, extra))
        return NULL;
    uintptr_t *out = arena_alloc(c->arena, 3 * 2 * sizeof *out);
    if (!out) {
        c->failed = 1;
        return NULL;
    }
    out[0] = cmd[0];
    out[1] = cmd[1];
    out[2] = extra[0];
    out[3] = extra[1];
    out[4] = (uintptr_t)OP_ENDDL << 24;
    out[5] = 0;
    return out;
}

/* Commands whose second word is an address the renderer dereferences. */
static int addressed(uint32_t op)
{
    return op == OP_VTX || op == OP_MTX || op == OP_MOVEMEM || op == OP_DMA_IO || op == OP_SETTIMG ||
           op == OP_SETZIMG || op == OP_SETCIMG;
}

/* Number of commands the RSP runs in the list at phys before it ends or branches away; *ends is
 * cleared when memory or the command limit cuts the list first. */
static uint32_t list_length(Ctx *c, uint32_t phys, int *ends)
{
    uint32_t n = 0;
    *ends = 0;
    while (n < N64C_DL_MAX_COMMANDS && in_memory(c, phys + n * 8, 8)) {
        uint32_t w0 = word_at(c, phys + n * 8);
        n++;
        if ((w0 >> 24) == OP_ENDDL || ((w0 >> 24) == OP_DL && (w0 >> 16 & 1))) {
            *ends = 1;
            break;
        }
    }
    return n;
}

static const uintptr_t *translate(Ctx *c, uint32_t dl, int depth)
{
    uint32_t phys = physical(c, dl);
    if (depth > MAX_DEPTH || !in_memory(c, phys, 8) || c->stats->commands >= N64C_DL_MAX_COMMANDS) {
        c->stats->bad++;
        return NULL;
    }
    int ends;
    uint32_t n = list_length(c, phys, &ends);
    uintptr_t *out = arena_alloc(c->arena, ((size_t)n + 1) * 2 * sizeof *out);
    if (!out) {
        c->failed = 1;
        return NULL;
    }
    c->stats->lists++;
    uint32_t i = 0;
    for (; i < n && !c->failed; i++) {
        uint32_t w0 = word_at(c, phys + i * 8), w1 = word_at(c, phys + i * 8 + 4);
        uintptr_t *cmd = out + i * 2;
        cmd[0] = w0;
        cmd[1] = w1;
        c->stats->commands++;
        switch (w0 >> 24) {
        case OP_VTX:
            cmd[1] = operand(c, w1, (w0 >> 12 & 0xFF) * 16, vertex_words, 4);
            geo_vertices(&c->geo, w0, (const unsigned char *)cmd[1]);
            break;
        case OP_MTX:
            cmd[1] = operand(c, w1, 64, plain_words, 1);
            if (cmd[1])
                geo_matrix(&c->geo, cmd);
            break;
        case OP_POPMTX:
            for (uint32_t k = w1 / 64; k-- && c->geo.mv_size > 0;)
                c->geo.mv_size--;
            break;
        case OP_GEOMETRYMODE:
            c->geo.mode = (c->geo.mode & w0) | w1;
            c->geo.mode_known |= (~w0 & 0xFFFFFF) | w1;
            break;
        case OP_MODIFYVTX: {
            const uint32_t where = w0 >> 16 & 0xFF, slot = (w0 & 0xFFFF) / 2;
            if (where == MWO_ST) { /* the renderer keeps texture coordinates itself */
                if (slot < VERTICES) {
                    c->geo.vtx[slot].st = w1;
                    c->geo.vtx[slot].st_set = 1;
                }
                break;
            }
            const uintptr_t *list = where == MWO_RGBA || where == MWO_XYSCREEN || where == MWO_ZSCREEN ? modify_list(c, w0, w1) : NULL;
            cmd[0] = (uintptr_t)(list ? OP_DL : OP_NOOP) << 24;
            cmd[1] = (uintptr_t)list;
            c->stats->modifies += list != NULL;
            c->stats->modifies_dropped += list == NULL;
            break;
        }
        case OP_LINE3D: {
            const uintptr_t *list = line_list(c, w0);
            cmd[0] = (uintptr_t)(list ? OP_DL : OP_NOOP) << 24;
            cmd[1] = (uintptr_t)list;
            c->stats->lines += list != NULL;
            c->stats->lines_dropped += list == NULL;
            break;
        }
        case OP_MOVEMEM: {
            uint32_t len = ((w0 >> 19 & 0x1F) + 1) * 8, index = w0 & 0xFF;
            if (index == MV_MMTX || index == MV_PMTX || index == MV_MATRIX)
                cmd[1] = operand(c, w1, len, plain_words, 1);
            else if (index == MV_VIEWPORT) {
                cmd[1] = operand(c, w1, len, halfword_words, 1);
                if (cmd[1]) { /* Vp: scale[4] then trans[4], in quarter pixels */
                    int16_t vp[8];
                    memcpy(vp, (const void *)cmd[1], sizeof vp);
                    c->geo.vp_scale[0] = vp[0] / 4.0f;
                    c->geo.vp_scale[1] = vp[1] / 4.0f;
                    c->geo.vp_trans[0] = vp[4] / 4.0f;
                    c->geo.vp_trans[1] = vp[5] / 4.0f;
                    c->geo.vp_scale[2] = vp[2];
                    c->geo.vp_trans[2] = vp[6];
                    c->geo.vp_known = 1;
                }
            } else
                cmd[1] = operand(c, w1, len, byte_words, 1);
            break;
        }
        case OP_MOVEWORD:
            if ((w0 >> 16 & 0xFF) == MW_SEGMENT)
                c->seg[(w0 & 0xFFFF) / 4 & 0x0F] = w1 & 0x1FFFFFFF;
            break;
        case OP_DMA_IO:
            cmd[1] = operand(c, w1, 8, plain_words, 1);
            break;
        case OP_LOAD_UCODE:
            cmd[0] = (uintptr_t)OP_NOOP << 24;
            cmd[1] = 0;
            c->stats->ucode_loads++;
            break;
        case OP_DL: {
            const uintptr_t *callee = translate(c, w1, depth + 1);
            if (callee)
                cmd[1] = (uintptr_t)callee;
            else { /* unresolved: end here rather than hand the renderer a raw address */
                cmd[0] = (uintptr_t)OP_ENDDL << 24;
                cmd[1] = 0;
            }
            break;
        }
        case OP_RDPHALF_1:
            if (i + 1 < n && word_at(c, phys + (i + 1) * 8) >> 24 == OP_BRANCH_Z) {
                uint32_t saved[16];
                Geo *geo = malloc(sizeof *geo); /* the fall-through path runs with the current state */
                if (!geo) {
                    c->failed = 1;
                    break;
                }
                memcpy(saved, c->seg, sizeof saved);
                *geo = c->geo;
                const uintptr_t *target = translate(c, w1, depth + 1);
                memcpy(c->seg, saved, sizeof saved);
                c->geo = *geo;
                free(geo);
                if (target)
                    cmd[1] = (uintptr_t)target;
            }
            break;
        case OP_SETTIMG:
            cmd[1] = operand(c, w1, 0, NULL, 1);
            c->timg_cmd = cmd;
            c->timg_phys = physical(c, w1);
            c->timg_width = (w0 & 0xFFF) + 1;
            c->timg_siz = w0 >> 19 & 3;
            c->timg_bytes = 0;
            break;
        case OP_LOADBLOCK:
            texture_reach(c, (w0 & 0xFFF) * c->timg_width + (w1 >> 12 & 0xFFF) + 1, c->timg_siz);
            break;
        case OP_LOADTILE:
            texture_reach(c, ((w1 & 0xFFF) >> 2) * c->timg_width + ((w1 >> 12 & 0xFFF) >> 2) + 1, c->timg_siz);
            break;
        case OP_LOADTLUT:
            texture_reach(c, ((w1 >> 12 & 0xFFF) >> 2) + 1, SIZ_16B);
            break;
        case OP_SETZIMG:
            cmd[1] = operand(c, w1, 0, NULL, 1);
            c->geo.zimg_phys = physical(c, w1);
            c->geo.zimg_known = 1;
            break;
        case OP_SETCIMG: {
            cmd[1] = operand(c, w1, 0, NULL, 1);
            const uintptr_t *list = cmd[1] ? color_image_list(c, cmd, physical(c, w1)) : NULL;
            if (list) {
                cmd[0] = (uintptr_t)OP_DL << 24;
                cmd[1] = (uintptr_t)list;
            }
            break;
        }
        default:
            if ((w0 >> 24) > OP_LINE3D && (w0 >> 24) < OP_FIRST_SP && !c->stats->unknown++)
                c->stats->unknown_op = w0 >> 24;
            break;
        }
        if (cmd[1] == 0 && w1 != 0 && addressed(w0 >> 24)) { /* never hand the renderer a raw address */
            cmd[0] = (uintptr_t)OP_NOOP << 24;
            cmd[1] = 0;
        }
    }
    /* The copy always ends in G_ENDDL: after a branch away it is never reached; after a cut the
     * renderer stops where the RSP would have read past memory. */
    out[i * 2] = (uintptr_t)OP_ENDDL << 24;
    out[i * 2 + 1] = 0;
    c->stats->bad += !ends;
    return out;
}

const uintptr_t *n64c_dl_translate(const N64cGfxMem *mem, uint32_t dl, N64cDlArena *arena, N64cDlStats *stats,
                                   const N64cDlHooks *hooks)
{
    Ctx *c = calloc(1, sizeof *c);
    *stats = (N64cDlStats){0};
    if (!c)
        return NULL;
    *c = (Ctx){.mem = mem, .arena = arena, .stats = stats, .hooks = hooks};
    c->geo.mv_size = 1; /* the renderer resets its stack to one entry per frame */
    const uintptr_t *root = translate(c, dl, 0);
    const int failed = c->failed;
    free(c);
    return failed ? NULL : root;
}
