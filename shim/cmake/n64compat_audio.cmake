# SPDX-License-Identifier: GPL-3.0-or-later
# Audio command-list interpreter for the n64compat shim: vendored GPL-2.0-or-later core + thin adapter.
# Builds without the shim target: it needs no libultraship. See audio_abi/SOURCES.md.
get_filename_component(N64C_SHIM_DIR "${CMAKE_CURRENT_LIST_DIR}/.." ABSOLUTE)
include("${CMAKE_CURRENT_LIST_DIR}/warnings.cmake")
set(N64A_DIR "${N64C_SHIM_DIR}/audio_abi")
set(N64A_VENDOR "${N64A_DIR}/vendor/m64p-rsp-hle")
add_library(n64compat_audio_vendor STATIC
    "${N64A_VENDOR}/alist.c" "${N64A_VENDOR}/alist_audio.c" "${N64A_VENDOR}/alist_naudio.c"
    "${N64A_VENDOR}/alist_nead.c" "${N64A_VENDOR}/audio.c" "${N64A_VENDOR}/memory.c")
set_target_properties(n64compat_audio_vendor PROPERTIES C_STANDARD 99 C_EXTENSIONS ON)
target_compile_options(n64compat_audio_vendor PRIVATE -w)
add_library(n64compat_audio STATIC "${N64A_DIR}/n64a_abi.c")
target_include_directories(n64compat_audio PUBLIC "${N64A_DIR}")
target_include_directories(n64compat_audio SYSTEM PRIVATE "${N64A_VENDOR}")
target_link_libraries(n64compat_audio PUBLIC n64compat_audio_vendor)
if(TARGET n64c_options)
    target_link_libraries(n64compat_audio PRIVATE n64c_options)
endif()
n64c_warnings(n64compat_audio)
if(BUILD_TESTING)
    add_executable(test_n64a_abi "${N64A_DIR}/test_n64a_abi.c")
    target_link_libraries(test_n64a_abi PRIVATE n64compat_audio)
    n64c_warnings(test_n64a_abi)
    add_test(NAME n64compat_audio_abi COMMAND test_n64a_abi)
    # Golden replay needs the oracle capture store; it reports SKIPPED (77) when the directory is absent.
    set(N64A_GOLDEN_DIRS "" CACHE STRING "oracle audio golden directories (tNNNN.dp/.rd0.raw/.post), semicolon separated")
    add_executable(test_n64a_golden "${N64A_DIR}/test_n64a_golden.c")
    target_link_libraries(test_n64a_golden PRIVATE n64compat_audio m)
    n64c_warnings(test_n64a_golden)
    add_test(NAME n64compat_audio_golden COMMAND test_n64a_golden 0.95 ${N64A_GOLDEN_DIRS})
    set_tests_properties(n64compat_audio_golden PROPERTIES SKIP_RETURN_CODE 77)
endif()

if(N64C_M32)
    target_compile_options(n64compat_audio_vendor PUBLIC -m32)
    target_link_options(n64compat_audio_vendor PUBLIC -m32)
endif()
