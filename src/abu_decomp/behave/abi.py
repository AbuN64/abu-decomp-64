"""Callees whose register convention is not C's: hand-written routines (the disassembler's `Handwritten function`
mark, or arguments taken from callee-saved registers s0-s7/fp). A C caller cannot pass those, so the call is an
opaque boundary for the comparison: what the routine did to memory and what it returned still count, the call
event itself does not."""

from __future__ import annotations

import re
from pathlib import Path

SAVED = frozenset({f"$s{i}" for i in range(9)} | {"$fp"})
_INSN = re.compile(r"^\s*/\*.*?\*/\s+(\w[\w.]*)\s*(.*?)\s*(?:#.*)?$")
_STORES = frozenset({"sb", "sh", "sw", "sd", "swl", "swr", "swc1", "sdc1", "swc2", "sdc2", "sc", "sdl", "sdr"})
_READ_ONLY = frozenset(
    {"beq", "bne", "beql", "bnel", "blez", "bgtz", "bltz", "bgez", "blezl", "bgtzl", "bltzl", "bgezl"}
    | {"jr", "jalr", "j", "jal", "mtc1", "mthi", "mtlo", "mult", "multu", "div", "divu", "dmult", "dmultu"}
    | {"ddiv", "ddivu"}
)
_MARK = "Handwritten function"
_REGS = re.compile(r"\$\w+")


def reads_saved_first(text: str) -> bool:
    """Whether the routine reads a callee-saved register before it writes it (a prologue save to the stack is
    neither): its arguments arrive in that register."""
    written: set[str] = set()
    for line in text.splitlines():
        match = _INSN.match(line)
        if not match:
            continue
        op, operands = match[1], match[2]
        regs = _REGS.findall(operands)
        if op in _STORES and "($sp)" in operands:
            continue
        if op in _STORES or op in _READ_ONLY:
            sources, dests = regs, []
        else:
            dests, sources = regs[:1], regs[1:]
        if any(r in SAVED and r not in written for r in sources):
            return True
        written.update(r for r in dests if r in SAVED)
    return False


def _hand_written(text: str) -> bool:
    return _MARK in text or reads_saved_first(text)


def opaque(project: Path, version: str, names: set[str]) -> set[str]:
    """The names, among `names`, whose assembly in the project is hand-written with the register convention above."""
    home = project / "asm" / version
    found: set[str] = set()
    for name in sorted(names):
        path = next(iter(sorted(home.rglob(f"{name}.s"))), None) if home.is_dir() else None
        if path is not None and _hand_written(path.read_text(errors="replace")):
            found.add(name)
    return found


_SP_ADJUST = re.compile(r"^\$sp,\s*\$sp,\s*(-?(?:0x[0-9A-Fa-f]+|\d+))$")


def enters_mid_frame(text: str) -> int | None:
    """The bytes of stack a routine releases that it never took, or none. A routine whose only `$sp` writes are
    `addiu $sp, $sp, N` releases (N > 0) without ever allocating (N < 0) was given an entry address after its prologue:
    entered there it reads every stack slot (the fifth argument at sp+16 among them) at the wrong offset, and the real
    callers never reach that address."""
    taken = released = 0
    for line in text.splitlines():
        match = _INSN.match(line)
        if not match or not match[2].startswith("$sp,"):
            continue
        adjust = _SP_ADJUST.match(match[2]) if match[1] == "addiu" else None
        if adjust is None:
            if match[1] not in _STORES:
                return None  # sp is set some other way: no judgement
            continue
        amount = int(adjust[1], 0)
        if amount < 0:
            taken -= amount
        else:
            released += amount
    return released if released > taken else None


def entry_defect(project: Path, version: str, name: str) -> str | None:
    """Why `name` cannot be entered at its symbol address (see enters_mid_frame), from the project's assembly."""
    home = project / "asm" / version
    path = next(iter(sorted(home.rglob(f"{name}.s"))), None) if home.is_dir() else None
    released = enters_mid_frame(path.read_text(errors="replace")) if path is not None else None
    if released is None:
        return None
    return (
        f"{name} releases {released} bytes of stack it never allocates: its symbol address in {version} lies after "
        f"the prologue (the allocation is in the word(s) just before it), so no run can enter it there; "
        f"correct the symbol address, then behave it"
    )
