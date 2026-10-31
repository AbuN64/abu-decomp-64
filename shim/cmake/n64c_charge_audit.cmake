# SPDX-License-Identifier: GPL-3.0-or-later
# Audit of a compiled original unit (CONTRACTS.md "Work scale"): the object must call the compiler's entry hook
# (__cyg_profile_func_enter), which only an object compiled with N64C_CHARGE_COMPILE_FLAGS does.
#   cmake -DOBJECT=<unit.o> [-DNM=<nm>] -P n64c_charge_audit.cmake
if(NOT OBJECT OR NOT EXISTS "${OBJECT}")
    message(FATAL_ERROR "n64c_charge_audit: OBJECT must name an existing object, got '${OBJECT}'")
endif()
if(NOT NM)
    set(NM nm)
endif()
execute_process(COMMAND "${NM}" "${OBJECT}" RESULT_VARIABLE status OUTPUT_VARIABLE symbols ERROR_VARIABLE err)
if(NOT status EQUAL 0)
    message(FATAL_ERROR "n64c_charge_audit: ${NM} ${OBJECT} failed: ${err}")
endif()
if(NOT symbols MATCHES "(^|\n)[ 0-9a-fA-F]* U __cyg_profile_func_enter(\n|$)")
    message(FATAL_ERROR "n64c_charge_audit: ${OBJECT} carries no __cyg_profile_func_enter call: it was compiled without the entry charge")
endif()
message(STATUS "n64c_charge_audit: ${OBJECT} calls the entry hook")
