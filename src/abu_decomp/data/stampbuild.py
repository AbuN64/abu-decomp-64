"""`decomp data stamp --diff X --build --tree SHARED`: build X alone on a warm stamp tree and stamp it.

A stamp used to need `data check` on a lane tree that held the diff, and a lane tree with several diffs rebuilt every unit
all of them changed (8 min wall, 1404 CPU-s with one header in it, 10-24). Here the diff gets its own `decomp worktree new`
tree (extraction, objects and times seeded from SHARED, so make rebuilds only what the diff reaches), `data check` judges
every version, a clean pass writes the stamp. The result carries wall and CPU seconds.

The trees are a POOL of K warm trees (STAMP-S0.., `stamp_trees` of the data config, default 4 on 12 cores; each ~6.6 GB
under the shared tree's own folder, never `wt/wt/`). A stamp LEASES one (a flock): a tree left clean at SHARED's commit
is reused (reset, diff applied, built incrementally, reverted), so a stamp pays no new 6.6 GB seed copy and concurrent
stamps are bounded by K, not one tree per stamp left behind by a killed run (10-24). A tree at another commit, or whose
last run did not finish (marker still "busy"), is RE-WARMED in place by one sync step from SHARED (`rewarm`: the
commit, the changed outputs and facts, `unbake setup --no-make`), never rebuilt cold.

A pack whose stamp is already held (content-keyed) re-reports what its build measured, so it can be re-proved."""

from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
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
SYNC_WORKERS = 4  # rsyncs of one re-warm side by side
STATE = ".stamp-state"  # in the tree: the commit it was left clean at, or "busy" while a stamp build owns it


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


