# SPDX-License-Identifier: GPL-3.0-or-later
# Shim tests live beside the shim: N64 names are allowed here and nowhere else outside platform/n64compat.
foreach(t sched scsched interrupts gu pi_cont si_pfs dl audio vi_dp rnc memory pi_relocate pi_resources system fpu fpu_thread al albnk)
    add_executable(n64c_test_${t} test_${t}.c)
    target_link_libraries(n64c_test_${t} PRIVATE n64compat)
    target_compile_options(n64c_test_${t} PRIVATE -Wall -Wextra -Werror -Wno-unused-parameter)
    add_test(NAME n64c_${t} COMMAND n64c_test_${t})
endforeach()
# Replay capture for the Fast3D replay gate (cmake/fast3d.cmake: n64c_f3d_replay, replay_ssim.py).
find_package(Python3 COMPONENTS Interpreter REQUIRED)
set(N64C_REPLAY_CAPTURE "" CACHE STRING "Capture directories (list) holding capture.json, rdram.bin and oracle.png (see tests/replay_ssim.py)")

if(TARGET SDL3::SDL3-static)
    add_test(NAME n64c_audio_sdl COMMAND n64c_test_audio sdl)
    set_tests_properties(n64c_audio_sdl PROPERTIES ENVIRONMENT "SDL_AUDIODRIVER=dummy" TIMEOUT 10)
endif()


if(RAGE_N64COMPAT_M32)
    target_compile_options(n64c_test_memory PRIVATE -msse2 -mfpmath=sse)
endif()
set_tests_properties(n64c_memory PROPERTIES TIMEOUT 20)


add_executable(n64c_test_sched_glue test_sched_glue.c)
target_include_directories(n64c_test_sched_glue PRIVATE ../decomp)
target_link_libraries(n64c_test_sched_glue PRIVATE n64compat)
target_compile_options(n64c_test_sched_glue PRIVATE -Wall -Wextra -Werror -Wno-unused-parameter)
add_test(NAME n64c_sched_glue COMMAND n64c_test_sched_glue)
set_tests_properties(n64c_sched_glue PROPERTIES TIMEOUT 10)

add_executable(n64c_test_frame ../decomp/frame.c ../decomp/missing.c test_frame.c)
target_include_directories(n64c_test_frame PRIVATE ../fast3d)
target_link_libraries(n64c_test_frame PRIVATE n64compat)
target_compile_options(n64c_test_frame PRIVATE -Wall -Wextra -Werror)
add_test(NAME n64c_frame_capture COMMAND n64c_test_frame "${CMAKE_CURRENT_BINARY_DIR}")

set_tests_properties(n64c_interrupts PROPERTIES TIMEOUT 10)
