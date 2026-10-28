# SPDX-License-Identifier: GPL-3.0-or-later
# n64c_source_files(<output> <original source tree>): the boot tools' suffix contract,
# including mixed case, with CMake's normal new/deleted-file reconfigure tracking.
include_guard(GLOBAL)
function(n64c_source_files output source)
    get_filename_component(source "${source}" ABSOLUTE)
    find_package(Python3 COMPONENTS Interpreter REQUIRED)
    set(reader "${CMAKE_CURRENT_FUNCTION_LIST_DIR}/../boot/srcfiles.py")
    execute_process(COMMAND "${Python3_EXECUTABLE}" -I -S "${reader}" --cmake-patterns
        OUTPUT_VARIABLE patterns OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY)
    set(globs)
    foreach(pattern IN LISTS patterns)
        list(APPEND globs "${source}/src/${pattern}")
    endforeach()
    file(GLOB_RECURSE inputs CONFIGURE_DEPENDS LIST_DIRECTORIES false RELATIVE "${source}/src" ${globs})
    # Host compiler supplies its runtime; original MIPS libgcc is never a native input.
    list(FILTER inputs EXCLUDE REGEX "(^|/)libgcc/")
    list(TRANSFORM inputs PREPEND "${source}/src/")
    list(SORT inputs)
    set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${reader}")
    set(${output} "${inputs}" PARENT_SCOPE)
endfunction()
