# SPDX-License-Identifier: GPL-3.0-or-later
# Hand-written gfx/OS shim (AUDIT.md); audio links the vendored core (audio_abi/SOURCES.md).
# Consumer contract: N64C_M32 (build -m32), an SDL3::SDL3-static target when the window/input/audio backends are wanted.
get_filename_component(N64C_SHIM_DIR "${CMAKE_CURRENT_LIST_DIR}/.." ABSOLUTE)
set(THREADS_PREFER_PTHREAD_FLAG ON)
find_package(Threads REQUIRED)
# os/ is the host seam: os_posix.c and os_win32.c each compile to nothing on the other OS (os/n64c_host.h).
file(GLOB n64compat_sources CONFIGURE_DEPENDS "${N64C_SHIM_DIR}/*.c" "${N64C_SHIM_DIR}/os/*.c")
add_library(n64compat STATIC ${n64compat_sources})
target_include_directories(n64compat PUBLIC "${N64C_SHIM_DIR}")
target_link_libraries(n64compat PUBLIC Threads::Threads m n64compat_audio)
if(TARGET SDL3::SDL3-static)
    target_link_libraries(n64compat PRIVATE SDL3::SDL3-static)
    target_compile_definitions(n64compat PRIVATE N64C_AUDIO_SDL3 N64C_INPUT_SDL3)
endif()
target_compile_features(n64compat PRIVATE c_std_11)
# The shim is built 32-bit so structure sizes and pointer width match the original; 64-bit follows later.
if(N64C_M32 AND NOT ANDROID) # an Android 32-bit ABI is 32-bit already (no -m32)
    target_compile_options(n64compat PUBLIC -m32 -msse2 -mfpmath=sse)
    target_link_options(n64compat PUBLIC -m32)
elseif(N64C_M32 AND CMAKE_SYSTEM_PROCESSOR MATCHES "^(x86|i[3-6]86)")
    target_compile_options(n64compat PUBLIC -msse2 -mfpmath=sse)
endif()
target_compile_options(n64compat PRIVATE -Wall -Wextra -Werror -Wno-unused-parameter)
if(BUILD_TESTING)
    add_subdirectory("${N64C_SHIM_DIR}/tests" n64c-tests)
endif()
