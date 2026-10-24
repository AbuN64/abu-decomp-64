# SPDX-License-Identifier: GPL-3.0-or-later
"""The target's object-file facts the boot tools need, from the environment the build sets (cmake/shim.cmake).

Linux/i386 needs none (every default is the host's). Windows/i386 (mingw-w64) mangles every C name with a leading
underscore, so a name read from nm or an object's symbol table loses it (strip) and a name written into a linker
script or an assembler line gains it (add); the binutils of the target do the archive and symbol work.
  N64C_SYMBOL_PREFIX  "_" on Windows/i386, empty on Linux
  N64C_NM, N64C_AR, N64C_RANLIB, N64C_CC   the target's tools (defaults nm, ar, ranlib, gcc)"""
import os

PREFIX = os.environ.get("N64C_SYMBOL_PREFIX", "")
NM = os.environ.get("N64C_NM", "nm")
AR = os.environ.get("N64C_AR", "ar")
RANLIB = os.environ.get("N64C_RANLIB", "ranlib")
CC = os.environ.get("N64C_CC", "gcc")
# COFF has no ELF sections: the assembler's section directive for read-only data differs.
COFF = bool(PREFIX)


def strip(name):
    """C name of an object's symbol."""
    return name[len(PREFIX):] if PREFIX and name.startswith(PREFIX) else name


def add(name):
    """Object-file name of a C name (for linker scripts and assembler text)."""
    return PREFIX + name
