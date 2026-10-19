"""The continuation rule for `decomp lane continue`: the new brief, the old lane's launch spec, the active list.

One rule for every lane: a lane that closed (context cap, wall, crash) is continued by a fresh lane in a sibling
directory. Its brief is the old brief with the appended LANE RULES and any older `## Continuation` section dropped,
then one continuation that points at the old HANDOFF.md and STATUS.md, then the rules again.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from abu_decomp import DecompError
from abu_decomp.lanes.relaunch import DONE_RULE

HEADING = "## Continuation"
STEERS_HEADING = "## Steers carried from earlier links"
STANDING_FILE = "STANDING-ORDERS.md"
STANDING_OPEN, STANDING_CLOSE = "<!-- STANDING-ORDERS -->", "<!-- /STANDING-ORDERS -->"
STEER_START = re.compile(r"^STEER \([^)\n]*\):\n", re.M)
CARRY_RULE = (
    "Steers delivered to earlier links and the lane's STANDING-ORDERS.md carry over into every continuation brief; "
    "re-read them below."
)
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


def split_brief(brief: str) -> tuple[str, list[str], str]:
    """(task text, steers in order, standing orders) of a brief.

    Steers are the `STEER (time, file):` blocks steer.py appends (after the LANE RULES block of a live lane) plus the
    ones an earlier continuation already carried; the task text drops the rules, `## Continuation` and the carried
    section, and the standing-orders block is returned verbatim."""
    standing = ""
    start, end = brief.find(STANDING_OPEN), brief.find(STANDING_CLOSE)
    if 0 <= start < end:
        standing = brief[start + len(STANDING_OPEN) : end].strip("\n")
        brief = brief[:start] + brief[end + len(STANDING_CLOSE) :]
    kept: list[str] = []
    carried: list[str] = []
    tail: list[str] = []
    skipping = carrying = False
    lines = brief.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("LANE RULES"):
            tail = lines[index + 1 :]
            break
        if line.strip() == STEERS_HEADING:
            skipping = carrying = True
            continue
        if line.strip() == HEADING or line.startswith(HEADING + " "):
            skipping, carrying = True, False
            continue
        if skipping and line.startswith("## "):
            skipping = carrying = False
        if carrying:
            carried.append(line)
        elif not skipping:
            kept.append(line)
    steers: list[str] = []
    for text in ("\n".join(carried), "\n".join(tail)):
        found = list(STEER_START.finditer(text))
        for number, match in enumerate(found):
            stop = found[number + 1].start() if number + 1 < len(found) else len(text)
            steers.append(text[match.start() : stop].strip())
    return "\n".join(kept).rstrip(), steers, standing


def task_text(brief: str) -> str:
    """The brief without its appended LANE RULES block, `## Continuation`, carried steers and standing orders."""
    return split_brief(brief)[0]


def continuation_brief(brief: str, rules: str, old_name: str, old_dir: Path, new_dir: Path, adopt: str = "") -> str:
    handoff = os.path.relpath(old_dir / "HANDOFF.md", new_dir)
    status = os.path.relpath(old_dir / "STATUS.md", new_dir)
    task, steers, standing = split_brief(brief)
    declared = old_dir / STANDING_FILE
    if declared.is_file():
        standing = declared.read_text().strip("\n")
    section = (
        f"{HEADING}\n\n"
        + f"This lane continues {old_name}, which has closed. Before anything else read {handoff} and {status}, "
        "then continue from where STATUS says. Do not redo finished steps or rerun finished checks. "
        "Keep your own STATUS.md and HANDOFF.md in this lane's directory. "
        f"{CARRY_RULE} "
        f"{DONE_RULE}"
    )
    parts = [f"{STANDING_OPEN}\n{standing}\n{STANDING_CLOSE}"] if standing else []
    parts += [task, section]
    # Deduplicated by content: the same steer text sent to two links is one order.
    seen: set[str] = set()
    kept = []
    for steer in steers:
        body = STEER_START.sub("", steer, count=1).strip()
        if body not in seen:
            seen.add(body)
            kept.append(steer)
    if kept:
        parts.append(STEERS_HEADING + "\n\n" + "\n\n".join(kept))
    parts.append(rules.rstrip())
    return "\n\n".join(parts) + "\n"


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


def require(spec: dict[str, Any], keys: tuple[str, ...]) -> None:
    missing = [key for key in keys if spec.get(key) in (None, "", [], ())]
    if missing:
        flags = ", ".join("--" + key.replace("_", "-") for key in missing)
        raise DecompError("lanes.inherit", f"the old lane does not record {flags}; pass them to `decomp lane continue`")
