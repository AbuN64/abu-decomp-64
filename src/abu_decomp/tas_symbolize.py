"""A host fault names its function: the faulting EIP of the run's signal frame (tas_fault) resolved against the port
binary the run used. The gdb bridge names a native fault `host-fault-<address>`, the accessed address, and its stacks
only show the frame loop that reported it. This module reads the port's own symbol table (`nm -n`, cached by path and
mtime), turns the EIP into `function+offset`, and overlays the tas stop classification with that frame's source site,
arity note, evidence level and one `next`. A game function (func_* or a name defined in the project) outranks
port/shim frames: when the EIP is in a port frame the first game function on its return chain owns the fault.
"""

from __future__ import annotations

import bisect
import re
import subprocess
from pathlib import Path
from typing import Any

from abu_decomp import arity, evidence, tas_triage

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
    fault = outcome.get("fault")
    if not isinstance(fault, dict) or "eip" not in fault or out.get("kind") != "host fault":
        return out  # the subject's suffix is the accessed address, never a pc: only a fault record names the frame
    # the record names its addresses against the executable the run used (tas_fault.capture), never the profile's
    port = fault.get("port", "")
    addresses = [int(x, 16) for x in (fault["eip"], *fault.get("frames", []))]
    names = [fault.get("at", ""), *fault.get("callers", [])]
    named = []
    for address, text in zip(addresses, names, strict=False):
        if m := re.fullmatch(r"(\S+)\+0x([0-9a-f]+)", text):
            named.append((address, (m[1], int(m[2], 16))))
    if not named:
        return out
    first = named[0][1]
    game = next(((a, found) for a, found in named if _GAME.fullmatch(found[0])), None)
    frame = f"{first[0]}+{first[1]:#x}"
    project = tas_triage._project(profile)
    out["frame"] = frame
    if game is None:
        out["note"] = f"{frame} is a port/shim frame and no game function is on its return chain"
        return out
    if game[1] != first:
        out["note"] = f"{frame} is a port/shim frame called from {game[1][0]}+{game[1][1]:#x}"
    pc, (name, offset) = game
    out["frame"] = frame = f"{name}+{offset:#x}"
    site = tas_triage.definition_site(project, name) or tas_triage.include_asm_site(project, name)
    out["owner"] = f"{frame} at {site}" if site else frame
    out["evidence"] = level_of(project, name)
    src = line_of(port, pc) if port else None
    if src:
        out["source"] = src
    notes = [n for n in arity.notes(profile, [name]) if n.get("function") == name]
    kept = [n for n in out.get("arity", []) if n.get("function") != name]
    out["arity"] = notes + kept
    if any(n.get("verdict") == "disagree" for n in notes):
        out["next"] = notes[0]["next"]
    else:
        out["next"] = arity.next_command(getattr(profile, "source_file", None), name, [])
    out["claim"] = (
        f"host fault: {outcome.get('subject')} = {frame}; blocking=true; {out['cannot_help']}; owner {out['owner']}; "
        f"evidence {out['evidence']}" + (f"; arity {notes[0]['summary']}" if notes else "") + f"; next: {out['next']}"
    )
    return out


def claim(
    profile: Any, outcome: dict[str, Any], stacks: list[str] | None = None, health: dict[str, Any] | None = None
) -> dict[str, Any]:
    """tas_triage.claim plus the host fault's own function and the port build's facts. Never raises."""
    out = overlay(profile, outcome, tas_triage.claim(profile, outcome, stacks))
    try:
        from abu_decomp import tas, tas_health

        if health is None:
            health = tas_health.read(tas.settings(profile), profile)
        return tas_health.apply(profile, health, outcome, out)
    except Exception:
        return out
