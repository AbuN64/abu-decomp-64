/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_DL_H
#define N64C_DL_H
/* n64c_dl: the port's display-list boundary for a host renderer that reads plain host pointers.
 * The game's lists use RSP addressing: segment-relative (segment table set by G_MOVEWORD), physical,
 * or KSEG0/KSEG1 addresses. n64c_dl_translate walks a list in RSP execution order, keeps the segment
 * table as the RSP would, and writes a copy of every list it reaches into an arena with each address
 * operand resolved to a host pointer. Game memory is only read.
 *
 * Memory comes in the two forms N64cGfxMem describes:
 * - native (replay == 0): addresses are host pointers (the 32-bit port maps RDRAM at KSEG0), or, when
 *   base is set, physical addresses 0..mask map to base. Records keep their host layout; the RSP
 *   reads texels as bytes, which cartridge DMA leaves in ROM order (n64c_pi.c RAW).
 * - replay (replay != 0): an RDRAM image in host word order (byte a at a^3, halfword a at a^2). Each
 *   record the RSP reads is copied into the arena in host layout by its type: vertices (three
 *   halfword-pair words and one byte word), viewports (halfwords), lights (bytes), texture images
 *   and palettes (bytes, over the extent the loads reach). Matrices and lists are 32-bit words and
 *   are read in place. Addresses past the image's end mirror back into it (mask).
 * A command whose address operand lies outside memory becomes a no-op and is counted in bad.
 *
 * Only commands of the F3DEX2 family are rewritten; every other command is copied unchanged. A
 * G_LOAD_UCODE is replaced by a no-op and counted: the host renderer keeps its own microcode state.
 *
 * L3DEX2 lines (G_LINE3D) become two triangles, since F3DEX2-only renderers have no line command.
 * n64c_dl tracks what the RSP would hold when the line runs: the modelview stack and projection
 * (G_MTX/G_POPMTX), the viewport (G_MOVEMEM), the clip-space position of each loaded vertex
 * (G_VTX) and the cull bits (G_GEOMETRYMODE). The two ends are clipped to the near and far planes
 * and widened in screen space to (1.5 + width / 2) pixels (the public gSPLineW3D description), and
 * the line command is replaced by a call to a generated list: modelview push and projection load
 * of exact power-of-two scales so the quad's vertices are normalized device coordinates, a G_VTX
 * into vertex slots 60-63 (past the 32 an F3DEX2/L3DEX2 RSP has, so no game vertex is replaced),
 * two G_TRI1 facing the viewer under the tracked cull mode, G_POPMTX, and the projection commands
 * the game issued since its latest projection load. Colours and texture coordinates are those of
 * each end's vertex; a lit line is shaded against untransformed lights. A line whose state is
 * unknown (no viewport, matrix or vertex loaded in this list) is dropped and counted in lines_dropped.
 *
 * G_MODIFYVTX with G_MWO_POINT_XYSCREEN, G_MWO_POINT_ZSCREEN or G_MWO_POINT_RGBA (the renderer only applies ST) becomes a
 * call to a generated list of the same shape that reloads that one slot at its screen position
 * (w = 1, depth kept from its load) with its colour, lighting lifted while it loads when lighting is
 * known to be on, and the slot's latest ST applied again. G_MWO_POINT_ZSCREEN (16.16 screen depth in
 * the viewport's z units) sets the slot's depth the same way. Modifies whose state is unknown are
 * dropped and counted in modifies_dropped. */
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
typedef struct N64cGfxMem {
    const uint8_t *base;
    uint32_t mask;
    int replay;
} N64cGfxMem;
typedef struct N64cDlBlock N64cDlBlock;
typedef struct N64cDlArena {
    N64cDlBlock *blocks; /* blocks never move, so pointers into the arena stay valid */
    N64cDlBlock *cur;    /* block being filled */
    size_t used;         /* bytes handed out since the last reset */
} N64cDlArena;
typedef struct N64cDlStats {
    uint32_t commands;   /* commands copied */
    uint32_t lists;      /* lists entered (root, calls, branches) */
    uint32_t ucode_loads;/* G_LOAD_UCODE commands replaced by no-ops */
    uint32_t bad;        /* operands outside memory (command dropped) and cut lists */
    uint32_t lines;      /* G_LINE3D commands drawn as triangles */
    uint32_t lines_dropped; /* G_LINE3D commands with unknown state, off-screen or past the clip planes */
    uint32_t modifies;   /* G_MODIFYVTX position/colour commands turned into reloads */
    uint32_t modifies_dropped;
    uint32_t unknown;    /* commands outside the F3DEX2/L3DEX2 and RDP opcode ranges */
    uint32_t unknown_op; /* opcode of the first of them */
} N64cDlStats;
/* Renderer extension point: called for each G_SETCIMG that does not target the current depth image,
 * with its physical address and width in pixels. A nonzero return appends the command written to
 * extra (two pointer-width words) right after the G_SETCIMG. */
typedef struct N64cDlHooks {
    int (*color_image)(void *user, uint32_t phys, uint32_t width, uintptr_t extra[2]);
    void *user;
} N64cDlHooks;
/* Limit on commands per translation; a list that exceeds it is cut with G_ENDDL and counted in bad. */
#define N64C_DL_MAX_COMMANDS (1u << 20)
void n64c_dl_arena_reset(N64cDlArena *arena);
void n64c_dl_arena_free(N64cDlArena *arena);
/* Translate the list at dl. Returns the host copy of the root list as pairs of pointer-width words
 * (a display-list command in host renderers: 8 bytes on the 32-bit port), or NULL when the arena
 * cannot grow. hooks may be NULL. */
const uintptr_t *n64c_dl_translate(const N64cGfxMem *mem, uint32_t dl, N64cDlArena *arena, N64cDlStats *stats,
                                   const N64cDlHooks *hooks);
#ifdef __cplusplus
}
#endif
#endif
