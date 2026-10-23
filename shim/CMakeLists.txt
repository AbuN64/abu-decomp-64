# SPDX-License-Identifier: GPL-3.0-or-later
# Standalone build of the shim and its unit tests (what abu-decomp-64's ci/check runs). A port includes the
# modules in cmake/ instead of this file.
cmake_minimum_required(VERSION 3.28)
project(n64shim LANGUAGES C)
include(CTest)
option(N64C_M32 "Build the shim and its tests with -m32" ON)
if(N64C_M32)
    set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -m32")
endif()
include(cmake/n64compat_audio.cmake)
include(cmake/n64compat.cmake)
include(cmake/n64lint.cmake)
