"""Which port executable a TAS play may use, and what every run records about it.

An explicitly named port (`--port`) is the isolated candidate path: it always runs, and the run records where it came
from. The profile's configured port is a shared seat: it runs only when the build states its provenance (repo, SHA,
shim pin) and that provenance equals the project's current main. Both facts come from section `tas.seat`:
  {"stamp": the JSON file beside the executable that states {"repo", "sha", "shim"} (written by whatever builds it),
   "repo": the repo the seat must be built from, "tree": a clone of it, "ref": the ref that is current main,
   "shim_file": a file of the tree that pins the shim, "shim_pattern": a regex whose group 1 is that pin}.
Everything but `stamp` is the requirement; a profile that names a shared port without it refuses every seat play.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from abu_decomp import DecompError

SEAT_KEYS = frozenset({"stamp", "repo", "tree", "ref", "shim_file", "shim_pattern"})
REQUIRE_KEYS = ("repo", "tree", "ref", "shim_file", "shim_pattern")
FIELDS = ("repo", "sha", "shim")


def stamp(seat: dict[str, Any], port: Path) -> dict[str, str]:
    """The provenance the build beside `port` states: {repo, sha, shim}, each '' when the stamp lacks it."""
    try:
        said = json.loads((Path(port).parent / seat["stamp"]).read_text())
    except (OSError, ValueError, KeyError):
        said = {}
    said = said if isinstance(said, dict) else {}
    return {key: str(said.get(key) or "") for key in FIELDS}


def current(seat: dict[str, Any]) -> dict[str, str]:
    """What the seat must have been built from: the tree's current main and the shim pin read from it."""
    tree, ref = str(seat["tree"]), str(seat["ref"])

    def git(*args: str) -> str:
        done = subprocess.run(["git", "-C", tree, *args], capture_output=True, text=True, check=False)
        if done.returncode:
            raise DecompError("tas.seat", f"git {' '.join(args)} failed in {tree}: {done.stderr.strip()}")
        return done.stdout

    found = re.search(seat["shim_pattern"], git("show", f"{ref}:{seat['shim_file']}"))
    if not found:
        raise DecompError("tas.seat", f"no shim pin matching {seat['shim_pattern']!r} in {ref}:{seat['shim_file']}")
    return {"repo": str(seat["repo"]), "sha": git("rev-parse", ref).strip(), "shim": found.group(1)}


def _said(have: dict[str, str]) -> str:
    return f"repo {have['repo'] or '?'} sha {have['sha'] or '?'} shim {have['shim'] or '?'}"


def check(cfg: dict[str, Any]) -> None:
    """Refuse the shared seat unless its stamped provenance is the current main's; the cause names the seat and SHAs."""
    port, seat = cfg.get("port"), cfg.get("seat") or {}
    if not port:
        raise DecompError("tas.port", "no port executable: the profile names no shared seat; pass --port <executable>")
    where = f"shared port seat {port}"
    if not all(seat.get(key) for key in REQUIRE_KEYS):
        raise DecompError(
            "tas.seat", f"{where} cannot be checked: tas.seat names no required {'/'.join(REQUIRE_KEYS)}; pass --port"
        )
    have = stamp(seat, Path(port))
    if not all(have.values()):
        raise DecompError(
            "tas.seat",
            f"{where} states no provenance ({seat['stamp']} beside it lacks "
            f"{', '.join(k for k in FIELDS if not have[k])}); "
            "pass --port or rebuild the seat",
        )
    want = current(seat)
    if have != want:
        raise DecompError(
            "tas.seat",
            f"{where} is stale: built from {_said(have)}, current main is {_said(want)}; pass --port or rebuild",
        )


def record(cfg: dict[str, Any], port: Path, build: str, explicit: bool) -> dict[str, Any]:
    """The run record's account of the executable: where it is, its SHA-1, whether it is the shared seat, and the
    repo/SHA/shim pin its build states (empty strings when it states none)."""
    return {
        "executable": str(port),
        "sha1": build,
        "seat": not explicit,
        **stamp(cfg.get("seat") or {"stamp": ""}, port),
    }
