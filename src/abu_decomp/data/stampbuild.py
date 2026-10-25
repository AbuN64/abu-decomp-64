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
from abu_decomp.data import gate as gate_mod
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


def release(shared: Path, tree: Path, before: dict[str, list] | None = None) -> None:
    """Back to a clean tree and its marker: the diff and its build leave nothing but objects that make sees stale.
    BEFORE: the tree's stamp records from before the diff was applied (`restore`)."""
    changed = [p for p in _git(tree, "diff", "--name-only", "-z", "HEAD").stdout.split("\0") if p]
    if _git(tree, "reset", "-q", "--hard").returncode or _git(tree, "clean", "-fdq").returncode:
        worktree.prune(shared, [tree.name])
        return
    restore(shared, tree, changed, before or {})
    (tree / STATE).write_text(_git(tree, "rev-parse", "HEAD").stdout.strip() + "\n")


def restore(shared: Path, tree: Path, changed: list[str], before: dict[str, list]) -> dict[str, int]:
    """The reverted files of a reused slot as the commit built them (A2, 10-25: the next stamp in a reused slot rebuilt
    2,000-4,500 includers per version a header diff had only signed). `git reset` gives each file the diff touched
    a new date: it takes the shared tree's date again where the bytes are the same, so make does not preprocess
    every includer of a reverted header; the includer stamps the diff's narrowing signed under its header bytes and
    no build touched are signed again under the commit's (`hdrnarrow.resign` with BEFORE, the records read before the
    diff was applied), so the stamp sweep keeps them. A unit the diff rebuilt keeps its newer stamp, whose record the
    sweep finds stale: make builds it again."""
    from unbake import hdrnarrow

    aligned = worktree.align_times(shared, tree, changed)
    headers = header_files(changed)
    return {"aligned": aligned, "signed": hdrnarrow.resign(tree, headers, before) if headers and before else 0}


def _tree(profile: Any, shared: Path, lane: str, rev: str | None):
    """(made, code, findings) for the slot's tree at REV (None: the branch tip): reused when it is clean, else fresh."""
    tree = worktree.base(shared) / lane
    if rev and (made := reuse(profile, tree, rev)):
        return made, 0, []
    worktree.prune(shared, [lane])  # a leftover of an interrupted run, or a tree that could not be made current
    with setup_lock(shared):
        return worktree.new(profile, shared, lane, rev)


def missing_versions(cfg: DataConfig, shared: Path) -> list[str]:
    """Versions the project builds (versions/<v>/splat.yaml) that the data config does not list: a stamp run without the
    host profile and config checks only the default pseudo-version and passes (10-25: a false pass missed the eu build)."""
    found = sorted(p.parent.name for p in (shared / "versions").glob("*/splat.yaml"))
    return [v for v in found if v not in cfg.versions]


def build(
    cfg: DataConfig,
    profile: Any,
    shared: Path,
    diff: Path,
    stamps: Path,
    at: str | None = None,
    reextract: bool = False,
):
    """(result, code, findings). The stamp is written only when every version built and no exact function broke."""
    text = diff.read_text(errors="replace")
    if not stamp_mod.touches_data(cfg, text):
        return {"stamp": "not needed: the diff touches no declaration or data"}, 0, []
    if stamp_mod.stamped(stamps, text):
        return {"stamp": "present"}, 0, []
    if bad := gate_mod.precheck(
        cfg, text
    ):  # the gate refuses these at queue time: cheaper to say so before a 70 s build
        return (
            {"stamp": "not stamped: the gate would refuse this diff"},
            1,
            [{"key": "data.gate", "blocking": True, "message": m} for m in bad],
        )
    shared = shared.expanduser().resolve()
    if absent := missing_versions(cfg, shared):
        msg = (
            f"the data config checks versions {list(cfg.versions)} but the project builds {absent} too: a stamp must build all "
            "of them. Pass the host profile and config: `decomp --profile ~/rig-state/tas/profile.json data --config "
            "~/rig-state/data/rw.toml stamp ...`"
        )
        return (
            {"stamp": "not stamped: the config does not cover every version"},
            1,
            [{"key": "data.stamp", "blocking": True, "message": msg}],
        )
    if at is None and _renamed(profile, shared, diff):
        return {"stamp": str(stamp_mod.write(stamps, text)), "proof": "renameproof"}, 0, []
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


def _renamed(profile: Any, shared: Path, diff: Path) -> bool:
    """Whether `unbake renameproof` proves the diff a pure rename of identifiers on the shared tree's commit: the proof
    already stands for the 5 ROMs when a diff is queued (claim-preflight), so a stamp needs no build (10-25: a types pack
    of renamed tags and members stamped at 831 CPU s). A diff that does not apply there, or any tool failure, builds."""
    if profile is None:
        return False
    head = _git(shared, "rev-parse", "HEAD").stdout.strip()
    if not head:
        return False
    from abu_decomp import tools

    try:
        called = tools.invoke(
            "unbake", profile, ["renameproof", "--against", head, "--patch", str(diff.resolve())], project=shared
        )
    except Exception:
        return False
    return bool(called.ok)


def header_files(files: list[str]) -> list[str]:
    """The headers among a diff's files (what `expand_units` counts as one), for the ROM link to treat as old."""
    return sorted(f for f in files if f.startswith("include/") or f.lower().endswith(".h"))


def _phase(name: str, since: list[float]) -> None:
    """One line per stamp phase with its wall and CPU seconds (10-25: a stamp of 762-1434 CPU s named no phase)."""
    now = [time.monotonic(), meter.cpu()]
    used = now[1] - since[1]
    over = f"  FLAG CPU-OVER-5min step={name}" if used > CPU_CAP else ""
    print(f"stamp phase {name}: wall={now[0] - since[0]:.0f}s cpu={used:.0f}s{over}", flush=True)
    since[:] = now


