// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <cstdint>
namespace N64c {
// Fast3D explicitly tags segmented addresses with bit zero. An unresolved tag
// is invalid; the magnitude of a resolved native pointer says nothing about it.
inline bool TextureAddressValid(uintptr_t encoded, uintptr_t resolved)
{
    return resolved != 0 && (!(encoded & 1) || resolved != encoded);
}
}
