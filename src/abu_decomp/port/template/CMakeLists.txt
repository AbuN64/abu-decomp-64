cmake_minimum_required(VERSION 3.28)
project({{cmake_project}} VERSION 0.1.0 LANGUAGES C)
include(CTest)
include(cmake/shim.cmake)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
# The shim and the original are 32-bit.
option(N64C_M32 "Build the project and the shim with -m32" ON)
if(N64C_M32)
    set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -m32")
endif()
include("${N64C_SHIM_DIR}/cmake/n64compat_audio.cmake")
include("${N64C_SHIM_DIR}/cmake/n64compat.cmake")
include("${N64C_SHIM_DIR}/cmake/n64lint.cmake")

# n64lint: N64 names stay inside the shim and platform/port; the ledger there only shrinks.
file(GLOB_RECURSE lint_inputs CONFIGURE_DEPENDS RELATIVE "${CMAKE_CURRENT_SOURCE_DIR}" src/*.c src/*.h)
list(JOIN lint_inputs "\n" lint_list)
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/n64lint_files.txt" "${lint_list}\n")
set(n64lint_args --root "${CMAKE_CURRENT_SOURCE_DIR}" --ledger platform/port
    --files-from "${CMAKE_CURRENT_BINARY_DIR}/n64lint_files.txt")
add_custom_target(lint_n64 COMMAND n64lint ${n64lint_args} DEPENDS n64lint COMMENT "n64lint" VERBATIM)

if(BUILD_TESTING)
    add_test(NAME n64lint COMMAND n64lint ${n64lint_args})
    add_executable(test_shim_link platform/port/test_shim_link.c)
    target_link_libraries(test_shim_link PRIVATE n64compat)
    target_compile_options(test_shim_link PRIVATE -Wall -Wextra -Werror)
    add_test(NAME shim_link COMMAND test_shim_link)
endif()
