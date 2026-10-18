"""The continuation rule for `decomp lane continue`: the new brief, the old lane's launch spec, the peek list.

One rule for every lane: a lane that closed (context cap, wall, crash) is continued by a fresh lane in a sibling
directory. Its brief is the old brief with the appended LANE RULES and any older `## Continuation` section dropped,
then one continuation that points at the old HANDOFF.md and STATUS.md, then the rules again.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from abu_decomp import DecompError

HEADING = "## Continuation"
# Launch-spec keys a continuation inherits, and the runner flags old-launched units spell them with.
FLAGS = {
    "--model": "model",
    "--effort": "effort",
    "--wall": "wall",
    "--continues": "continues",
    "--done-when": "done_when",
    "--fanout": "fanout",
    "--max-agents": "max_agents",
    "--cwd": "cwd",
    "--dir": "dir",
    "--name": "name",
    "--kind": "kind",
    "--owner-override": "owner_override",
}
INTEGERS = ("wall", "continues", "max_agents")


def task_text(brief: str) -> str:
    """The brief without its appended LANE RULES block and without any `## Continuation` section."""
    kept: list[str] = []
    skipping = False
    for line in brief.splitlines():
        if line.startswith("LANE RULES"):
            break
        if line.strip() == HEADING or line.startswith(HEADING + " "):
            skipping = True
            continue
        if skipping and line.startswith("## "):
            skipping = False
        if not skipping:
            kept.append(line)
    return "\n".join(kept).rstrip()


def continuation_brief(brief: str, rules: str, old_name: str, old_dir: Path, new_dir: Path) -> str:
    handoff = os.path.relpath(old_dir / "HANDOFF.md", new_dir)
    status = os.path.relpath(old_dir / "STATUS.md", new_dir)
    section = (
        f"{HEADING}\n\n"
        f"This lane continues {old_name}, which has closed. Before anything else read {handoff} and {status}, "
        "then continue from where STATUS says. Do not redo finished steps or rerun finished checks. "
        "Keep your own STATUS.md and HANDOFF.md in this lane's directory."
    )
    return f"{task_text(brief)}\n\n{section}\n\n{rules.rstrip()}\n"


def unit_text(unit: str) -> str | None:
    called = subprocess.run(["systemctl", "--user", "cat", unit], capture_output=True, text=True, check=False)
    return called.stdout if called.returncode == 0 else None


def known_units() -> list[str]:
    called = subprocess.run(
        ["systemctl", "--user", "list-units", "--all", "lane-*", "--no-legend", "--plain"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [row.split()[0].removesuffix(".service") for row in called.stdout.splitlines() if row.strip()]


def unit_spec(text: str) -> dict[str, Any]:
    """The launch spec in a lane unit's last ExecStart (systemd quotes each argument, as the shell does)."""
    lines = [line for line in text.splitlines() if line.startswith("ExecStart=") and line != "ExecStart="]
    if not lines:
        return {}
    argv = shlex.split(lines[-1].removeprefix("ExecStart="))
    spec: dict[str, Any] = {}
    for index, token in enumerate(argv[:-1]):
        if token in FLAGS:
            spec[FLAGS[token]] = argv[index + 1]
    for key in INTEGERS:
        if key in spec:
            spec[key] = int(spec[key])
    return spec


def launch_record(old_dir: Path) -> dict[str, Any] | None:
    """OLD/LAUNCH.json as spec keys: the runner's parsed arguments, written when the lane started."""
    path = old_dir / "LAUNCH.json"
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text())
    except ValueError as exc:
        raise DecompError("lanes.launch_record", f"{path}: {exc}") from exc
    if not isinstance(record, dict) or record.get("schema") != 1:
        raise DecompError("lanes.launch_record", f"{path}: expected a schema 1 object")
    for key in INTEGERS:
        if record.get(key) is not None:
            record[key] = int(record[key])
    return record


def old_unit(old_dir: Path, first: list[str] = ()) -> tuple[str, dict[str, Any]] | None:
    """The systemd unit of a lane, found by its --dir: the named units first, then every lane unit."""
    names = [*first, f"lane-{old_dir.name}", f"lane-{old_dir.name.lower()}"]
    for unit in [*names, *(unit for unit in known_units() if unit not in names)]:
        text = unit_text(unit)
        if text is None:
            continue
        spec = unit_spec(text)
        if spec.get("dir") and Path(spec["dir"]) == old_dir:
            return unit, spec
    return None


def replace_active(path: Path, old_dir: Path, name: str | None, transcript: str | None) -> None:
    """Rewrite the lanepeek list: drop the old lane's line (by its transcript path), then add the new one."""
    lines = path.read_text().splitlines() if path.exists() else []
    old = str(old_dir / "transcript.jsonl") if old_dir else None
    kept = [line for line in lines if not (old and line.partition("=")[2].strip() == old)]
    if name and transcript and f"{name}={transcript}" not in kept:
        kept.append(f"{name}={transcript}")
    temporary = path.with_name(path.name + ".new")
    temporary.write_text("".join(line + "\n" for line in kept))
    os.replace(temporary, path)


def require(spec: dict[str, Any], keys: tuple[str, ...]) -> None:
    missing = [key for key in keys if spec.get(key) in (None, "", [], ())]
    if missing:
        flags = ", ".join("--" + key.replace("_", "-") for key in missing)
        raise DecompError("lanes.inherit", f"the old lane does not record {flags}; pass them to `decomp lane continue`")
