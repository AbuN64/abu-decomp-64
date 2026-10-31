// SPDX-License-Identifier: GPL-3.0-or-later
// The DLLs Mesa's D3D12 GL driver (libgallium_wgl.dll) loads from its own directory. A missing one is not reported by
// Mesa: dxil.dll absent leaves the shaders unsigned, every pipeline state is refused with E_INVALIDARG and Mesa
// calls _exit(0x80070057) at the first draw.
#pragma once
#include <string>

namespace N64c {
static const char *const kMesaSiblingDlls[] = {"dxil.dll"};

// Returns the first sibling DLL of the driver at galliumPath that exists() does not find, or nullptr when all are there.
inline const char *f3d_mesa_missing_dll(const std::string &galliumPath, bool (*exists)(const std::string &))
{
    size_t slash = galliumPath.find_last_of("\\/");
    std::string dir = slash == std::string::npos ? std::string() : galliumPath.substr(0, slash + 1);
    for (const char *dll : kMesaSiblingDlls)
        if (!exists(dir + dll))
            return dll;
    return nullptr;
}
} // namespace N64c
