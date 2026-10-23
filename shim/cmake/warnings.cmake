# SPDX-License-Identifier: GPL-3.0-or-later
# Warning policy of the shim's own targets (a consumer keeps its own for its code).
function(n64c_warnings target)
    set_target_properties(${target} PROPERTIES C_STANDARD 23 C_STANDARD_REQUIRED ON C_EXTENSIONS OFF)
    target_compile_options(${target} PRIVATE -Wall -Wextra -Wpedantic -Werror)
endfunction()
