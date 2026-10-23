# SPDX-License-Identifier: GPL-3.0-or-later
# Consumer contract: rage_dep(name repo pin [FetchContent_Declare options]) (shared dependency sources), the
# SDL3::SDL3-static target, N64C_M32 and the n64compat target (cmake/n64compat.cmake).
# Fast3D renderer (LUS c6ecfdb, Emill MIT lineage) built from the pinned sources against the
# clean interface headers in fast3d/ (AUDIT.md). No LUS file is copied or
# forked: the clean headers come first on the include path and every grey LUS header is
# replaced by a stub that stops the build if anything still includes it.
get_filename_component(N64C_SHIM_DIR "${CMAKE_CURRENT_LIST_DIR}/.." ABSOLUTE)
if(NOT N64C_M32)
    message(FATAL_ERROR "The Fast3D renderer is built for the 32-bit port (N64C_M32=ON)")
endif()
# The consumer already adds -m32 to the C and C++ flags; C++ is enabled here with them.
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -msse2 -mfpmath=sse")
enable_language(CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_SCAN_FOR_MODULES OFF) # no C++ modules; skips a dependency scan per source
# Find the i386 multiarch builds of the system libraries (tools/i386-deps.sh installs them).
set(CMAKE_LIBRARY_ARCHITECTURE i386-linux-gnu)
if(NOT TARGET SDL3::SDL3-static)
    message(FATAL_ERROR "The Fast3D renderer needs the consumer's SDL3::SDL3-static target")
endif()
set(fast3d_dir "${N64C_SHIM_DIR}/fast3d")

rage_dep(lus https://github.com/Kenix3/libultraship.git c6ecfdb491cc3a0d08aa04eff2f17f0bf5946297
    GIT_SHALLOW FALSE
    GIT_SUBMODULES ""
    SOURCE_SUBDIR fetch-only)
# Dependencies at the versions LUS pins in cmake/dependencies/common.cmake. ImGui takes LUS's
# own configuration patch (ImTextureID as a pointer, which the renderer API relies on).
rage_dep(lus_imgui https://github.com/ocornut/imgui.git v1.91.9b-docking
    GIT_SHALLOW TRUE
    SOURCE_SUBDIR fetch-only)
rage_dep(lus_threadpool https://github.com/bshoshany/thread-pool.git v4.1.0
    GIT_SHALLOW TRUE
    SOURCE_SUBDIR fetch-only)
rage_dep(lus_prism https://github.com/KiritoDv/prism-processor.git 1de054450e7b3c5f777d2e3dfcb228ad120c329d
    GIT_SHALLOW FALSE)
rage_dep(lus_monocypher https://github.com/LoupVaillant/Monocypher.git 0d85f98c9d9b0227e42cf795cb527dff372b40a4
    GIT_SHALLOW FALSE
    SOURCE_SUBDIR fetch-only)
rage_dep(lus_stb https://github.com/nothings/stb.git 0bc88af4de5fb022db643c2d8e549a0927749354
    GIT_SHALLOW FALSE
    SOURCE_SUBDIR fetch-only)
set(PRISM_STANDALONE OFF CACHE BOOL "" FORCE)
FetchContent_MakeAvailable(lus lus_imgui lus_threadpool lus_prism lus_monocypher lus_stb)
# prism replaces CMAKE_CXX_FLAGS in its own CMakeLists, so it gets the port's pointer width here.
target_compile_options(prism PRIVATE -m32 -msse2 -mfpmath=sse)
execute_process(COMMAND git -C "${lus_imgui_SOURCE_DIR}" apply --reverse --check
        "${lus_SOURCE_DIR}/cmake/dependencies/patches/imgui-fixes-and-config.patch"
    RESULT_VARIABLE fast3d_imgui_patched OUTPUT_QUIET ERROR_QUIET)
if(NOT fast3d_imgui_patched EQUAL 0)
    execute_process(COMMAND git -C "${lus_imgui_SOURCE_DIR}" apply
            "${lus_SOURCE_DIR}/cmake/dependencies/patches/imgui-fixes-and-config.patch"
        COMMAND_ERROR_IS_FATAL ANY)
endif()

