"""Object identity as the proof of a header-only change (the types train, 10-22 REDUCE-6: NAMES-1's header proof took
616 s because it relinked 5 ROMs to learn that no object moved).

A unit's object is a pure function of its preprocessed text (.i), toolchain and flags, and the project's own Makefile
already treats an unchanged compiler output (.s) as an unchanged object. So a change of headers only needs the units
that read them: preprocess those (the Makefile replaces a .i only when its bytes change), compile the ones whose .i
moved, and compare each unit's .i, then its compiler output, with what the tip's build left. All equal: every object is
the tip's, the ROMs on disk are the tip's, nothing is relinked. Any difference, or any unit the tip's build state cannot
vouch for, is a reason and the full round runs."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from abu_decomp import shipproof

Make = Callable[[list[str]], tuple[int, str]]  # make targets -> (exit, output tail)


def digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def readers(tree: Path, touched: set[str]) -> dict[str, list[str]]:
    """Every built unit whose .d names a touched file: `build/<ver>/<src>.i.ok` stamp target -> its dependencies."""
    found: dict[str, list[str]] = {}
    for dep in sorted((tree / "build").rglob("*.d")):
        try:
            text = dep.read_text(errors="replace")
        except OSError:
            continue
        deps = shipproof.parse_deps(text)
        target = text.replace("\\\n", " ").split(":", 1)[0].strip()
        if deps and target.endswith(".i.ok") and touched & set(deps):
            found[target] = deps
    return found


def refuse(*reasons: str) -> dict[str, Any]:
    return {"ok": False, "reasons": list(reasons)}


def prove(tree: Path, files: Sequence[str], make: Make) -> dict[str, Any]:
    """{ok: True, units, recompiled, identical...} when every object the changed headers can reach is the tip's;
    {ok: False, reasons} otherwise (the caller then builds in full). `make` runs make on targets in `tree`."""
    names = sorted(files)
    if not names:
        return refuse("no changed file")
    if other := [n for n in names if not shipproof.HEADER.search(n)]:
        return refuse(f"{other[0]} is not a header" + (f" (and {len(other) - 1} more)" if len(other) > 1 else ""))
    build = tree / "build"
    if not any(build.rglob("*.d")):
        return refuse("the tree holds no build state to compare with")
    units = readers(tree, set(names))
    roms: dict[str, float | None] = {}
    before: dict[str, dict[str, Any]] = {}
    for stamp in units:
        version = Path(stamp).parts[1]
        if version not in roms:
            made = [mtime(rom) for rom in sorted((build / version).glob("*.z64"))]
            roms[version] = made[0] if made else None
        built = tree / stamp[: -len(".ok")]
        stage = built.with_name(built.name[: -len(".i")] + ".s")
        row = {"i": digest(built), "s": digest(stage), "i_time": mtime(built), "s_time": mtime(stage)}
        rom = roms[version]
        if rom is None:
            return refuse(f"{version} has no built ROM to carry over")
        if row["i"] is None or row["i_time"] > rom or (row["s_time"] is not None and row["s_time"] > rom):
            return refuse(f"{built} is newer than the {version} ROM: the tip's build left no state to compare")
        before[stamp] = row
    if not units:
        return {"ok": True, "units": 0, "preprocessed": 0, "recompiled": 0, "headers": names}
    code, tail = make(sorted(units))
    if code:
        return refuse(f"preprocessing the readers exited {code}: {tail.strip()[-200:]}")
    moved = [stamp for stamp in sorted(units) if digest(tree / stamp[: -len(".ok")]) != before[stamp]["i"]]
    if moved:
        code, tail = make([stamp[: -len(".i.ok")] + ".o" for stamp in moved])
        if code:
            return refuse(f"compiling {len(moved)} unit(s) whose text moved exited {code}: {tail.strip()[-200:]}")
        differ = []
        for stamp in moved:
            built = tree / stamp[: -len(".i.ok")]
            after = digest(built.with_name(built.name + ".s"))
            old = before[stamp]["s"]
            if old is None or after != old:
                differ.append(stamp[: -len(".i.ok")])
        if differ:
            return refuse(
                f"{len(differ)} of {len(moved)} recompiled unit(s) changed their compiler output "
                f"({', '.join(differ[:3])})"
            )
    return {
        "ok": True,
        "units": len(units),
        "preprocessed": len(units),
        "recompiled": len(moved),
        "identical": len(units),
        "headers": names,
    }
