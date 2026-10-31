// SPDX-License-Identifier: GPL-3.0-or-later
#include "../fast3d/f3d_mesa_bundle.h"
#include <cassert>
#include <cstring>

static std::string probed;
static bool has_dxil(const std::string &path) { probed = path; return path == "C:\\game\\dxil.dll"; }
static bool has_nothing(const std::string &path) { probed = path; return false; }

int main()
{
    using N64c::f3d_mesa_missing_dll;
    assert(!f3d_mesa_missing_dll("C:\\game\\libgallium_wgl.dll", has_dxil));
    assert(probed == "C:\\game\\dxil.dll");
    const char *missing = f3d_mesa_missing_dll("C:\\game\\libgallium_wgl.dll", has_nothing);
    assert(missing && !std::strcmp(missing, "dxil.dll"));
    assert(f3d_mesa_missing_dll("/opt/x/libgallium_wgl.dll", has_nothing) && probed == "/opt/x/dxil.dll");
    assert(f3d_mesa_missing_dll("libgallium_wgl.dll", has_nothing) && probed == "dxil.dll");
    return 0;
}
