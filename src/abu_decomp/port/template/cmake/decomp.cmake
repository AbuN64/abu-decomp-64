# SPDX-License-Identifier: GPL-3.0-or-later
# The original game source ({{decomp_version}}) at one pinned commit: the one place the pin is spelled besides the
# manifest (platform/port/manifest.json source_pin), which must name the same commit. Declared here; a target that
# compiles the original makes it available.
include(FetchContent)
include("${CMAKE_CURRENT_LIST_DIR}/deps.cmake")
n64c_dep(port_decomp {{decomp_repo}} {{decomp_pin}})
FetchContent_MakeAvailable(port_decomp)
include("${N64C_SHIM_DIR}/cmake/sources.cmake")
n64c_source_files(port_decomp_sources "${port_decomp_SOURCE_DIR}")
set(PORT_DECOMP_VERSION "{{decomp_version}}")
