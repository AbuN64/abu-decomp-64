"""Which object defines a symbol: one read of each object's ELF symbol table, no process (`nm` per object, or `fncmp` per
object and function, cost ~340 s per function over 2756 units, 10-27)."""

from __future__ import annotations

import struct
from pathlib import Path

_SHT_SYMTAB = 2
_STT_SECTION, _STT_FILE = 3, 4


def defined(path: Path) -> set[str]:
    """Names of the symbols the ELF32 object at PATH defines; empty when it is no ELF32 object or cannot be read."""
    try:
        data = path.read_bytes()
    except OSError:
        return set()
    if data[:4] != b"\x7fELF" or len(data) < 0x34 or data[4] != 1 or data[5] not in (1, 2):
        return set()
    e = "<" if data[5] == 1 else ">"
    try:
        (shoff,) = struct.unpack_from(e + "I", data, 0x20)
        shentsize, shnum = struct.unpack_from(e + "HH", data, 0x2E)
        sections = [struct.unpack_from(e + "10I", data, shoff + i * shentsize) for i in range(shnum)]
        names: set[str] = set()
        for _name, kind, _flags, _addr, offset, size, link, _info, _align, entsize in sections:
            if kind != _SHT_SYMTAB or not entsize:
                continue
            strtab = sections[link]
            text = data[strtab[4] : strtab[4] + strtab[5]]
            for at in range(offset, offset + size, entsize):
                st_name, _value, _size, info, _other, shndx = struct.unpack_from(e + "IIIBBH", data, at)
                if shndx and info & 0xF not in (_STT_SECTION, _STT_FILE) and st_name:
                    names.add(text[st_name : text.index(b"\0", st_name)].decode(errors="replace"))
        return names
    except (struct.error, IndexError, ValueError):
        return set()


def index(tree: Path, objects: dict[str, str]) -> dict[str, list[str]]:
    """{symbol: [unit, ...]} over OBJECTS ({unit: object path under TREE}); an object that is not there adds nothing."""
    found: dict[str, list[str]] = {}
    for unit, obj in objects.items():
        for name in defined(tree / obj):
            found.setdefault(name, []).append(unit)
    return found
