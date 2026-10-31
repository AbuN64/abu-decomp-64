# SPDX-License-Identifier: GPL-3.0-or-later
# A stand-in port for n64c_charge_entries (the ctest n64c_charge_port): its effective manifest lives in manifest/, away
# from the facts directory facts/ that holds sched_glue.c. CHARGE_PORT_VARIANT: "default" the full call, "no_port_dir"
# the call without PORT_DIR, "no_glue" a PORT_DIR whose sched_glue.c is absent.
cmake_minimum_required(VERSION 3.20)
project(charge_port C)
include("${N64C_SHIM_DIR}/cmake/n64c_charge.cmake")
add_library(charge_port STATIC original.c)
set(port_dir "${CMAKE_CURRENT_LIST_DIR}/facts")
if(CHARGE_PORT_VARIANT STREQUAL "no_glue")
    set(port_dir "${CMAKE_CURRENT_LIST_DIR}/nowhere")
endif()
set(call NS_PER_ENTRY 4702.010109321735 MANIFEST "${CMAKE_CURRENT_LIST_DIR}/manifest/us.effective.json"
    SOURCE "${CMAKE_CURRENT_LIST_DIR}/source" ORIGINAL_SOURCES original.c)
if(NOT CHARGE_PORT_VARIANT STREQUAL "no_port_dir")
    list(APPEND call PORT_DIR "${port_dir}")
endif()
n64c_charge_entries(charge_port ${call})