def take_shared(path: Path) -> int | None:
    """The fd of a shared flock on PATH (any number of copiers), or None while an extraction holds it exclusively."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o664)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


@contextmanager
def lease(folder: Path, count: int, wait: float = 900.0, prefer: str | None = None) -> Iterator[str]:
    """The lane name of a pool tree nobody else holds (a flock the kernel drops with the process, a dead holder's lock
    broken by `take`) and no process still works in (busy_in), leased for the stamp; waits for one, then refuses
    naming the holders. PREFER: a free tree left clean at that commit is taken before the others (a build there is
    a no-op, in another tree it is the whole diff). One log line names the tree and the wait."""
    folder.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    end = start + wait
    while True:
        for i in sorted(range(count), key=lambda i: _left_at(folder / f"STAMP-S{i}") != prefer):
            fd = take(folder / f".stamp-{i}.lock")
            if fd is None:
                continue
            if busy_in(folder / f"STAMP-S{i}"):
                os.close(fd)
                continue
            print(f"stamp pool: leased STAMP-S{i} of {count} (waited {time.monotonic() - start:.0f}s)", flush=True)
            try:
                yield f"STAMP-S{i}"
            finally:
                os.close(fd)
                print(f"stamp pool: released STAMP-S{i}", flush=True)
            return
        if time.monotonic() > end:
            who = "; ".join(f"STAMP-S{i}: {describe(folder / f'.stamp-{i}.lock')}" for i in range(count))
            raise DecompError(
                "data.stamp",
                f"all {count} pool trees are leased after {wait:.0f}s (stamp_trees in the data config): {who}",
            )
        time.sleep(2)


def _left_at(tree: Path) -> str:
    marker = tree / STATE
    return marker.read_text().strip() if marker.is_file() else ""


@contextmanager
def setup_lock(shared: Path, wait: float = 900.0, exclusive: bool = True) -> Iterator[None]:
    """Extraction against copying: an extraction (`exclusive`) writes beside the shared tree (tools, caches) that the other
    slots' makes run from (10-24: a stamp's ROM check failed with no output while another diff was being extracted), so
    it runs alone; a stamp that only COPIES from the shared tree into its own pool tree (`rewarm`, a fresh tree) holds the
    lock shared, so those copies overlap each other and wait only for an extraction (10-27: one exclusive lock over every
    6.6 GB copy made a stamp wait 152-602 s for a lock while computing 26-92 s). Waits `wait` seconds, then refuses naming
    the holder."""
    folder = worktree.base(shared)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ".stamp-setup.lock"
    end = time.monotonic() + wait
    while (fd := (take(path) if exclusive else take_shared(path))) is None:
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


def sync_one(name: str, shared: Path, tree: Path) -> bool:
    """One synced name copied from SHARED into TREE (rsync sends only what differs): True when it succeeded."""
    src, dst = shared / name, tree / name
    args = ["rsync", "-a", "--delete", f"{src}/", f"{dst}/"] if src.is_dir() else ["rsync", "-a", str(src), str(dst)]
    dst.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.run(args, capture_output=True).returncode == 0


def sync_all(names: list[str], shared: Path, tree: Path) -> list[bool]:
    """The names synced side by side: each rsync walks its own folder, so the walks (stat-bound, 24-35 s one after the
    other) overlap instead of queueing."""
    if len(names) < 2:
        return [sync_one(n, shared, tree) for n in names]
    with ThreadPoolExecutor(min(len(names), SYNC_WORKERS)) as pool:
        return list(pool.map(lambda n: sync_one(n, shared, tree), names))


def rewarm(profile: Any, shared: Path, tree: Path, rev: str) -> dict[str, Any] | None:
    """The pool tree at REV again after ONE sync step from SHARED, for a tree left at another commit or busy (killed or
    failed run): the commit is checked out in place, the shared tree's outputs and facts are synced over it (rsync copies
    only what differs, so the 6.6 GB are not copied again), `unbake setup --no-make` and the pool marker follow, as for a
    new tree. None when it cannot (no tree, no rsync, a refusal): the caller then builds a fresh tree."""
    from unbake import treelock

    if not (tree / ".git").exists() or not shutil.which("rsync"):
        return None
    started, was = time.monotonic(), (tree / STATE).read_text().strip() if (tree / STATE).is_file() else "unknown"
    if any(
        _git(tree, *a).returncode
        for a in (("reset", "-q", "--hard"), ("clean", "-fdq"), ("checkout", "-q", "--detach", "-f", rev))
    ):
        return None
    names = [*worktree.COPIED, *(f".unbake/{f}" for f in worktree.FACTS)]
    with setup_lock(shared, exclusive=False), treelock.hold(shared, who=f"decomp data stamp rewarm {tree.name}"):
        if not all(sync_all([n for n in names if (shared / n).exists()], shared, tree)):
            return None
        aligned = worktree.align_times(shared, tree)
        from abu_decomp import tools

        if not tools.invoke("unbake", profile, ["setup", "--no-make"], project=tree).ok:
            return None
        worktree.mark(shared, tree, rev)
    print(
        f"stamp pool: re-warmed {tree.name} {was[:9]} -> {rev[:9]} by one sync in {time.monotonic() - started:.0f}s",
        flush=True,
    )
    return {"worktree": str(tree), "rev": rev, "reused": False, "rewarmed": True, "aligned": aligned}


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
    """(made, code, findings) for the slot's tree at REV (None: the branch tip): reused when it is clean, else fresh.
    REV is resolved to its full hash first: the markers and every `== HEAD` comparison hold full hashes, so a short
    `stamp --at` never matched the shared tree and every unit was scored without the clean reference tree (10-27, ~1 h)."""
    tree = worktree.base(shared) / lane
    if rev and (full := _git(shared, "rev-parse", "--verify", f"{rev}^{{commit}}").stdout.strip()):
        rev = full
    if rev and (made := reuse(profile, tree, rev)):
        return made, 0, []
    if rev and (made := rewarm(profile, shared, tree, rev)):
        return made, 0, []
    worktree.prune(shared, [lane])  # a leftover of an interrupted run, or a tree that could not be made current
    with setup_lock(shared, exclusive=False):  # copies from the shared tree, writes only the lane's own
        return worktree.new(profile, shared, lane, rev)


def warm_pool(cfg: DataConfig, profile: Any, shared: Path) -> list[dict[str, Any]]:
    """Every idle pool tree brought to the shared tree's commit and settled, in the background class, so the next stamp
    finds a clean tree at its base instead of seeding one that is dozens of commits behind (10-27: 36 behind, 713 CPU s).
    A tree a stamp leases (or any process works in) is not idle and is left alone; a tree already left at the commit, or
    one that cannot be re-warmed by one sync, is skipped (a stamp makes it fresh as before). One row per tree touched."""
    head = _git(shared, "rev-parse", "HEAD").stdout.strip()
    folder, done = worktree.base(shared), []
    for i in range(cfg.stamp_trees):
        tree = folder / f"STAMP-S{i}"
        if not head or not (tree / ".git").exists() or _left_at(tree) == head:
            continue
        fd = take(folder / f".stamp-{i}.lock")
        if fd is None:
            continue
        try:
            if busy_in(tree) or _left_at(tree) == head or not (made := rewarm(profile, shared, tree, head)):
                continue
            with trial.Builder(cfg, tree).holding("background"):
                why = settle(cfg, tree)
            if not why:
                release(shared, tree)
            done.append({"tree": tree.name, "rev": head, "rewarmed": made.get("rewarmed", False), "settled": not why})
            print(
                f"stamp pool: warmed {tree.name} at {head[:9]}" + (f" (settle failed: {why[:120]})" if why else ""),
                flush=True,
            )
        finally:
            os.close(fd)
    return done


@contextmanager
def at_rev(cfg: DataConfig, profile: Any, shared: Path, rev: str, wait: float = 900.0) -> Iterator[Path]:
    """A pool tree at REV for a caller that builds there without a diff (`cleanup count --rev`): leased like a stamp's,
    reused, re-warmed by one sync or fresh, marked busy while the caller works, clean and marked at REV again after."""
    from unbake import hdrnarrow

    with lease(worktree.base(shared), cfg.stamp_trees, wait, rev) as lane:
        made, code, findings = _tree(profile, shared, lane, rev)
        if code:
            why = "; ".join(f.get("message", "") for f in findings) or f"exit {code}"
            raise DecompError("data.stamp", f"no pool tree at {rev[:9]}: {why}"[:300])
        tree = Path(made["worktree"])
        before = dict(hdrnarrow.records(tree))
        (tree / STATE).write_text("busy\n")
        try:
            yield tree
        finally:
            release(shared, tree, before)


def missing_versions(cfg: DataConfig, shared: Path) -> list[str]:
    """Versions the project builds (versions/<v>/splat.yaml) that the data config does not list: a stamp run without the
    host profile and config checks only the default pseudo-version and passes (10-25: a false pass missed the eu build)."""
    found = sorted(p.parent.name for p in (shared / "versions").glob("*/splat.yaml"))
    return [v for v in found if v not in cfg.versions]


def msg_refusal(diff: Path) -> list[str]:
    """Why the diff's `# MSG:` subject would be refused at the train, before any build: the one rule is claim-preflight.sh's
    (`--msg`), the check claim.sh queues with, so a stamp or check never runs 10-20 min for a subject line. The script is
    the rig's; a host without it has nothing to check against and does not block."""
    script = Path.home() / "rig-state/bin/claim-preflight.sh"
    if not script.is_file():
        return []
    done = subprocess.run(["sh", str(script), "--msg", str(diff.resolve())], capture_output=True, text=True)
    return [line.strip() for line in done.stderr.splitlines() if line.strip()] if done.returncode == 1 else []


