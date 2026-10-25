"""Selective corpus replay: `tas play --all --changed` replays only the movies a change can have touched.

A movie's last good run says which source files it entered (its gcovr report, RUN/coverage.json). The run record
keeps the build's pins (the file named by `tas.build.pins`: commits of the trees the port was built from). Between
that run's pins and the current ones a project command lists the changed game source files, and the movie is replayed
when any of them has a function the movie entered. Everything unknown replays: a movie without a pinned good run, a
non-ok last outcome, a pin other than the game's that moved, a changed file that is not a compiled source (a header,
data or asm), or a change list that failed. A skipped movie keeps its recorded standing; a plain `--all` is still the
full replay.

Section `tas.select`: {"game": "<pin key of the game sources>", "others": ["<pin keys whose move forces a full
replay>"], "changed": "<command printing changed source paths, one per line, with {old} and {new} = the game pin>"}.
The paths are matched as the tail of the file names in the coverage reports.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

from abu_decomp import DecompError
from abu_decomp.config import Profile, check_keys

KEYS = frozenset({"game", "others", "changed"})
SOURCES = (".c", ".cpp")
PINS = "pins"  # the run record's key


def settings(profile: Profile) -> dict[str, Any] | None:
    table = profile.section("tas").get("select")
    if not table:
        return None
    check_keys(profile.source_file, "tas.select", table, frozenset({"game", "changed"}), KEYS)
    return {
        "game": str(table["game"]),
        "others": [str(k) for k in table.get("others", [])],
        "changed": str(table["changed"]),
    }


def read_pins(cfg: dict[str, Any]) -> dict[str, Any] | None:
    """The current build's pins file (tas.build.pins), or None."""
    path = cfg.get("build", {}).get("pins")
    try:
        data = json.loads(Path(path).read_text()) if path else None
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def changed_files(select: dict[str, Any], old: dict[str, Any] | None, new: dict[str, Any] | None) -> set[str] | None:
    """The changed game source paths between two pin sets; None when everything must replay (unknown, or a pin other
    than the game's moved). Empty when nothing moved."""
    if not old or not new:
        return None
    if any(old.get(k) != new.get(k) for k in select["others"]):
        return None
    before, after = old.get(select["game"]), new.get(select["game"])
    if not before or not after:
        return None
    if before == after:
        return set()
    argv = shlex.split(select["changed"].format(old=before, new=after))
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return {line.strip() for line in done.stdout.splitlines() if line.strip()}


def entered(run: Path) -> dict[str, bool] | None:
    """{source file as the report names it: the movie entered one of its functions}; None without a report."""
    try:
        report = json.loads((run / "coverage.json").read_text())
    except (OSError, ValueError):
        return None
    return {
        entry.get("file", ""): any(f.get("execution_count") for f in entry.get("functions", []))
        for entry in report.get("files", [])
    }


def affected(files: dict[str, bool] | None, changed: set[str]) -> bool:
    """True when the movie may be touched by the changed paths (conservative on anything unknown)."""
    if files is None:
        return True
    for path in changed:
        if not path.lower().endswith(SOURCES):
            return True
        hits = [reached for name, reached in files.items() if name == path or name.endswith("/" + path)]
        if not hits or any(hits):
            return True
    return False


def resume_plan(
    cfg: dict[str, Any],
    names: list[str],
    previous: dict[str, tuple[Path, dict[str, Any]]],
) -> tuple[list[str], dict[str, tuple[Path, dict[str, Any]]], str]:
    """`play --all --resume`: a movie whose newest finished run was made by the CURRENT build (equal pins) and ended
    with a real outcome keeps that result; everything else (no run, no pins, other pins, refused/unknown) replays. No
    profile section needed: an interrupted or throttled sweep restarts without redoing its finished movies."""
    now = read_pins(cfg)
    replay: list[str] = []
    skipped: dict[str, tuple[Path, dict[str, Any]]] = {}
    for name in names:
        last = previous.get(name)
        if now is None or last is None or last[1].get(PINS) != now or last[1].get("outcome") in (None, "", "refused"):
            replay.append(name)
        else:
            skipped[name] = last
    return replay, skipped, f"{len(skipped)} of {len(names)} movies already have a result from this build"


def plan(
    profile: Profile,
    cfg: dict[str, Any],
    names: list[str],
    previous: dict[str, tuple[Path, dict[str, Any]]],
) -> tuple[list[str], dict[str, tuple[Path, dict[str, Any]]], str]:
    """(movies to replay, skipped movie -> (its last good run, record), why). `previous` maps a movie to its newest
    finished movie run and its record."""
    select = settings(profile)
    if select is None:
        raise DecompError("tas.select", "--changed needs a tas.select section in the profile")
    now = read_pins(cfg)
    replay: list[str] = []
    skipped: dict[str, tuple[Path, dict[str, Any]]] = {}
    cache: dict[tuple[str, str], set[str] | None] = {}
    for name in names:
        last = previous.get(name)
        if last is None or last[1].get("outcome") != "ok" or not last[1].get(PINS):
            replay.append(name)
            continue
        old = last[1][PINS]
        key = (json.dumps(old, sort_keys=True), "")
        if key not in cache:
            cache[key] = changed_files(select, old, now)
        changed = cache[key]
        if changed is None or affected(entered(last[0]), changed):
            replay.append(name)
        else:
            skipped[name] = last
    return replay, skipped, f"{len(skipped)} of {len(names)} movies unaffected by the changed sources"
