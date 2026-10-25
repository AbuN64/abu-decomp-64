"""`decomp data stamp --diff X --build --tree SHARED`: build X alone on a warm stamp tree and stamp it.

A stamp used to need `data check` on a lane tree that held the diff, and a lane tree with several diffs rebuilt every unit
all of them changed (8 min wall, 1404 CPU-s with one header in it, 10-24). Here the diff gets its own `decomp worktree new`
tree (extraction, objects and times seeded from SHARED, so make rebuilds only what the diff reaches), `data check` judges
every version, a clean pass writes the stamp. The result carries wall and CPU seconds.

The tree is a SLOT (STAMP-S0.., `ABU_STAMP_TREES`, default 2; each ~6.6 GB under the shared tree's own folder, never
`wt/wt/`): a flock'd slot whose tree was left clean at SHARED's commit is reused (reset, diff applied, built incrementally,
reverted), so a stamp pays no new 6.6 GB seed copy and concurrent stamps are bounded by the slot count, not one tree per
stamp left behind by a killed run (10-24). A slot at another commit, or whose last run did not finish (marker still
"busy"), is rebuilt from the shared tree's warm extraction."""

from __future__ import annotations

import contextlib
import fcntl
import os
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, meter, worktree
from abu_decomp.data import check as check_mod
from abu_decomp.data import stamp as stamp_mod
from abu_decomp.data import trial
from abu_decomp.data.config import DataConfig

CPU_CAP = 300.0
STATE = ".stamp-state"  # in the tree: the commit it was left clean at, or "busy" while a stamp build owns it


def trees() -> int:
    return max(1, int(os.environ.get("ABU_STAMP_TREES") or 2))


def busy_in(tree: Path, proc: Path = Path("/proc")) -> list[int]:
    """Pids (other than ours) with their working directory inside TREE: the orphaned make/cc1 of a stamp that was killed by a
    cap (SIGTERM reaches only the tool, its children go on). The slot flock died with that stamp, but its jobs still write the
    tree, so the next stamp would reset and rebuild it under them (10-24: a reused STAMP-S1 linked no ROM in any version)."""
    root, mine, found = str(tree), os.getpid(), []
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == mine:
            continue
        try:
            cwd = os.readlink(entry / "cwd")
        except OSError:
            continue
        if cwd == root or cwd.startswith(root + "/"):
            found.append(int(entry.name))
    return found


