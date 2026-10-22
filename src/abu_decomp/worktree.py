"""`decomp worktree new LANE`: a lane's own work tree, warm from the shared tree (10-22: a fresh worktree paid ~970
CPU-s of preprocessing and setup before its first `recipe work`, over the 5-minute cap).

A new git worktree has no extraction, no objects and fresh source times, so make preprocesses every unit again. The
seed makes it what the shared tree is at the same commit: the extraction, the objects, the preprocessed texts and the
unbake facts are copied (read under the tree's shared lock, so no ship is half way through writing them), and every
tracked file whose bytes equal the shared tree's file gets that file's time, so make finds the copied outputs current
and rebuilds only what the commit changed. The worktree keeps its own `.unbake` state: recipe leases, tries, the
ledger and the inbox never touch the shared tree's. A lane lands only through `decomp ship --tree SHARED --patch`."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from unbake import treelock

from abu_decomp import DecompError, land, meter, tools
from abu_decomp.config import Profile

LINKED = ("roms", "tools")  # ignored inputs the worktree reads in place
COPIED = (
    "asm",
    "build",
)  # outputs make and the extraction keep up to date: a copy, never a link (compilers write in place)
FACTS = (
    "extract.json",
    "setup.json",
    "make.json",
    "make.Makefile",
    "map.json",
    "symbols",
    "delta",
    "extract",
    "fakes.json",
)
OWN = ("local.mk",)


class WorktreeError(DecompError):
    pass


def _git(tree: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True, check=False)
    if done.returncode:
        raise WorktreeError("worktree.git", f"git {' '.join(args[:3])}: {(done.stderr or done.stdout).strip()[-300:]}")
    return done.stdout


def _copy(source: Path, target: Path) -> None:
    if source.is_dir():
        subprocess.run(["cp", "-a", "--reflink=auto", str(source), str(target)], check=True)
    elif source.exists():
        shutil.copy2(source, target)


def align_times(shared: Path, tree: Path) -> int:
    """Every tracked file of `tree` whose bytes equal the shared tree's file takes that file's modification time."""
    count = 0
    for name in _git(tree, "ls-files", "-z").split("\0"):
        if not name:
            continue
        one, other = shared / name, tree / name
        try:
            a, b = one.stat(), other.stat()
            if a.st_size == b.st_size and one.read_bytes() == other.read_bytes():
                os.utime(other, ns=(a.st_atime_ns, a.st_mtime_ns))
                count += 1
        except OSError:
            continue
    return count


def new(
    profile: Profile, shared: Path, lane: str, rev: str | None = None
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    started, cpu = time.monotonic(), meter.cpu()
    shared = shared.expanduser().resolve()
    target = shared.parent / "wt" / lane
    if target.exists():
        raise WorktreeError("worktree.exists", f"{target} exists: one worktree per lane")
    table = land.settings(profile, shared)
    if rev is None:
        _git(shared, "fetch", "-q", table["remote"], table["branch"])
        rev = _git(shared, "rev-parse", "--verify", "FETCH_HEAD^{commit}").strip()
    target.parent.mkdir(parents=True, exist_ok=True)
    _git(shared, "worktree", "add", "--detach", "-q", str(target), rev)
    with treelock.hold(shared, who=f"worktree {lane}"):  # no ship writes the outputs while they are copied
        for name in LINKED:
            if (shared / name).exists() and not (target / name).exists():
                (target / name).symlink_to(shared / name)
        for name in (*COPIED, *OWN):
            _copy(shared / name, target / name)
        (target / ".unbake").mkdir(exist_ok=True)
        for name in FACTS:
            _copy(shared / ".unbake" / name, target / ".unbake" / name)
        aligned = align_times(shared, target)
    called = tools.invoke("unbake", profile, ["setup", "--no-make"], project=target)
    result = {
        "worktree": str(target),
        "rev": rev,
        "aligned": aligned,
        "setup_ok": called.ok,
        "seconds": round(time.monotonic() - started, 1),
        "cpu": round(meter.cpu() - cpu, 1),
        "ship": f"decomp --profile {profile.source_file} ship --tree {shared} --patch {lane}=PATH:MESSAGE",
    }
    findings = [] if called.ok else [{"key": "worktree.setup", "message": "unbake setup refused", "blocking": True}]
    return result, int(not called.ok), findings
