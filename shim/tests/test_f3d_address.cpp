// SPDX-License-Identifier: GPL-3.0-or-later
#include "../fast3d/f3d_address.h"
#include <cstdio>
int main()
{
    struct Case { uintptr_t encoded, resolved; bool valid; };
    const Case cases[] = {
        {0x032a1000, 0x032a1000, true}, // ordinary Win32 heap, not a module
        {0x1000, 0x1000, true},         // low native allocation
        {0x8a001000, 0x8a001000, true},
        {0, 0, false},
        {0x03001001, 0x03001001, false}, // unresolved segmented address
        {0x03001001, 0x032a1000, true},  // segment resolved into a low native allocation
        {0x03001001, 0x8a001000, true},
        {0x03001001, 0, false}
    };
    for (const auto& c : cases)
        if (N64c::TextureAddressValid(c.encoded, c.resolved) != c.valid) return 1;
    std::puts("texture address contract: 8 cases passed");
    return 0;
}