find_package(OpenGL REQUIRED)
find_library(FAST3D_EGL_LIBRARY EGL REQUIRED) # n64c_f3d picks the software EGL device
find_package(Threads REQUIRED)
find_package(nlohmann_json REQUIRED)
find_package(tinyxml2 REQUIRED)
find_package(spdlog REQUIRED)
find_package(libzip REQUIRED)

add_library(fast3d_imgui STATIC
    ${lus_imgui_SOURCE_DIR}/imgui.cpp ${lus_imgui_SOURCE_DIR}/imgui_demo.cpp
    ${lus_imgui_SOURCE_DIR}/imgui_draw.cpp ${lus_imgui_SOURCE_DIR}/imgui_tables.cpp
    ${lus_imgui_SOURCE_DIR}/imgui_widgets.cpp
    ${lus_imgui_SOURCE_DIR}/backends/imgui_impl_opengl3.cpp
    ${lus_imgui_SOURCE_DIR}/backends/imgui_impl_sdl3.cpp)
target_include_directories(fast3d_imgui SYSTEM PUBLIC ${lus_imgui_SOURCE_DIR} ${lus_imgui_SOURCE_DIR}/backends)
target_link_libraries(fast3d_imgui PRIVATE SDL3::SDL3-static)
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/fast3d-stb/stb_impl.c"
    "#define STB_IMAGE_IMPLEMENTATION\n#include \"stb_image.h\"\n")
add_library(fast3d_stb STATIC "${CMAKE_CURRENT_BINARY_DIR}/fast3d-stb/stb_impl.c")
target_include_directories(fast3d_stb PUBLIC ${lus_stb_SOURCE_DIR})
add_library(fast3d_monocypher STATIC
    ${lus_monocypher_SOURCE_DIR}/src/monocypher.c
    ${lus_monocypher_SOURCE_DIR}/src/optional/monocypher-ed25519.c)
target_include_directories(fast3d_monocypher PUBLIC
    ${lus_monocypher_SOURCE_DIR}/src ${lus_monocypher_SOURCE_DIR}/src/optional)

# Grey header guard: one stub per grey LUS include path that has no clean replacement.
set(fast3d_guard "${CMAKE_CURRENT_BINARY_DIR}/fast3d-guard")
file(REMOVE_RECURSE "${fast3d_guard}")
file(STRINGS "${N64C_SHIM_DIR}/AUDIT-files.tsv" fast3d_audit)
foreach(row IN LISTS fast3d_audit)
    if(row MATCHES "^include/([^\t]+)\tgrey\t")
        set(header "${CMAKE_MATCH_1}")
        if(NOT EXISTS "${fast3d_dir}/include/${header}")
            file(WRITE "${fast3d_guard}/${header}"
                "#error \"grey LUS header ${header} is blocked (shim AUDIT.md)\"\n")
        endif()
    endif()
endforeach()

# Renderer, OpenGL/SDL backends and the ship framework they use. Left out: the grey bridge and
# os_* code (src/libultraship), Fast3dWindow/Fast3dGui and the SDL backend (they wire LUS's GUI and
# grey debugger window; the port has its own window, fast3d/f3d_window.cpp), the OTR display-list/vertex/matrix resources and their factories
# (they need the game-facing GBI), scripting and MPQ archives (off), non-Linux backends.
set(lus_src "${lus_SOURCE_DIR}/src")
file(GLOB_RECURSE fast3d_sources CONFIGURE_DEPENDS "${lus_src}/fast/*.cpp" "${lus_src}/ship/*.cpp" "${lus_src}/ship/*.c")
list(FILTER fast3d_sources EXCLUDE REGEX
    "/fast/Fast3d(Window|Gui)\\.cpp|/fast/backends/gfx_(direct3d|dxgi|metal|sdl)|/fast/resource/factory/|/fast/resource/type/(DisplayList|Vertex|Matrix)\\.cpp|/ship/(scripting|security|port)/|WasapiAudioPlayer|CoreAudioAudioPlayer|/ship/utils/Apple")
