# SPDX-License-Identifier: GPL-3.0-or-later
# Windows (mingw-w64) builds of the Fast3D renderer's C++ libraries, from pinned sources through n64c_dep (shared under
# N64C_DEPS_DIR like every other dependency). All MIT/zlib-style licences; recorded in the port's SOURCES.md.
#   nlohmann_json 3.11.3 (MIT), tinyxml2 11.0.0 (zlib), spdlog 1.15.2 (MIT, header-only, bundled fmt),
#   zlib 1.3.1 (zlib), libzip 1.11.3 (BSD-3-Clause, zlib only: no crypto, bzip2, lzma or zstd).
n64c_dep(win_zlib https://github.com/madler/zlib.git v1.3.1
    GIT_SHALLOW TRUE
    SOURCE_SUBDIR fetch-only)
n64c_dep(win_nlohmann_json https://github.com/nlohmann/json.git v3.11.3
    GIT_SHALLOW TRUE
    SOURCE_SUBDIR fetch-only)
n64c_dep(win_tinyxml2 https://github.com/leethomason/tinyxml2.git 11.0.0
    GIT_SHALLOW TRUE
    SOURCE_SUBDIR fetch-only)
n64c_dep(win_spdlog https://github.com/gabime/spdlog.git v1.15.2
    GIT_SHALLOW TRUE
    SOURCE_SUBDIR fetch-only)
n64c_dep(win_libzip https://github.com/nih-at/libzip.git v1.11.3
    GIT_SHALLOW TRUE)
# GLEW 2.2.0 (Modified BSD, MIT, Khronos): the release tarball carries the generated sources.
FetchContent_Declare(win_glew
    URL https://github.com/nigels-com/glew/releases/download/glew-2.2.0/glew-2.2.0.tgz
    URL_HASH SHA256=d4fc82893cfb00109578d0a1a2337fb8ca335b3ceccf97b97e5cc7f08e4353e1
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE
    SOURCE_SUBDIR fetch-only)
FetchContent_MakeAvailable(win_glew win_zlib win_nlohmann_json win_tinyxml2 win_spdlog)

# GLEW: one source file, linked statically.
add_library(fast3d_glew STATIC "${win_glew_SOURCE_DIR}/src/glew.c")
target_include_directories(fast3d_glew SYSTEM PUBLIC "${win_glew_SOURCE_DIR}/include")
target_compile_definitions(fast3d_glew PUBLIC GLEW_STATIC)
target_compile_options(fast3d_glew PRIVATE -w)
target_link_libraries(fast3d_glew PUBLIC opengl32)

# nlohmann_json: header-only.
add_library(nlohmann_json INTERFACE)
add_library(nlohmann_json::nlohmann_json ALIAS nlohmann_json)
target_include_directories(nlohmann_json SYSTEM INTERFACE "${win_nlohmann_json_SOURCE_DIR}/include")

# tinyxml2: one source file.
add_library(tinyxml2 STATIC "${win_tinyxml2_SOURCE_DIR}/tinyxml2.cpp")
add_library(tinyxml2::tinyxml2 ALIAS tinyxml2)
target_include_directories(tinyxml2 SYSTEM PUBLIC "${win_tinyxml2_SOURCE_DIR}")

# spdlog: header-only with its bundled fmt.
add_library(spdlog INTERFACE)
add_library(spdlog::spdlog ALIAS spdlog)
target_include_directories(spdlog SYSTEM INTERFACE "${win_spdlog_SOURCE_DIR}/include")
target_compile_definitions(spdlog INTERFACE SPDLOG_HEADER_ONLY)

# zlib: the sources, as a static library (no install, no shared build).
file(GLOB win_zlib_sources "${win_zlib_SOURCE_DIR}/*.c")
list(FILTER win_zlib_sources EXCLUDE REGEX "/(gzclose|gzlib|gzread|gzwrite)\\.c$")
add_library(rage_win_zlib STATIC ${win_zlib_sources}
    "${win_zlib_SOURCE_DIR}/gzclose.c" "${win_zlib_SOURCE_DIR}/gzlib.c" "${win_zlib_SOURCE_DIR}/gzread.c"
    "${win_zlib_SOURCE_DIR}/gzwrite.c")
target_include_directories(rage_win_zlib SYSTEM PUBLIC "${win_zlib_SOURCE_DIR}")
target_compile_definitions(rage_win_zlib PRIVATE _LARGEFILE64_SOURCE=1)
target_compile_options(rage_win_zlib PRIVATE -w)

# libzip: its own build, zlib the only codec, static, nothing installed. ZLIB comes from rage_win_zlib above through a
# Find module (cmake/win/FindZLIB.cmake) placed ahead of CMake's own.
set(ENABLE_COMMONCRYPTO OFF CACHE BOOL "" FORCE)
set(ENABLE_GNUTLS OFF CACHE BOOL "" FORCE)
set(ENABLE_MBEDTLS OFF CACHE BOOL "" FORCE)
set(ENABLE_OPENSSL OFF CACHE BOOL "" FORCE)
set(ENABLE_WINDOWS_CRYPTO OFF CACHE BOOL "" FORCE)
set(ENABLE_BZIP2 OFF CACHE BOOL "" FORCE)
set(ENABLE_LZMA OFF CACHE BOOL "" FORCE)
set(ENABLE_ZSTD OFF CACHE BOOL "" FORCE)
set(BUILD_TOOLS OFF CACHE BOOL "" FORCE)
set(BUILD_REGRESS OFF CACHE BOOL "" FORCE)
set(BUILD_OSSFUZZ OFF CACHE BOOL "" FORCE)
set(BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(BUILD_DOC OFF CACHE BOOL "" FORCE)
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(LIBZIP_DO_INSTALL OFF CACHE BOOL "" FORCE)
list(PREPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_LIST_DIR}/win")
FetchContent_MakeAvailable(win_libzip)
if(NOT TARGET libzip::zip)
    add_library(libzip::zip ALIAS zip)
endif()
