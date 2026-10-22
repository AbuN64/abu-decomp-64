"""A host fault names its function: the faulting PC resolved against the port binary the run used.

The gdb bridge names a native fault `host-fault-<pc>` but its stacks only show the boot loop after the fault. This
module reads the port's own symbol table (`nm -n`, cached by path and mtime), turns the PC into `function+offset`,
and overlays the tas stop classification with that frame's source site, arity note, evidence level and one `next`.
A game function (func_* or a name defined in the project) outranks port/shim frames, and both are shown.
"""

from __future__ import annotations

import bisect
import re
import subprocess
from pathlib import Path
from typing import Any

from abu_decomp import arity, evidence, tas_triage

_FAULT = re.compile(r"host-fault-([0-9A-Fa-f]+)$")
_GAME = re.compile(r"func_[0-9A-Fa-f]{8}\w*")
_CACHE: dict[tuple[str, int], tuple[list[int], list[str]]] = {}


def symbols(port: Path | str) -> tuple[list[int], list[str]]:
    """The port's text symbols as sorted (addresses, names); empty when nm cannot read it."""
    path = Path(port)
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return [], []
    if key not in _CACHE:
        try:
            out = subprocess.run(
                ["nm", "-n", "--defined-only", str(path)], capture_output=True, text=True, timeout=60, check=False
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        rows = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[1] in "tTwW":
                rows.append((int(parts[0], 16), parts[2]))
        _CACHE[key] = ([a for a, _ in rows], [n for _, n in rows])
    return _CACHE[key]


def resolve(port: Path | str, pc: int) -> tuple[str, int] | None:
    """(symbol, offset) holding `pc`, or None when it precedes every symbol."""
    addrs, names = symbols(port)
    index = bisect.bisect_right(addrs, pc) - 1
    return (names[index], pc - addrs[index]) if index >= 0 else None


def line_of(port: Path | str, pc: int) -> str | None:
    try:
        out = subprocess.run(
            ["addr2line", "-e", str(port), hex(pc)], capture_output=True, text=True, timeout=30, check=False
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return None if not out or out.startswith("??") else out


def level_of(project: Path | None, name: str) -> str:
    """The function's evidence rung from its source: asm, C unproven (guarded body) or matches."""
    site = tas_triage.definition_site(project, name) or tas_triage.include_asm_site(project, name)
    if not site or project is None:
        return "unknown"
    fn = evidence.parse((project / site.rpartition(":")[0]).read_text(errors="replace")).get(name)
    return evidence.level_name(fn)


def overlay(profile: Any, outcome: dict[str, Any], out: dict[str, Any]) -> dict[str, Any]:
    """`out` (tas_triage.claim) with the host fault's function+offset, site, evidence, arity note and next."""
    match = _FAULT.match(str(outcome.get("subject") or ""))
    if not match or out.get("kind") != "host fault":
        return out
    pc = int(match[1], 16)
    from abu_decomp import tas

    try:
        port = tas.settings(profile)["port"]
        found = resolve(port, pc)
    except Exception:
        return out
    if found is None:
        return out
    name, offset = found
    frame = f"{name}+{offset:#x}"
    project = tas_triage._project(profile)
    out["frame"] = frame
    if not _GAME.fullmatch(name):
        out["note"] = (
            f"{frame} is a port/shim frame; the nearest game function in the stacks is {out.get('owner', 'unknown')}"
        )
        return out
    site = tas_triage.definition_site(project, name) or tas_triage.include_asm_site(project, name)
    out["owner"] = f"{frame} at {site}" if site else frame
    out["evidence"] = level_of(project, name)
    src = line_of(port, pc)
    if src:
        out["source"] = src
    notes = [n for n in arity.notes(profile, [name]) if n.get("function") == name]
    kept = [n for n in out.get("arity", []) if n.get("function") != name]
    out["arity"] = notes + kept
    if any(n.get("verdict") == "disagree" for n in notes):
        out["next"] = notes[0]["next"]
    else:
        out["next"] = f"recipe work {name}"
    out["claim"] = (
        f"host fault: {outcome.get('subject')} = {frame}; blocking=true; {out['cannot_help']}; owner {out['owner']}; "
        f"evidence {out['evidence']}" + (f"; arity {notes[0]['summary']}" if notes else "") + f"; next: {out['next']}"
    )
    return out


def claim(profile: Any, outcome: dict[str, Any], stacks: list[str] | None = None) -> dict[str, Any]:
    """tas_triage.claim plus the host fault's own function. Never raises."""
    return overlay(profile, outcome, tas_triage.claim(profile, outcome, stacks))