def _build(cfg, profile, shared, diff, stamps, at, text, lane, reextract=False):
    started, cpu = time.monotonic(), meter.cpu()
    mark = [started, cpu]
    # The tree is at SHARED's own commit: any other base differs in symbol facts and splits the ROM again for every
    # version (5 cold extractions, 70 s wall, 10-24). Only a diff that does not apply there goes to the tip.
    head = _git(shared, "rev-parse", "HEAD").stdout.strip()
    made, code, findings = _tree(profile, shared, lane, at or head or None)
    tree = Path(made["worktree"])
    extracted = kept = False
    from unbake import hdrnarrow

    before = {} if code else dict(hdrnarrow.records(tree))  # the stamp records the clean slot holds, for `restore`
    try:
        if code:
            return {"worktree": made}, code, findings
        (tree / STATE).write_text("busy\n")
        applied = _git(tree, "apply", str(diff.resolve()))
        if applied.returncode and not at and head:
            # A diff based on an older commit than the pool tree: merge it onto the pool tree's own commit first (the train
            # applies --3way too). Moving the tree to the tip instead re-splits every splat.yaml main changed since the
            # pool's extraction (~450 CPU s) and drops the warm objects.
            applied = _git(tree, "apply", "--3way", str(diff.resolve()))
            if applied.returncode:  # conflicts leave markers behind: back to the clean commit before the tip is tried
                _git(tree, "reset", "-q", "--hard")
                _git(tree, "clean", "-fdq")
        if applied.returncode and not at and head:
            made, code, findings = _tree(profile, shared, lane, None)
            tree = Path(made["worktree"])
            before = {} if code else dict(hdrnarrow.records(tree))
            if code:
                return {"worktree": made}, code, findings
            (tree / STATE).write_text("busy\n")
            applied = _git(tree, "apply", str(diff.resolve()))
        if (
            applied.returncode
        ):  # main moved over files the diff edits: the train applies with --3way, so the stamp does too
            applied = _git(tree, "apply", "--3way", str(diff.resolve()))
        if (
            not applied.returncode
        ):  # a file the diff creates (a module a rename moved) is tracked for sync's [file] rows
            _git(tree, "add", "-A", "-N")
        if applied.returncode:
            msg = f"{diff.name} does not apply on {made['rev'][:9]}: {applied.stderr.strip()[:200]}"
            return {"worktree": made}, 1, [{"key": "data.stamp", "blocking": True, "message": msg}]
        _phase("tree+apply", mark)
        same = made["rev"] == head  # the shared tree's objects are the clean build only at its own commit
        files = stamp_mod.files_of(text)
        if stamp_mod.extraction_files(files):
            extracted = (
                True  # the tree's asm now follows the diff's symbol rows: it is no clean tree for the next stamp
            )
            with setup_lock(shared):
                why = check_mod.reextract(
                    profile, tree, files, allow=reextract
                )  # --reextract: a splat.yaml edit pays the re-split
            if why:
                return {"worktree": made}, 1, [{"key": "data.extract", "blocking": True, "message": why}]
        if "Makefile" not in files and any(f in ("config.toml", "rom.toml") for f in files):
            # a project row (a file's flags, a folder) moved: the committed Makefile still keys them by the old path, so a
            # renamed unit compiled without its -ffast-math (Math_Sqrtf edit=9, 10-25). The train regenerates it after landing.
            from abu_decomp import tools

            with setup_lock(shared):
                regen = tools.invoke("unbake", profile, ["sync", "--makefile"], project=tree)
            if not regen.ok:
                msg = "unbake sync --makefile refused in the stamp tree: the project rows cannot be rendered into a Makefile"
                return {"worktree": made}, 1, [{"key": "data.stamp", "blocking": True, "message": msg}]
        _phase("extract+regen", mark)
        builder = trial.Builder(cfg, tree, reference=shared if same else None)
        check_mod.prepare(builder, list(cfg.versions))
        results = [check_mod.check(builder, v) for v in cfg.versions]
        _phase("check", mark)
        if os.environ.get("ABU_STAMP_ROM_SKIP", "1") == "1":
            builder.skip_headers = header_files(files)  # the check judged their includers; the link does not redo them
        if (
            same and _git(shared, "rev-parse", "HEAD").stdout.strip() != head
        ):  # the pool moved on: its objects are not ours
            builder = trial.Builder(cfg, tree)
            results = [check_mod.check(builder, v) for v in cfg.versions]
            _phase("check again", mark)
        findings = [
            {"key": "data.check", "blocking": True, "message": f"{r['version']}: {u} {fn} {why}"}
            for r in results for u, fn, why in r["broken"]
        ] + [
            {"key": "data.build", "blocking": True, "message": f"{r['version']}: {u} does not compile: {why}"}
            for r in results for u, why in r.get("failed", [])
        ]  # fmt: skip
        if not findings and results and all(r.get("units", 1) == 0 for r in results):
            findings = [
                {
                    "key": "data.check",
                    "blocking": True,
                    "message": "the check judged 0 units in every version, so it proves nothing: no stamp",
                }
            ]
        if not findings and all(r["built"] for r in results):
            # A stamp means the 5 ROMs match: the units built, now link each version and compare its hash (10-24: a stamped
            # diff broke the de ROM at the train, which links). Incremental: the objects are the ones just built.
            rc, report = builder.rom_check()
            _phase("rom_check", mark)
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
            release(shared, tree, before)
        print(
            f"stamp build: wall={wall:.0f}s cpu={used:.0f}s tree={lane} {'reused' if made.get('reused') else 'fresh'}"
            + ("  FLAG CPU-OVER-5min" if used > CPU_CAP else ""),
            flush=True,
        )
