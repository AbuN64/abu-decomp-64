# SPDX-License-Identifier: GPL-3.0-or-later
# Shim tests live beside the shim: N64 names are allowed here and nowhere else outside the shim and the port's boundary directory.
foreach(t sched scsched interrupts si_timing clock gu pi_cont si_pfs cpak dl audio vi_dp rnc memory memory_win pi_relocate pi_resources pi_packed system fpu fpu_thread al albnk tas reach menu)
    add_executable(n64c_test_${t} test_${t}.c)
    target_link_libraries(n64c_test_${t} PRIVATE n64compat)
    target_compile_options(n64c_test_${t} PRIVATE -Wall -Wextra -Werror -Wno-unused-parameter)
    add_test(NAME n64c_${t} COMMAND n64c_test_${t})
endforeach()
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
