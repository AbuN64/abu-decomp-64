"""The runner's own continuation: when a closing lane is continued without the orchestrator.

A lane that ends with an unfinished RESULT.md or a context handoff is continued by `decomp lane continue`, so the
registry, the ladder and the overlap guard apply exactly as for any launch. A lane whose RESULT.md says DONE ends.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

import abu_decomp
from abu_decomp import DecompError

# The one done marker: RESULT.md's first non-blank line is the word DONE, then end of line, a space or a
# colon. Nothing else counts: "Status: COMPLETE", "RESULT: DONE" and a DONE on a later line are unfinished.
DONE = re.compile(r"DONE(?:$|[\s:])")
DONE_MARKER = "DONE"
DONE_RULE = (
    "Finished-lane contract: a lane that finished its whole task writes RESULT.md whose FIRST line is "
    f"exactly `{DONE_MARKER}`, the summary after it. No other wording ends a lane, so any RESULT.md without that first "
    "line is continued; an unfinished lane must not write it."
)
TRAILING = re.compile(r"(\d+)$")


def next_name(name: str, taken: set[str] | frozenset[str] = frozenset()) -> str:
    """The successor's name: the trailing number plus one (zero padding kept; a name without one gets 2).

    A name in `taken` (a sibling lane dir or registry row, such as TOP-8 beside TOP-7) is skipped by counting on, so a
    family of lanes named TOP-n never collides with its own successor.
    """
    candidate = name
    while True:
        match = TRAILING.search(candidate)
        if match is None:
            candidate += "2"
        else:
            digits = match[1]
            candidate = candidate[: match.start()] + str(int(digits) + 1).zfill(len(digits))
        if candidate not in taken:
            return candidate


def taken_names(directory: Path, registered: list[str]) -> set[str]:
    """Lane names already in use next to `directory`: non-empty sibling lane dirs and every registry row name."""
    siblings = (
        {entry.name for entry in directory.parent.iterdir() if entry.is_dir() and any(entry.iterdir())}
        if directory.parent.is_dir()
        else set()
    )
    return siblings | set(registered)


def result_text(directory: Path) -> str:
    path = directory / "RESULT.md"
    return path.read_text(errors="replace") if path.is_file() else ""


def explicit_done(done_when: str, said: str) -> bool:
    """RESULT.md ends the chain by the identical rule or the first-line DONE marker."""
    if done_when == "identical":
        return " identical " in said
    first = next((line for line in said.splitlines() if line.strip()), "")
    return DONE.match(first) is not None


COMMIT_WORD = re.compile(r"\b(?:pushed|committed|landed)\b", re.IGNORECASE)
COMMIT_ID = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")


def reported_commit(said: str) -> bool:
    """RESULT.md names a commit it pushed, committed or landed: one line with a 7-40 hex id and one of those words."""
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


def export_hashes(exports: list[str]) -> dict[str, str]:
    """Content hash of each declared export file that exists (a missing one has no entry)."""
    return {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in exports if Path(path).is_file()}


def changed_export(exports: list[str], before: dict[str, str], since: float) -> bool:
    """A declared export file is newer than `since` and its content differs from `before` (the link's start)."""
    now = export_hashes(exports)
    return any(Path(path).stat().st_mtime > since and digest != before.get(path) for path, digest in now.items())


def progress(
    said: str, owns: list[str], since: float, exports: list[str] | None = None, before: dict[str, str] | None = None
) -> str:
    """The evidence this link progressed (RESULT.md reports a commit, an owned tree gained one, an export changed).

    `exports` are the lane-declared files (under its owned dirs) that carry work instead of commits; `before` is their
    hashes when the link started, which equal the previous link's last ones. "" means no progress.
    """
    if reported_commit(said):
        return "RESULT.md reports a commit"
    if gained_commits(owns, since):
        return "an owned git tree gained a commit"
    if changed_export(exports or [], before or {}, since):
        return "a declared export file changed"
    return ""


def plan(
    done_when: str,
    continues: int,
    said: str,
    handoff: bool,
    context: bool,
    cancelled: bool,
    progressed: str = "",
    waiting: int = 0,
) -> tuple[str, str]:
    """(action, why): `none` (not a closing we continue), `end` (done), `blocked` (no budget) or `continue`.

    A link closing while its `waiting` rig jobs still run continues whatever RESULT.md and HANDOFF say,
    and spends no budget (its successor adopts the jobs).

    A link that progressed (`progressed` names the evidence) spends no budget and continues; a link without progress
    spends one, and at 0 the chain stops: "budget spent".
    """
    if cancelled:
        return "none", "cancelled"
    if explicit_done(done_when, said):
        return "end", "RESULT.md is DONE"
    if waiting:
        return "continue", f"waiting on {waiting} rig job(s) it started; budget kept at {continues}"
    if not said.strip() and not (context and handoff):
        return "none", "no RESULT.md and no context handoff"
    what = "context handoff" if context and handoff else "unfinished RESULT.md"
    if progressed:
        return "continue", f"{what}; progressed ({progressed}), budget kept at {continues}"
    if continues <= 0:
        return "blocked", f"{what}; blocked: budget spent (no progress, --continues is 0)"
    return "continue", f"{what}; no progress, spent one (budget {continues} to {continues - 1})"


def next_budget(continues: int, progressed: str, waiting: int = 0) -> int:
    """The successor's --continues: unchanged after a link that progressed or waits on its jobs, else one less."""
    return continues if progressed or waiting else continues - 1


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