def build(
    cfg: DataConfig,
    profile: Any,
    shared: Path,
    diff: Path,
    stamps: Path,
    at: str | None = None,
    reextract: bool = False,
):
    """(result, code, findings). The stamp is written only when every version built and no exact function broke. A diff that
    is already stamped is built and measured again like any other: the stamp is content-keyed, so a held pack could never be
    re-proved or re-timed through a bare "present" (a caller that only wants the presence asks `stamp_mod.stamped`)."""
    text = diff.read_text(errors="replace")
    if not stamp_mod.touches_data(cfg, text):
        return {"stamp": "not needed: the diff touches no declaration or data"}, 0, []
    if held := stamp_mod.measured(stamps, text):
        print(
            f"stamp build: cpu={held['cpu']:.0f}s wall={held['wall']:.0f}s tree={held['tree']} held "
            f"(stamp {stamp_mod.path_of(stamps, text).name} measured at {str(held['rev'])[:9]}, not built again)",
            flush=True,
        )
        return {"stamp": str(stamp_mod.path_of(stamps, text)), "held": True, "measured": held}, 0, []
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
    with lease(worktree.base(shared), cfg.stamp_trees) as lane:
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


def skipped(r: dict) -> int:
    """Includers the narrowing proved the edit cannot reach: a check that rebuilt none of them still proved the diff."""
    return (r.get("narrowed") or {}).get("skipped", 0)


