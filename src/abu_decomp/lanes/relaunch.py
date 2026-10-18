"""The runner's own continuation: when a closing lane is continued without the orchestrator.

A lane that ends with an unfinished RESULT or a context handoff is continued by `decomp lane continue`, so the
registry, the ladder and the overlap guard apply exactly as for any launch. A lane whose RESULT says DONE ends.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import abu_decomp
from abu_decomp import DecompError

DONE = re.compile(r"\s*(?:RESULT:\s*)?DONE\b")
TRAILING = re.compile(r"(\d+)$")


def next_name(name: str) -> str:
    """The successor's name: the trailing number plus one (zero padding kept); a name without one gets 2."""
    match = TRAILING.search(name)
    if match is None:
        return name + "2"
    digits = match[1]
    return name[: match.start()] + str(int(digits) + 1).zfill(len(digits))


def result_text(directory: Path) -> str:
    for name in ("RESULT", "RESULT.md"):
        path = directory / name
        if path.is_file():
            return path.read_text(errors="replace")
    return ""


def explicit_done(done_when: str, said: str) -> bool:
    """RESULT ends the chain: `identical` lanes by their ` identical ` word, `result` lanes by an explicit DONE line."""
    if done_when == "identical":
        return " identical " in said
    return any(DONE.match(line) for line in said.splitlines())


def plan(done_when: str, continues: int, said: str, handoff: bool, context: bool, cancelled: bool) -> tuple[str, str]:
    """(action, why): `none` (not a closing we continue), `end` (done), `blocked` (no budget) or `continue`."""
    if cancelled:
        return "none", "cancelled"
    if explicit_done(done_when, said):
        return "end", "RESULT is DONE"
    if not said.strip() and not (context and handoff):
        return "none", "no RESULT and no context handoff"
    what = "context handoff" if context and handoff else "unfinished RESULT"
    if continues <= 0:
        return "blocked", f"{what}; the --continues budget is spent"
    return "continue", what


def spawn(profile: Path, directory: Path, name: str, continues: int, wait: int = 600) -> str:
    """Start `decomp lane continue` as its own transient unit (the lane's unit is still active and would be killed).

    The unit waits for the lane's unit to close, then continues with one less continuation. Returns its unit name.
    """
    unit = f"relaunch-{name}"
    command = [
        "systemd-run", "--user", "--collect", "--quiet", f"--unit={unit}", f"--working-directory={directory}",
        f"--property=StandardOutput=append:{directory / 'relaunch.log'}",
        f"--property=StandardError=append:{directory / 'relaunch.log'}",
        f"--setenv=PYTHONPATH={Path(abu_decomp.__file__).parents[1]}",
        sys.executable, "-m", "abu_decomp", "--profile", str(profile), "lane", "continue",
        "--from", str(directory), "--name", name, "--continues", str(continues), "--wait", str(wait),
    ]  # fmt: skip
    called = subprocess.run(command, capture_output=True, text=True, check=False)
    if called.returncode != 0:
        raise DecompError("lanes.systemd", f"systemd-run failed: {called.stderr.strip()}")
    return unit
