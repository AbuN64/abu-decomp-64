# SPDX-License-Identifier: GPL-3.0-or-later
"""The one definition of a project's source and header files for the shim: suffixes match case-blind, so
RageWars' `.CPP` units, its 87 `.c` units and `.H`/`.h` headers are all seen. Never hard-code `*.CPP`."""
SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx")
HEADER_SUFFIXES = (".h", ".hh", ".hpp", ".hxx")


def is_source(path):
    return str(path).lower().endswith(SOURCE_SUFFIXES)


def is_header(path):
    return str(path).lower().endswith(HEADER_SUFFIXES)


def sources(source, folder="src"):
    """Every source file under source/folder (libgcc excluded: the cmake glob does not build it), sorted."""
    return sorted(p for p in (source / folder).rglob("*") if p.is_file() and is_source(p) and "/libgcc/" not in str(p))


def headers(source, folder="include"):
    return sorted(p for p in (source / folder).rglob("*") if p.is_file() and is_header(p))