def header_files(files: list[str]) -> list[str]:
    """The headers among a diff's files (what `expand_units` counts as one), for the ROM link to treat as old."""
    return sorted(f for f in files if f.startswith("include/") or f.lower().endswith(".h"))


def closure_line(r: dict[str, Any]) -> str:
    """The rebuild of one version for a header diff, measured: the units that include a changed header (`closure`), the
    ones the narrowing proved the edit cannot reach (`skipped`, signed not rebuilt) and the units the check rebuilt, which
    are the closure less those plus the diff's own units. Empty for a diff with no header."""
    note = r.get("narrowed") or {}
    if not note.get("headers"):
        return ""
    closure, skipped, rebuilt = int(note["includers"]), int(note["skipped"]), int(r["units"])
    return (
        f"stamp closure: {r['version']} headers={note['headers']} closure={closure} skipped={skipped} rebuilt={rebuilt} "
        f"own={rebuilt - (closure - skipped)}"
    )


def _phase(name: str, since: list[float], cap: bool = True) -> None:
    """One line per stamp phase with its wall and CPU seconds (10-25: a stamp of 762-1434 CPU s named no phase)."""
    now = [time.monotonic(), meter.cpu()]
    used = now[1] - since[1]
    over = f"  FLAG CPU-OVER-5min step={name}" if cap and used > CPU_CAP else ""
    print(f"stamp phase {name}: wall={now[0] - since[0]:.0f}s cpu={used:.0f}s{over}", flush=True)
    since[:] = now


def settle(cfg: DataConfig, tree: Path) -> str | None:
    """Link the CLEAN tree (the diff not yet applied) so make has nothing left to catch up on; the reason it cannot, else
    None. The first make of a slot pays for more than the diff: stamps and objects the last diff's revert left stale, the
    dependency caches of a fresh or reset tree, a cold file cache. That debt ran inside rom_check and made equivalent
    declaration-only packs cost 24 s on one slot and 688 s on another (10-26), so the 300 s cap decided by cache luck.
    Paid here, outside the cap's meter (as is the seeding of a fresh tree), rom_check is the link of what the diff changed, the same cold or warm."""
    if not cfg.rom_check:
        return None
    rc, report = trial.Builder(cfg, tree).rom_check()
    return (
        f"the clean tree does not link before the diff is applied, so it is not the diff's fault: {report}"
        if rc
        else None
    )