def holder_of(path: Path) -> int | None:
    """The pid a lock file names as its holder (written on every take), or None."""
    try:
        return int(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def alive(pid: int, proc: Path = Path("/proc")) -> bool:
    return (proc / str(pid)).exists()


def describe(path: Path, proc: Path = Path("/proc")) -> str:
    """Who holds the lock, for a refusal: pid, command line, age of the claim."""
    pid = holder_of(path)
    if pid is None:
        return "an unknown holder"
    try:
        cmd = (proc / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        cmd = ""
    age = ""
    with contextlib.suppress(OSError):
        age = f", claimed {time.time() - path.stat().st_mtime:.0f}s ago"
    return f"pid {pid} ({'live: ' + cmd[:120] if cmd else 'gone'}){age}"


def take(path: Path) -> int | None:
    """The fd of an exclusive flock on PATH, or None when held. The file names its holder. A lock whose named holder is
    gone is stale (an orphan forked from it kept the flock): the file is replaced by a fresh one, which no orphan can
    hold, and taken. The fd never reaches a child process."""
    for _ in range(2):
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o664)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            pid = holder_of(path)
            if pid is not None and not alive(pid):
                with contextlib.suppress(OSError):
                    path.unlink()  # a new inode: the orphan keeps the old one, nobody waits on it
                continue
            return None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        return fd
    return None


@contextmanager
def slot(folder: Path, count: int, wait: float = 900.0) -> Iterator[str]:
    """The lane name of a stamp slot nobody else holds (a flock the kernel drops with the process, a dead holder's lock
    broken by `take`) and no process still works in (busy_in); waits for one, then refuses naming the holders."""
    folder.mkdir(parents=True, exist_ok=True)
    end = time.monotonic() + wait
    while True:
        for i in range(count):
            fd = take(folder / f".stamp-{i}.lock")
            if fd is None:
                continue
            if busy_in(folder / f"STAMP-S{i}"):
                os.close(fd)
                continue
            try:
                yield f"STAMP-S{i}"
            finally:
                os.close(fd)
            return
        if time.monotonic() > end:
            who = "; ".join(f"STAMP-S{i}: {describe(folder / f'.stamp-{i}.lock')}" for i in range(count))
            raise DecompError(
                "data.stamp", f"all {count} stamp trees are busy after {wait:.0f}s (ABU_STAMP_TREES): {who}"
            )
        time.sleep(2)


@contextmanager
def setup_lock(shared: Path, wait: float = 900.0) -> Iterator[None]:
    """One `unbake setup` at a time across the slots: it writes beside the shared tree (tools, caches) that the other slot's
    make is running from (10-24: a stamp's ROM check failed with no output while another diff was being extracted).
    Waits `wait` seconds, then refuses naming the holder."""
    folder = worktree.base(shared)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ".stamp-setup.lock"
    end = time.monotonic() + wait
    while (fd := take(path)) is None:
        if time.monotonic() > end:
            raise DecompError("data.stamp", f"the setup lock is held after {wait:.0f}s by {describe(path)}")
        time.sleep(2)
    try:
        yield
    finally:
        os.close(fd)


def _git(tree: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True)


def reuse(profile: Any, tree: Path, rev: str) -> dict[str, Any] | None:
    """The slot's tree, clean again, when it was left clean at exactly REV. None otherwise (the caller builds a fresh tree):
    a tree at another commit would need `unbake setup` in place, which splits the ROM cold for every version (10-24:
    5 cold extractions, 340 CPU s), where a fresh tree is seeded from the shared tree's warm extraction."""
    marker = tree / STATE
    state = marker.read_text().strip() if marker.is_file() else ""
    if not (tree / ".git").exists() or state != rev:
        return None
    if _git(tree, "reset", "-q", "--hard").returncode or _git(tree, "clean", "-fdq").returncode:
        return None
    return {"worktree": str(tree), "rev": rev, "reused": True}


def release(shared: Path, tree: Path) -> None:
    """Back to a clean tree and its marker: the diff and its build leave nothing but objects that make sees stale."""
    if _git(tree, "reset", "-q", "--hard").returncode or _git(tree, "clean", "-fdq").returncode:
        worktree.prune(shared, [tree.name])
        return
    (tree / STATE).write_text(_git(tree, "rev-parse", "HEAD").stdout.strip() + "\n")


def _tree(profile: Any, shared: Path, lane: str, rev: str | None):
    """(made, code, findings) for the slot's tree at REV (None: the branch tip): reused when it is clean, else fresh."""
    tree = worktree.base(shared) / lane
    if rev and (made := reuse(profile, tree, rev)):
        return made, 0, []
    worktree.prune(shared, [lane])  # a leftover of an interrupted run, or a tree that could not be made current
    with setup_lock(shared):
        return worktree.new(profile, shared, lane, rev)


def build(
    cfg: DataConfig, profile: Any, shared: Path, diff: Path, stamps: Path, at: str | None = None, reextract: bool = False
):
    """(result, code, findings). The stamp is written only when every version built and no exact function broke."""
    text = diff.read_text(errors="replace")
    if not stamp_mod.touches_data(cfg, text):
        return {"stamp": "not needed: the diff touches no declaration or data"}, 0, []
    if stamp_mod.stamped(stamps, text):
        return {"stamp": "present"}, 0, []
    shared = shared.expanduser().resolve()
    lag = worktree.behind(shared)
    if lag is not None and lag > worktree.MAX_BEHIND:
        msg = (
            f"the seed tree {shared} is {lag} commits behind main: a stamp seeded from it rebuilds from stale asm and objects. "
            "Refresh it (pool-mint.sh --refresh) and retry"
        )
        return (
            {"stamp": "not stamped: seed tree behind main"},
            1,
            [{"key": "data.stamp", "blocking": True, "message": msg}],
        )
    with slot(worktree.base(shared), trees()) as lane:
        return _build(cfg, profile, shared, diff, stamps, at, text, lane, reextract)


def _build(cfg, profile, shared, diff, stamps, at, text, lane, reextract=False):
    started, cpu = time.monotonic(), meter.cpu()
    # The tree is at SHARED's own commit: any other base differs in symbol facts and splits the ROM again for every
    # version (5 cold extractions, 70 s wall, 10-24). Only a diff that does not apply there goes to the tip.
    head = _git(shared, "rev-parse", "HEAD").stdout.strip()
    made, code, findings = _tree(profile, shared, lane, at or head or None)
    tree = Path(made["worktree"])
    extracted = kept = False
    try:
        if code:
            return {"worktree": made}, code, findings
        (tree / STATE).write_text("busy\n")
        applied = _git(tree, "apply", str(diff.resolve()))
        if applied.returncode and not at and head:
            made, code, findings = _tree(profile, shared, lane, None)
            tree = Path(made["worktree"])
            if code:
                return {"worktree": made}, code, findings
            (tree / STATE).write_text("busy\n")
            applied = _git(tree, "apply", str(diff.resolve()))
        if applied.returncode:
            msg = f"{diff.name} does not apply on {made['rev'][:9]}: {applied.stderr.strip()[:200]}"
            return {"worktree": made}, 1, [{"key": "data.stamp", "blocking": True, "message": msg}]
        same = made["rev"] == head  # the shared tree's objects are the clean build only at its own commit
        files = stamp_mod.files_of(text)
        if stamp_mod.extraction_files(files):
            extracted = (
                True  # the tree's asm now follows the diff's symbol rows: it is no clean tree for the next stamp
            )
            with setup_lock(shared):
                why = check_mod.reextract(profile, tree, files, allow=reextract)  # --reextract: a splat.yaml edit pays the re-split
            if why:
                return {"worktree": made}, 1, [{"key": "data.extract", "blocking": True, "message": why}]
        builder = trial.Builder(cfg, tree, reference=shared if same else None)
        results = [check_mod.check(builder, v) for v in cfg.versions]
        if (
            same and _git(shared, "rev-parse", "HEAD").stdout.strip() != head
        ):  # the pool moved on: its objects are not ours
            builder = trial.Builder(cfg, tree)
            results = [check_mod.check(builder, v) for v in cfg.versions]
        findings = [
            {"key": "data.check", "blocking": True, "message": f"{r['version']}: {u} {fn} {why}"}
            for r in results for u, fn, why in r["broken"]
        ] + [
            {"key": "data.build", "blocking": True, "message": f"{r['version']}: {u} does not compile: {why}"}
            for r in results for u, why in r.get("failed", [])
        ]  # fmt: skip
        if not findings and all(r["built"] for r in results):
            # A stamp means the 5 ROMs match: the units built, now link each version and compare its hash (10-24: a stamped
            # diff broke the de ROM at the train, which links). Incremental: the objects are the ones just built.
            rc, report = builder.rom_check()
            if rc and not tree.is_dir():
                return (
                    {"worktree": made},
                    1,
                    [
                        {
                            "key": "data.stamp",
                            "blocking": True,
                            "message": f"stamp tree {lane} ({tree}) vanished during the build: another process removed it; retry the stamp",
                        }
                    ],
                )
            if rc:
                kept = True  # the failed tree stays (its build/ and .stamp-rom.log) until the slot is next used
                findings = [{"key": "data.rom", "blocking": True, "message": f"ROM check failed: {report}"}]
        result: dict[str, Any] = {
            "results": results,
            "rev": made["rev"],
            "tree": lane,
            "reused": made.get("reused", False),
        }
        if not findings and all(r["built"] for r in results):
            result["stamp"] = str(stamp_mod.write(stamps, text))
        else:
            result["stamp"] = "not stamped: the check failed"
        return result, 1 if findings else 0, findings
    finally:
        wall, used = time.monotonic() - started, meter.cpu() - cpu
        if kept and (tree / ".git").exists():
            (tree / STATE).write_text("busy\n")  # not reusable; the next stamp in this slot rebuilds it
        elif extracted:
            worktree.prune(shared, [tree.name])
        elif (tree / ".git").exists():
            release(shared, tree)
        print(
            f"stamp build: wall={wall:.0f}s cpu={used:.0f}s tree={lane} {'reused' if made.get('reused') else 'fresh'}"
            + ("  FLAG CPU-OVER-5min" if used > CPU_CAP else ""),
            flush=True,
        )
