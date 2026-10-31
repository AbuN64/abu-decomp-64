// SPDX-License-Identifier: GPL-3.0-or-later
#include "../fast3d/f3d_mesa_bundle.h"
#include <assert.h>

static char probed[1024];
static bool has_dxil(const char *path)
{
    snprintf(probed, sizeof probed, "%s", path);
    return !strcmp(path, "C:\\game\\dxil.dll");
}
static bool has_nothing(const char *path)
{
    snprintf(probed, sizeof probed, "%s", path);
    return false;
}

int main(void)
{
    assert(!f3d_mesa_missing_dll("C:\\game\\libgallium_wgl.dll", has_dxil));
    assert(!strcmp(probed, "C:\\game\\dxil.dll"));
    const char *missing = f3d_mesa_missing_dll("C:\\game\\libgallium_wgl.dll", has_nothing);
    assert(missing && !strcmp(missing, "dxil.dll"));
    assert(f3d_mesa_missing_dll("/opt/x/libgallium_wgl.dll", has_nothing) && !strcmp(probed, "/opt/x/dxil.dll"));
    assert(f3d_mesa_missing_dll("libgallium_wgl.dll", has_nothing) && !strcmp(probed, "dxil.dll"));
    return 0;
}
