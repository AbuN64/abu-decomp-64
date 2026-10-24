"""What the assembly says about each data symbol: addresses, label bytes and every accessor (read only, no build)."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from abu_decomp.data.config import DataConfig

SYMROW = re.compile(r"^\s*(\w+)\s*=\s*0x([0-9A-Fa-f]+)\s*;")
INSN = re.compile(r"^\s*/\*\s*[0-9A-F]+\s+([0-9A-F]{8})\s+[0-9A-F]{8}\s*\*/\s+(\S+)\s*(.*)$")
LOREF = re.compile(r"%lo\((\w+)(?:\s*\+\s*(0x[0-9A-Fa-f]+|\d+))?\)")
MEMOP = re.compile(r"^(\$\w+),\s*(?:(-?0x[0-9A-Fa-f]+|-?\d+)|%lo\([^)]*\))\((\$\w+)\)$")
# op -> (width, type, L/S)
WIDTH = {
    "lb": (1, "s8", "L"), "lbu": (1, "u8", "L"), "sb": (1, "8", "S"), "lh": (2, "s16", "L"), "lhu": (2, "u16", "L"),
    "sh": (2, "16", "S"), "lw": (4, "s32", "L"), "sw": (4, "32", "S"), "lwc1": (4, "f32", "L"), "swc1": (4, "f32", "S"),
    "ld": (8, "s64", "L"), "sd": (8, "64", "S"), "ldc1": (8, "f64", "L"), "sdc1": (8, "f64", "S"),
    "lwl": (4, "s32", "L"), "lwr": (4, "s32", "L"), "swl": (4, "32", "S"), "swr": (4, "32", "S"),
}  # fmt: skip
NODEST = {
    "sb", "sh", "sw", "swc1", "sd", "sdc1", "swl", "swr", "beq", "bne", "beqz", "bnez", "b", "j", "jr", "jal", "jalr",
    "bgez", "bltz", "blez", "bgtz", "bc1t", "bc1f", "mtc1", "ctc1", "nop", "break", "syscall", "mult", "multu", "div",
    "divu", "teq", "c.eq.s", "c.lt.s", "c.le.s", "c.eq.d", "c.lt.d", "c.le.d", "beql", "bnel", "bc1tl", "bc1fl",
}  # fmt: skip
SAVED = ("$s0", "$s1", "$s2", "$s3", "$s4", "$s5", "$s6", "$s7", "$fp")


def num(s: str | None) -> int:
    return int(s, 0) if s else 0


def kindof(t: str) -> str:
    return "f" if t.startswith("f") else "i"


def load_syms(tree: Path, cfg: DataConfig, v: str) -> dict[str, int]:
    out: dict[str, int] = {}
    f = tree / cfg.path(cfg.symbols_file, v)
    if not f.is_file():
        return out
    for line in f.read_text(errors="replace").splitlines():
        m = SYMROW.match(line)
        if m and m.group(1) not in out:
            out[m.group(1)] = int(m.group(2), 16)
    return out


def load_labels(tree: Path, cfg: DataConfig, v: str) -> dict[str, tuple[str, bytes]]:
    """name -> (file, bytes) for every dlabel in the version's data .s files."""
    out: dict[str, tuple[str, bytes]] = {}
    for f in sorted((tree / cfg.path(cfg.data_asm, v)).rglob("*.s")):
        cur, buf = None, bytearray()
        rel = str(f.relative_to(tree))
        for line in f.read_text(errors="replace").splitlines():
            s = line.strip()
            if s.startswith(("dlabel ", "glabel ")):
                if cur:
                    out[cur] = (rel, bytes(buf))
                cur, buf = s.split()[1], bytearray()
            elif s.startswith(".byte") and cur:
                buf += bytes(int(x, 0) & 255 for x in s[5:].split(",") if x.strip())
        if cur:
            out[cur] = (rel, bytes(buf))
    return out


def scan_fn(text: str, acc: dict[str, list], fn: str) -> None:
    """Add one function's accessors to acc: sym -> [(fn, off, width, type, kind)]; kind L load, S store, A address,
    I indexed access through a base register."""
    base: dict[str, tuple[str, int, bool]] = {}  # reg -> (sym, off, indexed)
    for line in text.splitlines():
        if line.lstrip().startswith(".L"):
            base = {}
            continue
        m = INSN.match(line)
        if not m:
            continue
        op, args = m.group(2), m.group(3).strip()
        lo = LOREF.search(args)
        parts = [a.strip() for a in args.split(",")]
        dest = parts[0] if parts and parts[0].startswith("$") and op not in NODEST else None
        if lo and op in WIDTH:
            w, t, k = WIDTH[op]
            acc[lo.group(1)].append((fn, num(lo.group(2)), w, t, k))
        elif lo and op == "addiu":
            acc[lo.group(1)].append((fn, num(lo.group(2)), 0, "", "A"))
            if dest:
                base[dest] = (lo.group(1), num(lo.group(2)), False)
                continue
        elif op in WIDTH:
            mm = MEMOP.match(args)
            if mm and mm.group(3) in base:
                sym, off, idx = base[mm.group(3)]
                w, t, k = WIDTH[op]
                acc[sym].append((fn, off + num(mm.group(2)), w, t, "I" if idx else k))
        elif op in ("addu", "addiu") and len(parts) == 3 and not lo:
            src = [p for p in parts[1:] if p in base]
            if dest and src:
                sym, off, idx = base[src[0]]
                if op == "addiu":
                    base[dest] = (sym, off + num(parts[2]), idx)
                else:
                    other = parts[2] if src[0] == parts[1] else parts[1]
                    base[dest] = (sym, off, True if other != "$zero" else idx)
                continue
        if dest and dest in base:
            del base[dest]
        if op in ("jal", "jalr"):
            for r in [r for r in base if r not in SAVED]:
                del base[r]


def load_access(tree: Path, cfg: DataConfig, v: str) -> dict[str, list]:
    acc: dict[str, list] = defaultdict(list)
    for f in sorted((tree / cfg.path(cfg.fn_asm, v)).rglob("*.s")):
        scan_fn(f.read_text(errors="replace"), acc, f.stem)
    return acc
