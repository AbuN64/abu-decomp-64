/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The game module API (docs/FORMAT.md boundary 4). Format 1 links the module statically (the original C on the
 * shim); a mod module exports the same GetGameAPI from a shared library. No N64 names here (n64lint). */
#ifndef G_PUBLIC_H
#define G_PUBLIC_H
#include <stddef.h>
#include <stdint.h>

#define GAME_API_VERSION 1
#define GAME_MAX_CONTROLLERS 4

/* One controller poll for one game tick: the TAS movie record, the netcode unit. */
typedef struct {
    uint32_t tick;
    uint16_t buttons;
    int8_t stick_x;
    int8_t stick_y;
} usercmd_t;

/* Engine services the module may call. */
typedef struct {
    int api_version;
    void (*print)(const char *text);
    /* VFS: reads a whole file through the search path (mods over base); NULL when absent. */
    void *(*fs_load)(const char *path, size_t *size);
    void (*fs_free)(void *data);
    const char *(*cvar_get)(const char *name);
    void (*cvar_set)(const char *name, const char *value);
    uint64_t (*time_ns)(void);
} game_import_t;

/* Module entry points the engine calls. */
typedef struct {
    int api_version;
    int (*init)(const game_import_t *import);
    /* One game tick; cmds holds one usercmd per connected controller. Returns 0 to keep running. */
    int (*run_frame)(const usercmd_t *cmds, unsigned count);
    void (*shutdown)(void);
} game_export_t;

const game_export_t *GetGameAPI(const game_import_t *import);

#endif
