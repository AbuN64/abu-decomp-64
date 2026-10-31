// SPDX-License-Identifier: GPL-3.0-or-later
// The DLLs Mesa's D3D12 GL driver (libgallium_wgl.dll) loads from its own directory. A missing one is not reported by
// Mesa: dxil.dll absent leaves the shaders unsigned, every pipeline state is refused with E_INVALIDARG and Mesa
// calls _exit(0x80070057) at the first draw.
#pragma once
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

static const char *const f3d_mesa_sibling_dlls[] = {"dxil.dll"};

// Returns the first sibling DLL of the driver at galliumPath that exists() does not find, or NULL when all are there.
static inline const char *f3d_mesa_missing_dll(const char *galliumPath, bool (*exists)(const char *))
{
    const char *slash = strrchr(galliumPath, '\\');
    const char *fwd = strrchr(galliumPath, '/');
    if (fwd > slash)
        slash = fwd;
    int dirLen = slash ? (int)(slash - galliumPath) + 1 : 0;
    for (size_t i = 0; i < sizeof f3d_mesa_sibling_dlls / sizeof *f3d_mesa_sibling_dlls; i++) {
        char path[1024];
        snprintf(path, sizeof path, "%.*s%s", dirLen, galliumPath, f3d_mesa_sibling_dlls[i]);
        if (!exists(path))
            return f3d_mesa_sibling_dlls[i];
    }
    return NULL;
}
