"""The read-only words of a version: literal pools and constant tables.

A generated case varies the words the original reads from memory. A float literal or a constant table is not state:
it is whatever the ROM holds, and a candidate that carries its own copy of the pool (an exact object, a draft with
float literals) reads that copy while the original reads the ROM address, so varying the original's side made every
stored constant differ (REV1-TOP-O2B: 49 false differs on a byte-identical exact). These words are the ROM's, in every
case. The read-only extents come from the version's splat description (its `rodata` subsegments), the bytes from the
linked ELF (the linker script merges read-only input into writable output sections, so section flags say nothing).
"""

from __future__ import annotations

import struct
from pathlib import Path

import yaml

SHF_EXEC, SHF_ALLOC = 4, 2
SHT_PROGBITS = 1


def read_only_ranges(project: Path, version: str) -> tuple[tuple[int, int], ...]:
    """(start, end) vram of each `rodata` subsegment in versions/<version>/splat.yaml; () when it cannot be read."""
    try:
        doc = yaml.safe_load((project / "versions" / version / "splat.yaml").read_text())
        segments = [s for s in doc["segments"] if isinstance(s, dict) and "start" in s]
        found = []
        for index, seg in enumerate(segments):
            if "vram" not in seg or "subsegments" not in seg:
                continue
            start, vram = int(seg["start"]), int(seg["vram"])
            subs = [(int(row[0]), str(row[1])) for row in seg["subsegments"] if isinstance(row, list) and len(row) > 1]
            end = int(segments[index + 1]["start"]) if index + 1 < len(segments) else None
            for number, (offset, kind) in enumerate(subs):
                limit = subs[number + 1][0] if number + 1 < len(subs) else end
                if kind == "rodata" and limit is not None and limit > offset:
                    found.append((vram + offset - start, vram + limit - start))
        return tuple(found)
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError):
        return ()


def sections(elf: bytes) -> list[tuple[int, bytes]]:
    """(address, bytes) of each allocated, non-code PROGBITS section of a 32-bit big-endian ELF."""
    if len(elf) < 0x34 or elf[:4] != b"\x7fELF" or elf[4] != 1 or elf[5] != 2:
        return []
    shoff = struct.unpack(">I", elf[0x20:0x24])[0]
    shentsize, shnum = struct.unpack(">HH", elf[0x2E:0x32])
    found = []
    for index in range(shnum):
        row = shoff + index * shentsize
        if row + 24 > len(elf):
            break
        _, kind, flags, addr, offset, size = struct.unpack(">IIIIII", elf[row : row + 24])
        if kind == SHT_PROGBITS and flags & SHF_ALLOC and not flags & SHF_EXEC and size:
            found.append((addr, elf[offset : offset + size]))
    return found


def linked_elf(project: Path, version: str) -> Path:
    """The version's linked ELF under build/<version>/ (named after the project, so found by suffix)."""
    return next(iter(sorted((project / "build" / version).glob("*.elf"))), project / "build" / version / "missing.elf")


def words(elf: Path, addresses: tuple[int, ...], ranges: tuple[tuple[int, int], ...]) -> dict[int, int]:
    """Address -> big-endian word for each of `addresses` inside one of `ranges` (none when the ELF is missing: the
    words then stay swept, as before)."""
    try:
        parts = sections(elf.read_bytes())
    except OSError:
        return {}
    found = {}
    for address in addresses:
        if not any(lo <= address and address + 4 <= hi for lo, hi in ranges):
            continue
        for base, data in parts:
            if base <= address and address + 4 <= base + len(data):
                found[address] = struct.unpack(">I", data[address - base : address - base + 4])[0]
                break
    return found
