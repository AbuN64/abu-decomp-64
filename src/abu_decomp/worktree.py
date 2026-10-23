"""`decomp worktree new LANE`: a lane's own work tree, warm from the shared tree (10-22: a fresh worktree paid ~970
CPU-s of preprocessing and setup before its first build, over the 5-minute cap).

A new git worktree has no extraction, no objects and fresh source times, so make preprocesses every unit again. The
seed makes it what the shared tree is at the same commit: the extraction, the objects, the preprocessed texts and the
unbake facts are copied (WITHOUT the tree lock, which a ship holds for its whole landing; a file a ship rewrote
during the copy is dropped and the worktree's own build makes it again), and every
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


def _files(root: Path) -> dict[str, tuple[int, int]]:
    found = {}
    for base, _dirs, names in os.walk(root):
        for name in names:
            path = Path(base) / name
            try:
                st = path.lstat()
            except OSError:
                continue
            found[str(path.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return found


def seed(shared: Path, target: Path) -> int:
    """Copy the shared tree's outputs and facts without its lock: a ship holds that lock for the whole landing, and a
    new lane must not wait behind it. A file a ship rewrote while it was copied (size or time differs from the source
    afterwards) is deleted from the copy, and the worktree's own warm build makes it again. Returns files dropped."""
    for name in LINKED:
        if (shared / name).exists() and not (target / name).exists():
            (target / name).symlink_to(shared / name)
    sources = [*COPIED, *OWN]
    facts = [Path(".unbake") / f for f in FACTS]
    (target / ".unbake").mkdir(exist_ok=True)
    for name in (*sources, *facts):
        try:
            _copy(shared / name, target / name)
        except (subprocess.CalledProcessError, OSError):
            shutil.rmtree(target / name, ignore_errors=True)  # vanished mid-copy: the build makes it again
    dropped = 0
    for name in (*sources, *facts):
        src, copy = shared / name, target / name
        if not copy.exists():
            continue
        if src.is_dir():
            theirs = _files(src)
            for key, have in _files(copy).items():
                if key in theirs and have != theirs[key]:
                    (copy / key).unlink(missing_ok=True)
                    dropped += 1
        elif src.exists():
            a, b = src.lstat(), copy.lstat()
            if (a.st_size, a.st_mtime_ns) != (b.st_size, b.st_mtime_ns):
                copy.unlink(missing_ok=True)
                dropped += 1
    return dropped


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
    epoch = treelock.epoch(shared)  # the shared tree's lock is never taken: a ship may hold it for an hour
    dropped = seed(shared, target)
    aligned = align_times(shared, target)
    if treelock.epoch(shared) != epoch:
        print("worktree.seed: a ship ended while the outputs were copied; torn files were dropped", flush=True)
    called = tools.invoke("unbake", profile, ["setup", "--no-make"], project=target)
    result = {
        "worktree": str(target),
        "rev": rev,
        "aligned": aligned,
        "dropped": dropped,
        "setup_ok": called.ok,
        "seconds": round(time.monotonic() - started, 1),
        "cpu": round(meter.cpu() - cpu, 1),
        "ship": f"decomp --profile {profile.source_file} ship --tree {shared} --patch {lane}=PATH:MESSAGE",
    }
    findings = [] if called.ok else [{"key": "worktree.setup", "message": "unbake setup refused", "blocking": True}]
    return result, int(not called.ok), findings


def _referenced(shared: Path, path: Path) -> list[str]:
    """Why a worktree may not go: a queued ship's published input, or a live process, names a file inside it."""
    why = []
    common = shared / ".git"
    for entry in (
        sorted((common / "decomp-ship.inputs").glob("*.json")) if (common / "decomp-ship.inputs").is_dir() else []
    ):
        if str(path) in entry.read_text(errors="replace"):
            why.append(f"queued ship input {entry.name}")
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            line = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if "decomp" in line and " ship " in line and str(path) in line and proc.name != str(os.getpid()):
            why.append(f"ship process {proc.name}")
    return why


def prune(
    shared: Path, ended: list[str], results: Path | None = None
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    """Remove the worktrees of ended lanes: the names given, and every lane with a RESULT.md under `results`. A
    worktree a queued ship still names is kept and reported. The disk freed is the result's `freed_mb`."""
    shared = shared.expanduser().resolve()
    base = shared.parent / "wt"
    names = set(ended)
    if results is not None:
        names |= {p.parent.name for p in results.glob("*/RESULT.md")}
    removed, kept = [], []
    before = shutil.disk_usage(base).free if base.is_dir() else 0
    for name in sorted(names):
        path = base / name
        if not path.is_dir():
            continue
        if why := _referenced(shared, path):
            kept.append({"worktree": str(path), "why": why})
            continue
        _git(shared, "worktree", "remove", "--force", str(path))
        removed.append(str(path))
    _git(shared, "worktree", "prune")
    freed = (shutil.disk_usage(base).free - before) // 2**20 if base.is_dir() else 0
    findings = [
        {"key": "worktree.referenced", "message": f"{k['worktree']} kept: {', '.join(k['why'])}", "blocking": False}
        for k in kept
    ]
    return {"removed": removed, "kept": kept, "freed_mb": freed}, 0, findings
