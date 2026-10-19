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


COMMIT_WORD = re.compile(r"\b(?:pushed|committed|landed)\b", re.IGNORECASE)
COMMIT_ID = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")


def reported_commit(said: str) -> bool:
    """RESULT names a commit it pushed, committed or landed: one line with a 7-40 hex id and one of those words."""
    return any(COMMIT_WORD.search(line) and COMMIT_ID.search(line) for line in said.splitlines())


def gained_commits(owns: list[str], since: float) -> bool:
    """An owned path inside a git work tree has a commit newer than `since` (epoch seconds) touching that path."""
    for owned in owns:
        if not Path(owned).is_dir():
            continue
        called = subprocess.run(
            ["git", "-C", owned, "log", f"--since=@{int(since)}", "-1", "--format=%H", "--", "."],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        if called.returncode == 0 and called.stdout.strip():
            return True
    return False


def progress(said: str, owns: list[str], since: float) -> str:
    """The evidence this link progressed (RESULT reports a commit, an owned tree gained one), or "" for none."""
    if reported_commit(said):
        return "RESULT reports a commit"
    if gained_commits(owns, since):
        return "an owned git tree gained a commit"
    return ""


def plan(
    done_when: str, continues: int, said: str, handoff: bool, context: bool, cancelled: bool, progressed: str = ""
) -> tuple[str, str]:
    """(action, why): `none` (not a closing we continue), `end` (done), `blocked` (no budget) or `continue`.

    A link that progressed (`progressed` names the evidence) spends no budget and continues; a link without progress
    spends one, and at 0 the chain stops: "budget spent".
    """
    if cancelled:
        return "none", "cancelled"
    if explicit_done(done_when, said):
        return "end", "RESULT is DONE"
    if not said.strip() and not (context and handoff):
        return "none", "no RESULT and no context handoff"
    what = "context handoff" if context and handoff else "unfinished RESULT"
    if progressed:
        return "continue", f"{what}; progressed ({progressed}), budget kept at {continues}"
    if continues <= 0:
        return "blocked", f"{what}; blocked: budget spent (no progress, --continues is 0)"
    return "continue", f"{what}; no progress, spent one (budget {continues} to {continues - 1})"


def next_budget(continues: int, progressed: str) -> int:
    """The successor's --continues: unchanged after a link that progressed, one less after one that did not."""
    return continues if progressed else continues - 1


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