configure_file("${lus_src}/ship/install_config.h.in" "${CMAKE_CURRENT_BINARY_DIR}/fast3d-config/ship/install_config.h" @ONLY)
include("${lus_SOURCE_DIR}/cmake/cvars.cmake")
add_library(rage_fast3d STATIC ${fast3d_sources})
target_include_directories(rage_fast3d BEFORE PUBLIC "${fast3d_dir}/include" "${fast3d_guard}")
# Third-party headers: consumers compile with their own warnings, not LUS's.
target_include_directories(rage_fast3d SYSTEM PUBLIC
    "${lus_SOURCE_DIR}/include" "${lus_SOURCE_DIR}/include/libultraship" "${lus_src}"
    "${CMAKE_CURRENT_BINARY_DIR}/fast3d-config" "${lus_threadpool_SOURCE_DIR}/include")
target_compile_definitions(rage_fast3d PUBLIC ENABLE_OPENGL F3DEX_GBI_2
    LUS_VERSION_EPOCH=2 LUS_VERSION_MAJOR=0 LUS_VERSION_MINOR=0 LUS_VERSION_PATCH=0)
target_compile_options(rage_fast3d PRIVATE -w)
target_link_libraries(rage_fast3d PUBLIC fast3d_imgui prism fast3d_monocypher nlohmann_json::nlohmann_json
    tinyxml2::tinyxml2 spdlog::spdlog SDL3::SDL3-static OpenGL::GL Threads::Threads
    PRIVATE fast3d_stb libzip::zip)

# Replay: a captured display list through the port's DL boundary into Fast3D, gated by the same
# SSIM check as the hand-written renderer (tests/replay_ssim.py).
add_custom_command(OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/fast3d-shaders.o2r"
    COMMAND ${CMAKE_COMMAND} -E tar cf "${CMAKE_CURRENT_BINARY_DIR}/fast3d-shaders.o2r" --format=zip
        shaders/opengl/default.shader.glsl
    WORKING_DIRECTORY "${lus_src}/fast"
    DEPENDS "${lus_src}/fast/shaders/opengl/default.shader.glsl"
    VERBATIM)
add_library(n64c_fast3d STATIC "${fast3d_dir}/f3d_window.cpp" "${fast3d_dir}/n64c_f3d.cpp")
target_include_directories(n64c_fast3d PUBLIC "${fast3d_dir}")
target_link_libraries(n64c_fast3d PUBLIC rage_fast3d n64compat PRIVATE ${FAST3D_EGL_LIBRARY})
target_compile_options(n64c_fast3d PRIVATE -Wall -Wextra -Werror)
add_executable(n64c_f3d_replay "${fast3d_dir}/f3d_replay.cpp" "${CMAKE_CURRENT_BINARY_DIR}/fast3d-shaders.o2r")
target_link_libraries(n64c_f3d_replay PRIVATE n64c_fast3d)
target_compile_options(n64c_f3d_replay PRIVATE -Wall -Wextra -Werror)
# N64C_REPLAY_CAPTURE: one capture directory or a list of them; one gate test each, named after it.
if(BUILD_TESTING)
    foreach(capture IN LISTS N64C_REPLAY_CAPTURE)
        get_filename_component(capture_name "${capture}" NAME)
        add_test(NAME n64c_f3d_replay_${capture_name} COMMAND "${Python3_EXECUTABLE}" -I
            "${N64C_SHIM_DIR}/tests/replay_ssim.py"
            "$<TARGET_FILE:n64c_f3d_replay>" "${capture}" "${CMAKE_CURRENT_BINARY_DIR}/f3d-replay-${capture_name}")
        set_tests_properties(n64c_f3d_replay_${capture_name} PROPERTIES ENVIRONMENT "SDL_VIDEODRIVER=offscreen"
            TIMEOUT 60)
    endforeach()
endif()

# Context ownership is tested with SDL calls mocked; no renderer or display required.
add_executable(n64c_test_f3d_context "${fast3d_dir}/../tests/test_f3d_context.cpp")
target_link_libraries(n64c_test_f3d_context PRIVATE SDL3::SDL3-static)
target_compile_options(n64c_test_f3d_context PRIVATE -Wall -Wextra -Werror)
add_test(NAME n64c_f3d_context COMMAND n64c_test_f3d_context)

# Color-image resolution changes use a mocked backend; no renderer or display required.
add_executable(n64c_test_f3d_surface "${fast3d_dir}/../tests/test_f3d_surface.cpp")
target_compile_options(n64c_test_f3d_surface PRIVATE -Wall -Wextra -Werror)
add_test(NAME n64c_f3d_surface COMMAND n64c_test_f3d_surface)
