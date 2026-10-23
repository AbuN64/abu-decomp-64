# SPDX-License-Identifier: GPL-3.0-or-later
# Shared dependency sources of the shim and its consumers (a consumer may define n64c_dep itself). Without N64C_DEPS_DIR every build tree fetches its own copy (FetchContent default).
# With it, each dependency lives once at <N64C_DEPS_DIR>/<name>-<pin>, cloned on first use and then only read, so a
# fresh build tree configures without any clone. The key is the pin, so another pin of the same dependency gets
# its own directory. Build outputs stay in each build tree (compiles are shared through the compiler launcher).
set(N64C_DEPS_DIR "" CACHE PATH "Directory of shared, read-only dependency sources keyed by pin (empty: fetch per build tree)")
find_package(Git QUIET)

# n64c_dep(<name> <repository> <pin> [FetchContent_Declare options...])
function(n64c_dep name repo pin)
    FetchContent_Declare(${name} GIT_REPOSITORY ${repo} GIT_TAG ${pin} ${ARGN})
    if(NOT N64C_DEPS_DIR)
        return()
    endif()
    if(NOT GIT_FOUND)
        message(FATAL_ERROR "N64C_DEPS_DIR needs git")
    endif()
    string(REGEX REPLACE "[^A-Za-z0-9._-]" "_" key "${name}-${pin}")
    set(dir "${N64C_DEPS_DIR}/${key}")
    if(NOT IS_DIRECTORY "${dir}")
        # Clone beside the target and rename: a concurrent configure sees either nothing or a finished tree.
        string(RANDOM LENGTH 8 suffix)
        set(tmp "${N64C_DEPS_DIR}/.${key}.${suffix}")
        file(MAKE_DIRECTORY "${N64C_DEPS_DIR}")
        execute_process(COMMAND "${GIT_EXECUTABLE}" clone --quiet "${repo}" "${tmp}"
            COMMAND_ERROR_IS_FATAL ANY)
        execute_process(COMMAND "${GIT_EXECUTABLE}" -C "${tmp}" -c advice.detachedHead=false checkout --quiet "${pin}"
            COMMAND_ERROR_IS_FATAL ANY)
        file(RENAME "${tmp}" "${dir}")
        if(NOT IS_DIRECTORY "${dir}")
            message(FATAL_ERROR "could not publish ${dir}")
        endif()
    endif()
    string(TOUPPER "${name}" upper)
    set(FETCHCONTENT_SOURCE_DIR_${upper} "${dir}" PARENT_SCOPE)
endfunction()
