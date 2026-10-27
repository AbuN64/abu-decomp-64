# SPDX-License-Identifier: GPL-3.0-or-later
# Consumer contract: n64c_dep (defined here unless the consumer has its own; cmake/deps.cmake), the
# SDL3::SDL3-static target, N64C_M32 and the n64compat target (cmake/n64compat.cmake).
# Fast3D renderer (LUS c6ecfdb, Emill MIT lineage) built from the pinned sources against the
# clean interface headers in fast3d/ (AUDIT.md). A checked compile view repairs the
# texture address contract without changing pinned sources. Clean headers come first; every grey LUS header is
# replaced by a stub that stops the build if anything still includes it.
get_filename_component(N64C_SHIM_DIR "${CMAKE_CURRENT_LIST_DIR}/.." ABSOLUTE)
if(NOT COMMAND n64c_dep)
    include("${CMAKE_CURRENT_LIST_DIR}/deps.cmake")
endif()
if(NOT N64C_M32)
    message(FATAL_ERROR "The Fast3D renderer is built for the 32-bit port (N64C_M32=ON)")
endif()
# The consumer already adds -m32 to the C and C++ flags; C++ is enabled here with them.
if(NOT ANDROID OR CMAKE_SYSTEM_PROCESSOR MATCHES "^(x86|i[3-6]86)")
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -msse2 -mfpmath=sse")
endif()
enable_language(CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_SCAN_FOR_MODULES OFF) # no C++ modules; skips a dependency scan per source
if(NOT WIN32 AND NOT ANDROID)
    # Find the i386 multiarch builds of the system libraries (tools/i386-deps.sh installs them).
    set(CMAKE_LIBRARY_ARCHITECTURE i386-linux-gnu)
endif()
if(NOT TARGET SDL3::SDL3-static)
    message(FATAL_ERROR "The Fast3D renderer needs the consumer's SDL3::SDL3-static target")
endif()
set(fast3d_dir "${N64C_SHIM_DIR}/fast3d")

n64c_dep(lus https://github.com/Kenix3/libultraship.git c6ecfdb491cc3a0d08aa04eff2f17f0bf5946297
    GIT_SHALLOW FALSE
    GIT_SUBMODULES ""
    SOURCE_SUBDIR fetch-only)
# Dependencies at the versions LUS pins in cmake/dependencies/common.cmake. ImGui takes LUS's
# own configuration patch (ImTextureID as a pointer, which the renderer API relies on).
n64c_dep(lus_imgui https://github.com/ocornut/imgui.git v1.91.9b-docking
    GIT_SHALLOW TRUE
    SOURCE_SUBDIR fetch-only)
n64c_dep(lus_threadpool https://github.com/bshoshany/thread-pool.git v4.1.0
    GIT_SHALLOW TRUE
    SOURCE_SUBDIR fetch-only)
n64c_dep(lus_prism https://github.com/KiritoDv/prism-processor.git 1de054450e7b3c5f777d2e3dfcb228ad120c329d
    GIT_SHALLOW FALSE)
n64c_dep(lus_monocypher https://github.com/LoupVaillant/Monocypher.git 0d85f98c9d9b0227e42cf795cb527dff372b40a4
    GIT_SHALLOW FALSE
    SOURCE_SUBDIR fetch-only)
n64c_dep(lus_stb https://github.com/nothings/stb.git 0bc88af4de5fb022db643c2d8e549a0927749354
    GIT_SHALLOW FALSE
    SOURCE_SUBDIR fetch-only)
set(PRISM_STANDALONE OFF CACHE BOOL "" FORCE)
if(WIN32)
    # Windows (mingw-w64): window and GL context come from SDL (WGL); there is no EGL device to pick, and the C++
    # libraries have no system package, so they are built from their pinned sources (licences in SOURCES.md). They are
    # defined before prism, which looks spdlog up itself and must not find the build host's.
    set(CMAKE_DISABLE_FIND_PACKAGE_spdlog ON)
    set(CMAKE_DISABLE_FIND_PACKAGE_fmt ON)
    include("${CMAKE_CURRENT_LIST_DIR}/fast3d_win_deps.cmake")
endif()
FetchContent_MakeAvailable(lus lus_imgui lus_threadpool lus_prism lus_monocypher lus_stb)
# prism replaces CMAKE_CXX_FLAGS in its own CMakeLists, so it gets the port's pointer width here.
if(NOT ANDROID)
    target_compile_options(prism PRIVATE -m32 -msse2 -mfpmath=sse)
