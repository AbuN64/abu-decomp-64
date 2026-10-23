# The n64 shim (abu-decomp-64/shim, GPL-3.0-or-later) at one pinned commit. A dependency in N64C_DEPS_DIR is shared
# between build trees; without it the configure fetches the shim into its own build tree.
include(FetchContent)
include("${CMAKE_CURRENT_LIST_DIR}/deps.cmake")
n64c_dep(abu_shim {{shim_repo}} {{shim_pin}})
FetchContent_MakeAvailable(abu_shim)
set(N64C_SHIM_DIR "${abu_shim_SOURCE_DIR}/shim")
set(N64C_PORT_DIR "${CMAKE_CURRENT_SOURCE_DIR}/platform/port")
