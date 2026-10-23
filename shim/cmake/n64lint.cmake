# SPDX-License-Identifier: GPL-3.0-or-later
# n64lint: keeps N64 names inside the shim and the port's boundary directory (tools/n64lint.c) and enforces the
# port's ledger (ENTRY_POINTS.tsv, OPEN_COUNT, VERSION_DEBT in its --ledger directory).
get_filename_component(N64C_SHIM_DIR "${CMAKE_CURRENT_LIST_DIR}/.." ABSOLUTE)
include("${CMAKE_CURRENT_LIST_DIR}/warnings.cmake")
add_executable(n64lint "${N64C_SHIM_DIR}/tools/n64lint.c")
n64c_warnings(n64lint)
if(BUILD_TESTING)
    add_executable(test_n64lint "${N64C_SHIM_DIR}/tests/test_n64lint.c")
    n64c_warnings(test_n64lint)
    add_test(NAME n64lint_rules COMMAND test_n64lint)
endif()