elseif(CMAKE_SYSTEM_PROCESSOR MATCHES "^(x86|i[3-6]86)")
    target_compile_options(prism PRIVATE -msse2 -mfpmath=sse)
endif()
execute_process(COMMAND git -C "${lus_imgui_SOURCE_DIR}" apply --reverse --check
        "${lus_SOURCE_DIR}/cmake/dependencies/patches/imgui-fixes-and-config.patch"
    RESULT_VARIABLE fast3d_imgui_patched OUTPUT_QUIET ERROR_QUIET)
if(NOT fast3d_imgui_patched EQUAL 0)
    execute_process(COMMAND git -C "${lus_imgui_SOURCE_DIR}" apply
            "${lus_SOURCE_DIR}/cmake/dependencies/patches/imgui-fixes-and-config.patch"
        COMMAND_ERROR_IS_FATAL ANY)
endif()

find_package(Threads REQUIRED)
if(WIN32)
    find_package(OpenGL REQUIRED)
elseif(ANDROID)
    # The NDK has no system packages: the four libraries LUS uses are built from pinned sources, and the GL backend
    # is OpenGL ES 3 (LUS USE_OPENGLES; <GLES3/gl3.h>, libGLESv3).
    n64c_dep(nlohmann_json https://github.com/nlohmann/json.git v3.11.3 GIT_SHALLOW TRUE)
    n64c_dep(tinyxml2 https://github.com/leethomason/tinyxml2.git 10.0.0 GIT_SHALLOW TRUE)
    n64c_dep(spdlog https://github.com/gabime/spdlog.git v1.14.1 GIT_SHALLOW TRUE)
    n64c_dep(libzip https://github.com/nih-at/libzip.git v1.10.1 GIT_SHALLOW TRUE)
    set(JSON_BuildTests OFF CACHE BOOL "" FORCE)
    set(JSON_Install OFF CACHE BOOL "" FORCE)
    set(tinyxml2_BUILD_TESTING OFF CACHE BOOL "" FORCE)
    set(SPDLOG_BUILD_EXAMPLE OFF CACHE BOOL "" FORCE)
    set(SPDLOG_BUILD_TESTS OFF CACHE BOOL "" FORCE)
    set(SPDLOG_INSTALL OFF CACHE BOOL "" FORCE)
    foreach(option BUILD_TOOLS BUILD_REGRESS BUILD_EXAMPLES BUILD_DOC BUILD_SHARED_LIBS ENABLE_COMMONCRYPTO
            ENABLE_GNUTLS ENABLE_MBEDTLS ENABLE_OPENSSL ENABLE_WINDOWS_CRYPTO ENABLE_BZIP2 ENABLE_LZMA ENABLE_ZSTD)
        set(${option} OFF CACHE BOOL "" FORCE)
    endforeach()
    set(CMAKE_POLICY_VERSION_MINIMUM 3.5)
    FetchContent_MakeAvailable(nlohmann_json tinyxml2 spdlog libzip)
    if(NOT TARGET tinyxml2::tinyxml2)
        add_library(tinyxml2::tinyxml2 ALIAS tinyxml2)
    endif()
    if(NOT TARGET libzip::zip)
        add_library(libzip::zip ALIAS zip)
    endif()
    add_library(OpenGL::GL INTERFACE IMPORTED)
    target_link_libraries(OpenGL::GL INTERFACE GLESv3)
else()
    find_package(OpenGL REQUIRED)
    find_library(FAST3D_EGL_LIBRARY EGL REQUIRED) # n64c_f3d picks the software EGL device
    find_package(nlohmann_json REQUIRED)
    find_package(tinyxml2 REQUIRED)
    find_package(spdlog REQUIRED)
    find_package(libzip REQUIRED)
endif()

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
set(fast3d_excluded
    "/fast/Fast3d(Window|Gui)\\.cpp|/fast/backends/gfx_(direct3d|dxgi|metal|sdl)|/fast/resource/factory/|/fast/resource/type/(DisplayList|Vertex|Matrix)\\.cpp|/ship/(scripting|security|port)/|WasapiAudioPlayer|CoreAudioAudioPlayer|/ship/utils/Apple")
if(WIN32)
    # The ship audio component names its WASAPI player on Windows; it builds with mingw-w64's COM headers.
    string(REPLACE "|WasapiAudioPlayer" "" fast3d_excluded "${fast3d_excluded}")
endif()
list(FILTER fast3d_sources EXCLUDE REGEX "${fast3d_excluded}")
# SegAddr marks segmented addresses with bit zero; small even host pointers are
# valid. The upstream magnitude/module heuristic drops ordinary Win32 heap views.
file(READ "${lus_src}/fast/interpreter.cpp" fast3d_interpreter)
set(texture_guard_begin "    // If the resolved address is still in the N64 segmented range")
set(texture_guard_end "    gfx->GfxDpSetTextureImage(C0(21, 3)")
string(FIND "${fast3d_interpreter}" "${texture_guard_begin}" guard_begin)
string(FIND "${fast3d_interpreter}" "${texture_guard_end}" guard_end)
if(guard_begin LESS 0 OR guard_end LESS guard_begin)
    message(FATAL_ERROR "Fast3D texture address contract changed; review the pinned source")
endif()
math(EXPR guard_length "${guard_end} - ${guard_begin}")
string(SUBSTRING "${fast3d_interpreter}" ${guard_begin} ${guard_length} old_guard)
string(REPLACE "${old_guard}" "" fast3d_interpreter "${fast3d_interpreter}")
set(texture_resolve "    uintptr_t i = (uintptr_t)gfx->SegAddr(cmd->words.w1);")
string(FIND "${fast3d_interpreter}" "${texture_resolve}" texture_resolve_at)
if(texture_resolve_at LESS 0)
    message(FATAL_ERROR "Fast3D texture resolver changed; review the pinned source")
endif()
string(REPLACE "${texture_resolve}" "${texture_resolve}
    if (!N64c::TextureAddressValid(cmd->words.w1, i)) return false;"
    fast3d_interpreter "${fast3d_interpreter}")
# A viewport or scissor set inside a framebuffer takes that framebuffer's row order (f3d_area.h).
set(fb_area_y "        area->y = mActiveFrameBuffer->second.orig_height - area->y;")
string(FIND "${fast3d_interpreter}" "${fb_area_y}" fb_area_y_at)
if(fb_area_y_at LESS 0)
    message(FATAL_ERROR "Fast3D framebuffer viewport/scissor contract changed; review the pinned source")
endif()
string(REPLACE "${fb_area_y}" "        area->y = N64c::FramebufferAreaY(area->y, area->height, mActiveFrameBuffer->second.orig_height,
                                         mRapi->GetClipParameters().invertY);"
    fast3d_interpreter "${fast3d_interpreter}")
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/fast3d-generated/interpreter.cpp.in"
    "#include \"${fast3d_dir}/f3d_address.h\"\n#include \"${fast3d_dir}/f3d_area.h\"\n${fast3d_interpreter}")
configure_file("${CMAKE_CURRENT_BINARY_DIR}/fast3d-generated/interpreter.cpp.in"
    "${CMAKE_CURRENT_BINARY_DIR}/fast3d-generated/interpreter.cpp" COPYONLY)
list(REMOVE_ITEM fast3d_sources "${lus_src}/fast/interpreter.cpp")
list(APPEND fast3d_sources "${CMAKE_CURRENT_BINARY_DIR}/fast3d-generated/interpreter.cpp")
configure_file("${lus_src}/ship/install_config.h.in" "${CMAKE_CURRENT_BINARY_DIR}/fast3d-config/ship/install_config.h" @ONLY)
include("${lus_SOURCE_DIR}/cmake/cvars.cmake")
add_library(rage_fast3d STATIC ${fast3d_sources})
target_include_directories(rage_fast3d BEFORE PUBLIC "${fast3d_dir}/include" "${fast3d_guard}")
if(WIN32)
    # The crash handler includes <DbgHelp.h> (mingw-w64's header is lower case); the forwarding header is on this path only.
    target_include_directories(rage_fast3d BEFORE PRIVATE "${N64C_SHIM_DIR}/cmake/win/include")
    target_link_libraries(rage_fast3d PUBLIC dbghelp ole32 oleaut32 uuid ksuser)
endif()
# Third-party headers: consumers compile with their own warnings, not LUS's.
target_include_directories(rage_fast3d SYSTEM PUBLIC
    "${lus_SOURCE_DIR}/include" "${lus_SOURCE_DIR}/include/libultraship" "${lus_src}"
    "${CMAKE_CURRENT_BINARY_DIR}/fast3d-config" "${lus_threadpool_SOURCE_DIR}/include")
if(ANDROID)
    target_compile_definitions(rage_fast3d PUBLIC USE_OPENGLES)
    set_source_files_properties("${lus_src}/fast/backends/gfx_opengl.cpp" PROPERTIES
        COMPILE_OPTIONS "-include;${fast3d_dir}/f3d_gles_rgba.h")
endif()
target_compile_definitions(rage_fast3d PUBLIC ENABLE_OPENGL F3DEX_GBI_2
    LUS_VERSION_EPOCH=2 LUS_VERSION_MAJOR=0 LUS_VERSION_MINOR=0 LUS_VERSION_PATCH=0)
target_compile_options(rage_fast3d PRIVATE -w)
target_link_libraries(rage_fast3d PUBLIC fast3d_imgui prism fast3d_monocypher nlohmann_json::nlohmann_json
    tinyxml2::tinyxml2 spdlog::spdlog SDL3::SDL3-static OpenGL::GL Threads::Threads
    PRIVATE fast3d_stb libzip::zip)
if(WIN32)
    # The renderer loads OpenGL 3 entry points through GLEW on Windows (opengl32.dll exports only GL 1.1).
    target_link_libraries(rage_fast3d PUBLIC fast3d_glew)
endif()

# Replay: a captured display list through the port's DL boundary into Fast3D, gated by the same
# SSIM check as the hand-written renderer (tests/replay_ssim.py).
# The OpenGL ES vertex stage scales z with a compound assignment to a swizzle of gl_Position (gl_Position.z *= 0.3f); the
# Android emulator's GLES translator drops every triangle that shader draws, so the frame stays black. The same scale
# written as a plain assignment draws.
file(READ "${lus_src}/fast/shaders/opengl/default.shader.glsl" fast3d_shader)
string(REPLACE "gl_Position.z *= 0.3f;" "gl_Position.z = aVtxPos.z * 0.3;" fast3d_shader_fixed "${fast3d_shader}")
if(fast3d_shader_fixed STREQUAL fast3d_shader)
    message(FATAL_ERROR "Fast3D vertex shader contract changed; review the pinned source")
endif()
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/fast3d-generated/shaders/opengl/default.shader.glsl" "${fast3d_shader_fixed}")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${lus_src}/fast/shaders/opengl/default.shader.glsl")
add_custom_command(OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/fast3d-shaders.o2r"
    COMMAND ${CMAKE_COMMAND} -E tar cf "${CMAKE_CURRENT_BINARY_DIR}/fast3d-shaders.o2r" --format=zip
        shaders/opengl/default.shader.glsl
    WORKING_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/fast3d-generated"
    DEPENDS "${CMAKE_CURRENT_BINARY_DIR}/fast3d-generated/shaders/opengl/default.shader.glsl"
    VERBATIM)
add_library(n64c_fast3d STATIC "${fast3d_dir}/f3d_window.cpp" "${fast3d_dir}/f3d_controls_overlay.cpp" "${fast3d_dir}/n64c_f3d.cpp")
target_include_directories(n64c_fast3d PUBLIC "${fast3d_dir}")
target_link_libraries(n64c_fast3d PUBLIC rage_fast3d n64compat)
if(FAST3D_EGL_LIBRARY)
    target_link_libraries(n64c_fast3d PRIVATE ${FAST3D_EGL_LIBRARY})
else()
    target_compile_definitions(n64c_fast3d PRIVATE N64C_F3D_NO_EGL)
endif()
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

add_executable(n64c_test_f3d_address "${fast3d_dir}/../tests/test_f3d_address.cpp")
target_compile_options(n64c_test_f3d_address PRIVATE -Wall -Wextra -Werror)
add_test(NAME n64c_f3d_address COMMAND n64c_test_f3d_address)

add_executable(n64c_test_f3d_area "${fast3d_dir}/../tests/test_f3d_area.cpp")
target_compile_options(n64c_test_f3d_area PRIVATE -Wall -Wextra -Werror)
add_test(NAME n64c_f3d_area COMMAND n64c_test_f3d_area)
