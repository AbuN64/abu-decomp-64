# SPDX-License-Identifier: GPL-3.0-or-later
# Shim tests live beside the shim: N64 names are allowed here and nowhere else outside the shim and the port's boundary directory.
# The stand-in for a project's generated charged table (charged.py): n64c_init refuses a link without one.
add_library(n64c_test_charged OBJECT test_charged_table.c)
foreach(t sched scsched interrupts si_timing clock charge gu pi_cont si_pfs cpak dl audio vi_dp rnc memory memory_win pi_relocate pi_resources pi_packed system fpu fpu_thread al albnk tas reach menu)
    add_executable(n64c_test_${t} test_${t}.c)
    target_link_libraries(n64c_test_${t} PRIVATE n64compat $<TARGET_OBJECTS:n64c_test_charged>)
    target_compile_options(n64c_test_${t} PRIVATE -Wall -Wextra -Werror -Wno-unused-parameter)
    add_test(NAME n64c_${t} COMMAND n64c_test_${t})
endforeach()
add_executable(n64c_test_charge_refuse test_charge_refuse.c)
target_link_libraries(n64c_test_charge_refuse PRIVATE n64compat)
target_compile_options(n64c_test_charge_refuse PRIVATE -Wall -Wextra -Werror)
add_test(NAME n64c_charge_refuse COMMAND n64c_test_charge_refuse)
# The custom-command form of the entry charge (cmake/n64c_charge.cmake): an original unit compiled by an
# add_custom_command that runs the compiler (a port's unitstep), tracing off. Its object carries the entry call (audit)
# and the linked executable is charged by it; an object compiled without the exported flags fails the audit.
include(${N64C_SHIM_DIR}/cmake/n64c_charge.cmake)
set(custom_unit "${CMAKE_CURRENT_BINARY_DIR}/charge_custom_unit.o")
set(plain_unit "${CMAKE_CURRENT_BINARY_DIR}/charge_plain_unit.o")
add_custom_command(OUTPUT "${custom_unit}"
    COMMAND "${CMAKE_C_COMPILER}" $<TARGET_PROPERTY:n64compat,INTERFACE_COMPILE_OPTIONS> ${N64C_CHARGE_COMPILE_FLAGS}
        -c "${CMAKE_CURRENT_SOURCE_DIR}/test_charge_custom_unit.c" -o "${custom_unit}"
    DEPENDS test_charge_custom_unit.c VERBATIM)
add_custom_command(OUTPUT "${plain_unit}"
    COMMAND "${CMAKE_C_COMPILER}" $<TARGET_PROPERTY:n64compat,INTERFACE_COMPILE_OPTIONS>
        -c "${CMAKE_CURRENT_SOURCE_DIR}/test_charge_custom_unit.c" -o "${plain_unit}"
    DEPENDS test_charge_custom_unit.c VERBATIM)
set_source_files_properties("${custom_unit}" "${plain_unit}" PROPERTIES EXTERNAL_OBJECT TRUE GENERATED TRUE)
add_executable(n64c_test_charge_custom test_charge_custom.c test_charge_custom_table.c "${custom_unit}")
add_custom_target(n64c_charge_plain_unit ALL DEPENDS "${plain_unit}")
target_link_libraries(n64c_test_charge_custom PRIVATE ${N64C_CHARGE_HOOK_LIBRARY})
target_compile_options(n64c_test_charge_custom PRIVATE -Wall -Wextra -Werror)
add_test(NAME n64c_charge_custom COMMAND n64c_test_charge_custom)
add_test(NAME n64c_charge_custom_audit
    COMMAND "${CMAKE_COMMAND}" -DOBJECT=${custom_unit} -P "${N64C_SHIM_DIR}/cmake/n64c_charge_audit.cmake")
add_test(NAME n64c_charge_plain_audit
    COMMAND "${CMAKE_COMMAND}" -DOBJECT=${plain_unit} -P "${N64C_SHIM_DIR}/cmake/n64c_charge_audit.cmake")
set_tests_properties(n64c_charge_plain_audit PROPERTIES WILL_FAIL TRUE)
if(N64C_M32)
    add_executable(n64c_test_guest_imports test_guest_imports.c)
    target_link_libraries(n64c_test_guest_imports PRIVATE n64compat)
    target_compile_options(n64c_test_guest_imports PRIVATE -Wall -Wextra -Werror)
    add_test(NAME n64c_guest_imports COMMAND n64c_test_guest_imports)
