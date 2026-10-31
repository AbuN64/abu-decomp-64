# SPDX-License-Identifier: GPL-3.0-or-later
# The original-function entry charge, part of every build of a port (CONTRACTS.md "Work scale"). Include after
# n64compat.cmake.
#   n64c_charge_entries(<target> NS_PER_ENTRY <decimal> MANIFEST <effective manifest.json> SOURCE <original source dir>
#                       ORIGINAL_SOURCES <files>)
# NS_PER_ENTRY is the project's one declaration of the work scale (guest ns one charged entry costs): compiled into the
# executable, so nothing at run time can change it. There is no default; a call without it stops the configure.
# The original units get the compiler's entry call, whose hook is n64c_charge.c in n64compat; the generated table names
# the charged functions. Tracing (boot/trace.c) is a separate, optional layer that takes the same call.
include_guard(GLOBAL)
function(n64c_charge_entries target)
    cmake_parse_arguments(PARSE_ARGV 1 arg "" "NS_PER_ENTRY;MANIFEST;SOURCE" "ORIGINAL_SOURCES")
    foreach(required NS_PER_ENTRY MANIFEST SOURCE ORIGINAL_SOURCES)
        if(NOT arg_${required})
            message(FATAL_ERROR "n64c_charge_entries: ${required} is required (the work scale has no default)")
        endif()
    endforeach()
    if(NOT arg_NS_PER_ENTRY MATCHES "^[0-9]+(\\.[0-9]+)?$" OR arg_NS_PER_ENTRY MATCHES "^0+(\\.0*)?$")
        message(FATAL_ERROR "n64c_charge_entries: NS_PER_ENTRY must be a positive decimal, got '${arg_NS_PER_ENTRY}'")
    endif()
    find_package(Python3 COMPONENTS Interpreter REQUIRED)
    set(script "${N64C_SHIM_DIR}/boot/charged.py")
    set(generated "${CMAKE_CURRENT_BINARY_DIR}/n64c-charge")
    add_custom_command(OUTPUT "${generated}/charged_table.c"
        COMMAND "${Python3_EXECUTABLE}" -I "${script}" --manifest "${arg_MANIFEST}" --source "${arg_SOURCE}"
            --ns-per-entry "${arg_NS_PER_ENTRY}" --output "${generated}"
        DEPENDS "${arg_MANIFEST}" "${script}" VERBATIM)
    target_sources(${target} PRIVATE "${generated}/charged_table.c")
    set_source_files_properties(${arg_ORIGINAL_SOURCES} PROPERTIES
        COMPILE_OPTIONS "-finstrument-functions;-fno-omit-frame-pointer")
endfunction()
