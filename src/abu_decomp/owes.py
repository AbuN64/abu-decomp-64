"""The functions that owe a match, read from the code and the play logs, never stored in the game repo.

A function owes a match when its body sits under the `NON_MATCHING` guard (a behaves-the-same stand-in for a body that
does not yet produce the original's bytes), or when it is still an `INCLUDE_ASM` stand-in that play reached. Each is
listed with its evidence level (`asm`, `C unproven`, `behaves k/n`; evidence.py) and ranked by how many `tas` runs
reached it (a `boot-step`, `boot-missing` or `debug-reason` line, or a frame of the failure stack): the next work list.
The behaves level is the evidence gate's cached measure of the file as it stands, found again after a tool version
moved the cache key; a function never measured is `C unproven`. Nothing here runs behave.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, evidence, evidence_gate

_REACHED = re.compile(r"^(?:boot-step: \d+/\d+ |boot-missing: |debug-reason: )(\w+)$", re.M)
_FRAME = re.compile(r"\bin (\w+) \(")
LEVELS = ("asm", "C unproven", "behaves")


def settings(profile: Any) -> dict[str, Any]:
    """The `land.evidence` section (guard, stand-in, suffixes, headers, ROM globs); the command needs it."""
    from abu_decomp.land import evidence_settings

    given = profile.section("land").get("evidence")
    cfg = evidence_settings(profile, given if given is not None else {})
    assert cfg is not None
    return cfg


def reached(runs: Path) -> tuple[Counter[str], int]:
    """How many runs reached each name, and how many runs there are (a run is a directory with an output.txt)."""
    counts: Counter[str] = Counter()
    logs = sorted({*runs.glob("*/output.txt"), *runs.glob("*/*/output.txt")})
    for log in logs:
        stack = log.with_name("failure-stack.txt")
        names = set(_REACHED.findall(log.read_text(errors="replace")))
        names |= set(_FRAME.findall(stack.read_text(errors="replace"))) if stack.is_file() else set()
        counts.update(names)
    return counts, len(logs)


def functions(project: Path, cfg: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Every NON_MATCHING body and INCLUDE_ASM stand-in under src: name -> {file, kind}."""
    found: dict[str, dict[str, str]] = {}
    for path in sorted((project / "src").rglob("*")):
        if path.suffix.lower() not in cfg["suffixes"] or not path.is_file():
            continue
        text = path.read_text(errors="replace")
        if cfg["guard"] not in text and cfg["stand_in"] not in text:
            continue
        for name, fn in evidence.parse(text, cfg["guard"], cfg["stand_in"]).items():
            if fn.kind != evidence.PLAIN:
                found.setdefault(name, {"file": str(path.relative_to(project)), "kind": fn.kind})
    return found


def measured(profile: Any, project: Path, cfg: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """The newest cached behave measure per (function, file text digest) for the current headers and ROMs."""
    from abu_decomp.behave.run import configuration, work_dir

    listing = subprocess.run(
        ["git", "-C", str(project), "ls-tree", "-r", "HEAD", "--", *cfg["headers"]],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    want = (evidence_gate.header_digest(listing), evidence_gate.rom_digest(project, cfg["roms"]))
    out: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
    for path in (work_dir(configuration(profile)) / "evidence").glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or (data.get("headers"), data.get("roms")) != want:
            continue
        key = (str(data.get("function")), str(data.get("text")))
        if key not in out or path.stat().st_mtime > out[key][0]:
            out[key] = (path.stat().st_mtime, data)
    return {k: v[1] for k, v in out.items()}


def run(profile: Any, project: Path | None, top: int) -> dict[str, Any]:
    from abu_decomp import tas, tools

    cfg = settings(profile)
    root = tools.project_dir(profile, project)
    try:
        counts, runs = reached(tas.settings(profile)["runs"])
    except DecompError:
        counts, runs = Counter(), 0
    scores = measured(profile, root, cfg)
    rows: list[dict[str, Any]] = []
    for name, where in functions(root, cfg).items():
        hits = counts.get(name, 0)
        if where["kind"] == evidence.ASM and not hits:
            continue
        level, word = "asm", "asm"
        if where["kind"] == evidence.NM:
            digest = evidence_gate.text_digest((root / where["file"]).read_text(errors="replace"))
            data = scores.get((name, digest))
            seen = bool(data and data.get("k", 0) > 0)
            level, word = (f"behaves {data['k']}/{data['n']}", "behaves") if seen and data else ("C unproven",) * 2
        rows.append({"function": name, "file": where["file"], "level": level, "word": word, "reached": hits})
    rows.sort(key=lambda r: (-r["reached"], r["function"]))
    levels = Counter(r.pop("word") for r in rows)
    for row in rows[:top]:
        row["next"] = f"recipe work {row['function']}"
    return {
        "owes": len(rows),
        "levels": {name: levels.get(name, 0) for name in LEVELS},
        "runs": runs,
        "reached_by_play": sum(1 for r in rows if r["reached"]),
        "top": rows[:top],
        "next": rows[0]["next"] if rows else "nothing owes a match",
    }
