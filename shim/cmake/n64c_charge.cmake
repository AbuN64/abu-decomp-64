# SPDX-License-Identifier: GPL-3.0-or-later
# The original-function entry charge, part of every build of a port (CONTRACTS.md "Work scale"). Include after
# n64compat.cmake.
#   n64c_charge_entries(<target> NS_PER_ENTRY <decimal> MANIFEST <effective manifest.json> SOURCE <original source dir>
#                       ORIGINAL_SOURCES <files>)
# NS_PER_ENTRY is the project's one declaration of the work scale (guest ns one charged entry costs): compiled into the
# executable, so nothing at run time can change it. There is no default; a call without it stops the configure.
# The original units get the compiler's entry call, whose hook is n64c_charge.c in n64compat; the generated table names
# the charged functions. Tracing (boot/trace.c) is a separate, optional layer that takes the same call.
#
# Two ways to compile an original unit, one set of flags:
#  - Source property (set by n64c_charge_entries on ORIGINAL_SOURCES): for units the target compiles itself. A custom
#    compiler command is not a compile of the target and ignores it.
#  - Exported flags (any add_custom_command that runs the compiler on an original unit): put N64C_CHARGE_COMPILE_FLAGS
#    on its compile line and link the produced object into a target that links ${N64C_CHARGE_HOOK_LIBRARY} (the hook):
#        add_custom_command(OUTPUT unit.o
#            COMMAND ${CMAKE_C_COMPILER} $<TARGET_PROPERTY:n64compat,INTERFACE_COMPILE_OPTIONS>
#                    ${N64C_CHARGE_COMPILE_FLAGS} -c unit.c -o unit.o DEPENDS unit.c VERBATIM)
#        set_source_files_properties(unit.o PROPERTIES EXTERNAL_OBJECT TRUE GENERATED TRUE)
#        target_sources(game PRIVATE unit.o)
#        target_link_libraries(game PRIVATE ${N64C_CHARGE_HOOK_LIBRARY})
#    n64c_charge_audit.cmake (cmake -DOBJECT=<unit.o> -P) refuses an object that carries no entry call.
include_guard(GLOBAL)
set(N64C_CHARGE_COMPILE_FLAGS -finstrument-functions -fno-omit-frame-pointer CACHE INTERNAL
    "The exact compile flags of an original unit (list)")
set(N64C_CHARGE_HOOK_LIBRARY n64compat CACHE INTERNAL "The library holding the entry hook an original unit calls")
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
        COMPILE_OPTIONS "${N64C_CHARGE_COMPILE_FLAGS}")
endfunction()