endif()
add_executable(n64c_test_native_varargs test_native_varargs.c)
target_compile_options(n64c_test_native_varargs PRIVATE -Wall -Wextra -Werror)
add_test(NAME n64c_native_varargs COMMAND n64c_test_native_varargs)
# Replay capture for the Fast3D replay gate (cmake/fast3d.cmake: n64c_f3d_replay, replay_ssim.py).
find_package(Python3 COMPONENTS Interpreter REQUIRED)
set(N64C_REPLAY_CAPTURE "" CACHE STRING "Capture directories (list) holding capture.json, rdram.bin and oracle.png (see tests/replay_ssim.py)")

if(TARGET SDL3::SDL3-static)
    add_test(NAME n64c_audio_sdl COMMAND n64c_test_audio sdl)
    set_tests_properties(n64c_audio_sdl PROPERTIES ENVIRONMENT "SDL_AUDIODRIVER=dummy" TIMEOUT 10)
endif()


if(N64C_M32)
    target_compile_options(n64c_test_memory PRIVATE -msse2 -mfpmath=sse)
    target_compile_options(n64c_test_memory_win PRIVATE -msse2 -mfpmath=sse)
endif()
if(WIN32) # the windows live above 2 GB
    target_link_options(n64c_test_memory_win PRIVATE -Wl,--large-address-aware -Wl,--image-base=0x10000000 -Wl,--disable-dynamicbase -Wl,--stack,262144)
endif()
set_tests_properties(n64c_memory n64c_memory_win PROPERTIES TIMEOUT 20)


add_executable(n64c_test_frame ../boot/frame.c ../boot/missing.c test_frame.c)
target_include_directories(n64c_test_frame PRIVATE ../fast3d)
target_link_libraries(n64c_test_frame PRIVATE n64compat)
target_compile_options(n64c_test_frame PRIVATE -Wall -Wextra -Werror)
add_test(NAME n64c_frame_capture COMMAND n64c_test_frame "${CMAKE_CURRENT_BINARY_DIR}")

set_tests_properties(n64c_interrupts PROPERTIES TIMEOUT 10)

# Fake SDL at the device boundary: exercise production mapping and SI without a window.
if(TARGET SDL3::SDL3-static)
    add_executable(n64c_test_input_sdl test_input_sdl.c ../n64c_input.c ../n64c_input_config.c ../n64c_binds.c ../n64c_controls.c ../n64c_menu.c ../n64c_touch.c ../n64c_cont.c ../n64c_cpak.c)
    target_include_directories(n64c_test_input_sdl PRIVATE ..
        $<TARGET_PROPERTY:SDL3::SDL3-static,INTERFACE_INCLUDE_DIRECTORIES>)
    target_compile_definitions(n64c_test_input_sdl PRIVATE N64C_INPUT_SDL3)
    target_compile_options(n64c_test_input_sdl PRIVATE -Wall -Wextra -Werror)
    target_link_libraries(n64c_test_input_sdl PRIVATE Threads::Threads m)
    target_compile_features(n64c_test_input_sdl PRIVATE c_std_11)
    if(N64C_M32)
        target_compile_options(n64c_test_input_sdl PRIVATE -m32)
        target_link_options(n64c_test_input_sdl PRIVATE -m32)
    endif()
    add_test(NAME n64c_input_sdl COMMAND n64c_test_input_sdl)
    set_tests_properties(n64c_input_sdl PROPERTIES TIMEOUT 10)
endif()

# Mocked Windows API contract: context size/alignment is OS-owned, including XState.
add_executable(n64c_test_win_context test_win_context.c)
target_compile_options(n64c_test_win_context PRIVATE -Wall -Wextra -Werror -UNDEBUG)
add_test(NAME n64c_win_context COMMAND n64c_test_win_context)

# The guest's placeholder ranges over a model of the Win32 rules; the module is OS-free, so it runs on every host.
add_executable(n64c_test_win_space test_win_space.c ../os/win_space.c)
target_compile_options(n64c_test_win_space PRIVATE -Wall -Wextra -Werror -UNDEBUG)
add_test(NAME n64c_win_space COMMAND n64c_test_win_space)

# The Windows window refuses a Mesa driver without the DLLs it loads from its own directory (fast3d/f3d_mesa_bundle.h).
add_executable(n64c_test_mesa_bundle test_mesa_bundle.c)
target_compile_options(n64c_test_mesa_bundle PRIVATE -Wall -Wextra -Werror -UNDEBUG)
add_test(NAME n64c_mesa_bundle COMMAND n64c_test_mesa_bundle)