def check_versions(builder: trial.Builder, versions: list[str]) -> list[dict[str, Any]]:
    """`check` of every version side by side inside the stamp's slots, which the stamp has already queued for once
    (`builder.holding`): as many makes as versions the slots allow, each at its share of the jobs, so the sum stays the
    stamp's weight. Five serial makes each paid a make start-up and, under load, its own queue trip (10-27: 68 s wall
    for 38 CPU s on a warm tree, 455 s for 47 on a cold one). Results come back in VERSIONS order."""
    width = builder.width()
    workers = max(1, min(len(versions), width))
    outer = type(builder).make_jobs
    type(builder).make_jobs = str(max(1, width // workers))
    try:
        if workers == 1:
            return [check_mod.check(builder, v) for v in versions]
        with ThreadPoolExecutor(workers) as pool:
            return list(pool.map(lambda v: check_mod.check(builder, v), versions))
    finally:
        type(builder).make_jobs = outer


def _build(cfg, profile, shared, diff, stamps, at, text, lane, reextract=False):
    started, cpu = time.monotonic(), meter.cpu()
    slots_held = contextlib.ExitStack()  # the stamp's host slots from the settle until the link is done
    mark = [started, cpu]
    # CPU and wall seconds the tree and `settle` used: shown on their own lines, not charged to the diff
    free = [0.0, 0.0]

    def settled(tree: Path) -> list[dict[str, Any]]:
        before, began = mark[1], mark[0]
        _phase("tree (not charged)", mark, cap=False)  # seeding a fresh tree is the slot's cost, not the diff's
        # ONE queue trip for the settle, the check and the link
        slots_held.enter_context(trial.Builder(cfg, tree).holding())
        # a leased tree whose asm marks differ from its configs (a sync moved it) is re-extracted before anything compiles
        why = None
        if check_mod.stale_extractions(cfg, tree):
            with setup_lock(shared):
                why = check_mod.refresh_extractions(cfg, profile, tree)
        why = why or settle(cfg, tree)
        _phase("settle (not charged)", mark, cap=False)
        free[0] += mark[1] - before
        free[1] += mark[0] - began
        return [{"key": "data.stamp", "blocking": True, "message": why}] if why else []

    # The tree is at SHARED's own commit: any other base differs in symbol facts and splits the ROM again for every
    # version (5 cold extractions, 70 s wall, 10-24). Only a diff that does not apply there goes to the tip.
    head = _git(shared, "rev-parse", "HEAD").stdout.strip()
    made, code, findings = _tree(profile, shared, lane, at or head or None)
    tree = Path(made["worktree"])
    extracted = kept = False
    wrote: dict[str, int] | None = (
        None  # the units per version of a stamp written: its measured record is completed below
    )
    from unbake import hdrnarrow

    before = {} if code else dict(hdrnarrow.records(tree))  # the stamp records the clean slot holds, for `restore`
    try:
        if code:
            return {"worktree": made}, code, findings
        (tree / STATE).write_text("busy\n")
        if found := settled(tree):
            return {"worktree": made}, 1, found
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
            if found := settled(tree):
                return {"worktree": made}, 1, found
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
        _phase("apply", mark)
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
        slots_held.enter_context(builder.holding())  # ONE queue trip for the check and the link
        results = check_versions(builder, list(cfg.versions))
        _phase("check", mark)
        if refused := [r for r in results if r.get("refused")]:  # a build failure: no scoring, no second check, no link
            msg = "; ".join(f"{r['version']}: {r['errors'][0]}" for r in refused)
            return {"worktree": made}, 1, [{"key": "data.build", "blocking": True, "message": msg}]
        for line in filter(None, map(closure_line, results)):
            print(line, flush=True)
        if os.environ.get("ABU_STAMP_ROM_SKIP", "1") == "1":
            builder.skip_headers = header_files(files)  # the check judged their includers; the link does not redo them
        if (
            same and _git(shared, "rev-parse", "HEAD").stdout.strip() != head
        ):  # the pool moved on: its objects are not ours
            slots_held.close()
            builder = trial.Builder(cfg, tree)
            slots_held.enter_context(builder.holding())
            results = check_versions(builder, list(cfg.versions))
            _phase("check again", mark)
        findings = [
            {"key": "data.check", "blocking": True, "message": f"{r['version']}: {u} {fn} {why}"}
            for r in results for u, fn, why in r["broken"]
        ] + [
            {"key": "data.build", "blocking": True, "message": f"{r['version']}: {u} does not compile: {why}"}
            for r in results for u, why in r.get("failed", [])
        ] + [
            {"key": "data.build", "blocking": True, "message": f"{r['version']}: not built: {'; '.join(r['errors'])}"}
            for r in results if not r["built"] and not r.get("failed")
        ]  # fmt: skip
        # units == 0 proves nothing unless the narrowing proved the includers unreachable (skipped): a dead declaration
        if not findings and results and all(r.get("units", 1) == 0 and not skipped(r) for r in results):
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
            wrote = {r["version"]: r.get("units", 0) for r in results}
        else:
            result["stamp"] = "not stamped: the check failed"
        return result, 1 if findings else 0, findings
    finally:
        slots_held.close()
        wall, used = time.monotonic() - started, meter.cpu() - cpu - free[0]
        if kept and (tree / ".git").exists():
            (tree / STATE).write_text("busy\n")  # not reusable; the next stamp in this slot rebuilds it
        elif extracted:
            worktree.prune(shared, [tree.name])
        elif (tree / ".git").exists():
            release(shared, tree, before)
        if wrote is not None:  # the cost the line below shows: a held pack re-reports it (`measured`)
            record = {"cpu": round(used, 1), "wall": round(wall, 1), "tree": lane, "rev": made["rev"], "units": wrote}
            stamp_mod.write(stamps, text, measured=record)
        print(
            f"stamp build: wall={wall:.0f}s cpu={used:.0f}s tree={lane} {'reused' if made.get('reused') else 'fresh'}"
            + ("  FLAG CPU-OVER-5min" if used > CPU_CAP else "")
            + (f"  (uncharged tree+settle: wall={free[1]:.0f}s cpu={free[0]:.0f}s)" if free[1] else ""),
            flush=True,
        )
