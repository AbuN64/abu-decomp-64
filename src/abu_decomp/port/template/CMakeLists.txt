cmake_minimum_required(VERSION 3.28)
project({{cmake_project}} VERSION 0.1.0 LANGUAGES C)
include(CTest)
include(cmake/warnings.cmake)
option(PORT_SANITIZERS "Enable address and undefined behavior sanitizers" OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

add_library(port_options INTERFACE)
target_compile_features(port_options INTERFACE c_std_23)
if(PORT_SANITIZERS AND NOT MSVC)
    target_compile_options(port_options INTERFACE -fsanitize=address,undefined -fno-omit-frame-pointer)
    target_link_options(port_options INTERFACE -fsanitize=address,undefined)
endif()

# Platform boundary: the only library that may include OS or device headers.
add_library(port_platform STATIC src/platform/sys_core.c)
# Boot chain: original owners in boot order, each native or an explicit stop.
add_library(port_boot STATIC src/boot/boot.c)
foreach(target port_platform port_boot)
    target_include_directories(${target} PUBLIC src)
    target_link_libraries(${target} PUBLIC port_options)
    port_warnings(${target})
endforeach()
target_link_libraries(port_boot PUBLIC port_platform)

add_executable({{executable}} src/main.c)
target_link_libraries({{executable}} PRIVATE port_boot)
port_warnings({{executable}})

if(BUILD_TESTING)
    add_executable(test_boot tests/test_boot.c)
    target_link_libraries(test_boot PRIVATE port_boot)
    port_warnings(test_boot)
    add_test(NAME boot COMMAND test_boot)
    # The boot gate runs the real executable and requires every line of tests/boot_gate.txt in order.
    add_test(NAME boot_gate COMMAND ${CMAKE_COMMAND} -DEXECUTABLE=$<TARGET_FILE:{{executable}}>
        -DGATE=${CMAKE_CURRENT_SOURCE_DIR}/tests/boot_gate.txt -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake/boot_gate.cmake)
endif()
