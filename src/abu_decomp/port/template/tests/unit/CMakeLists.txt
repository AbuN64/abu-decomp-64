# SPDX-License-Identifier: GPL-3.0-or-later
add_executable(test_shim_link test_shim_link.c)
target_link_libraries(test_shim_link PRIVATE n64compat)
target_compile_options(test_shim_link PRIVATE -Wall -Wextra -Werror)
add_test(NAME shim_link COMMAND test_shim_link)
set_tests_properties(shim_link PROPERTIES LABELS unit)
