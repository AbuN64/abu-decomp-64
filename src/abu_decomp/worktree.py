"""`decomp worktree new LANE`: a lane's own work tree, warm from the shared tree (10-22: a fresh worktree paid ~970
CPU-s of preprocessing and setup before its first build, over the 5-minute cap).

A new git worktree has no extraction, no objects and fresh source times, so make preprocesses every unit again. The
seed makes it what the shared tree is at the same commit: the extraction, the objects, the preprocessed texts and the
unbake facts are copied under the shared tree's lock held shared (`unbake setup` holds it exclusively while it extracts,
and make never remakes an extraction: 10-25 a seed copied during a pool refresh lacked build/de/undefined_syms_auto.txt
and every link of the stamp failed; a build output make rewrote during the copy is dropped and the worktree's own
build makes it again), and every
tracked file whose bytes equal the shared tree's file gets that file's time, so make finds the copied outputs current
and rebuilds only what the commit changed. The worktree keeps its own `.unbake` state: recipe leases, tries, the
ledger and the inbox never touch the shared tree's. A lane lands only through `decomp ship --tree SHARED --patch`."""

from __future__ import annotations

import contextlib
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


def base(shared: Path) -> Path:
    """The folder lane trees live in: `<shared>/../wt`, or the shared tree's own folder when it already is one (a shared
    tree under `wt/`, like the pool's seed, used to nest its lanes in `wt/wt/`)."""
    shared = shared.expanduser().resolve()
    return shared.parent if shared.parent.name == "wt" else shared.parent / "wt"


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


def align_times(shared: Path, tree: Path, names: list[str] | None = None) -> int:
    """Every tracked file of `tree` (or each of NAMES) whose bytes equal the shared tree's file takes that file's
    modification time."""
    count = 0
    for name in _git(tree, "ls-files", "-z").split("\0") if names is None else names:
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
    """Copy the shared tree's outputs and facts (the caller holds its tree lock shared, so no extraction runs). A file a
    build rewrote while it was copied (size or time differs from the source afterwards) is deleted from the copy, and
    the worktree's own warm build makes it again. Returns files dropped."""
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


def seed_locked(shared: Path, target: Path, lane: str) -> int:
    """`seed` under the shared tree's lock held shared: no extraction is half-written while the files are copied."""
    with treelock.hold(shared, who=f"decomp worktree seed {lane}"):
        return seed(shared, target)


def mark(shared: Path, target: Path, rev: str) -> str:
    """The pool's marker, the way `pool-mint.sh` writes it (`build/.pool-extracted-at`: the commit, written once the
    extraction is current), so `hdrnarrow` narrows a header check on this tree. The marker's time is the instant from
    which a unit's `.i.ok` counts as rebuilt since the base: the seed holds the base's objects only when the shared tree
    is clean at the same tree content as REV, else the time is 0 and no unit is taken as untouched (a narrowing that
    keeps every includer, never one that skips a stale unit). A built marker the seed copied from a shared tree at other
    content is dropped (it would make `hdrnarrow` read a base this tree is not). Returns the marker's path."""
    from unbake.hdrnarrow import BUILT_MARKER, EXTRACTED_MARKER

    def tree_id(root: Path, what: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(root), "rev-parse", f"{what}^{{tree}}"], capture_output=True, text=True, check=False
        )
        return done.stdout.strip() if done.returncode == 0 else ""

    clean = not _git(shared, "status", "--porcelain", "--untracked-files=no").strip()
    exact = clean and tree_id(shared, "HEAD") != "" and tree_id(shared, "HEAD") == tree_id(target, rev)
    built = target / BUILT_MARKER
    with contextlib.suppress(OSError):
        if built.exists() and (not exact or built.read_text().strip() != rev):
            built.unlink()
    path = target / EXTRACTED_MARKER
    path.parent.mkdir(exist_ok=True)
    path.write_text(rev + "\n")
    if not exact:
        os.utime(path, (0, 0))
    return str(path)


def new(
    profile: Profile, shared: Path, lane: str, rev: str | None = None
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    started, cpu = time.monotonic(), meter.cpu()
    shared = shared.expanduser().resolve()
    target = base(shared) / lane
    if target.exists():
        raise WorktreeError("worktree.exists", f"{target} exists: one worktree per lane")
    table = land.settings(profile, shared)
    if rev is None:
        _git(shared, "fetch", "-q", table["remote"], table["branch"])
        rev = _git(shared, "rev-parse", "--verify", "FETCH_HEAD^{commit}").strip()
    target.parent.mkdir(parents=True, exist_ok=True)
    _git(shared, "worktree", "add", "--detach", "-q", str(target), rev)
    epoch = treelock.epoch(shared)
    dropped = seed_locked(shared, target, lane)
    aligned = align_times(shared, target)
    if treelock.epoch(shared) != epoch:
        print("worktree.seed: a ship ended while the outputs were copied; torn files were dropped", flush=True)
    called = tools.invoke("unbake", profile, ["setup", "--no-make"], project=target)
    marker = mark(shared, target, rev) if called.ok else None
    result = {
        "worktree": str(target),
        "rev": rev,
        "aligned": aligned,
        "dropped": dropped,
        "setup_ok": called.ok,
        "marker": marker,
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


def lane_tree(profile: Profile, shared: Path, lane: str, rev: str) -> tuple[Path, str]:
    """The worktree of LANE at REV and the commit REV names: made by `new` when absent, reused when it already stands
    at that commit (a rerun is warm), refused when it stands anywhere else."""
    sha = _git(shared, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()
    target = base(shared) / lane
    if not target.exists():
        _result, code, found = new(profile, shared, lane, sha)
        if code:
            raise WorktreeError("worktree.setup", f"{target}: {found[0]['message'] if found else 'setup refused'}")
    elif _git(target, "rev-parse", "HEAD").strip() != sha:
        raise WorktreeError("worktree.exists", f"{target} stands at another revision than {sha}: prune it first")
    return target, sha


TOOL_TREES = (
    "decomp",
    "unbake",
    "recipe",
    "taste",
)  # the tool-repo worktrees a lane gets beside its own (pool-mint --release)


def ended_lanes(results: Path) -> set[str]:
    """The lanes under RESULT.md folders `results` whose card is over: a first line DONE. CONTINUE, BLOCKED and PARTIAL
    are read by a successor card, which needs the tree and the scratch the HANDOFF names."""
    out = set()
    for path in results.glob("*/RESULT.md"):
        with contextlib.suppress(OSError):
            if path.read_text(errors="replace").lstrip().startswith("DONE"):
                out.add(path.parent.name)
    return out


def _claims(entry: str, lane: str) -> bool:
    return entry == lane or (entry.startswith(lane) and entry[len(lane)] in ".-")


def lane_scratch(root: Path, lane: str) -> list[Path]:
    """What LANE left under ROOT: its folder, its tool-tree folders, and its loose logs and diffs (`LANE.log`,
    `LANE-build.log`) unless a longer sibling folder name owns them (`LANE-x` is another lane's folder)."""
    if not root.is_dir():
        return []
    entries = list(root.iterdir())
    longer = [e.name for e in entries if e.is_dir() and len(e.name) > len(lane) and _claims(e.name, lane)]
    out = []
    for entry in entries:
        if entry.is_dir():
            if entry.name == lane or entry.name in {f"{lane}-{t}" for t in TOOL_TREES}:
                out.append(entry)
        elif _claims(entry.name, lane) and not any(_claims(entry.name, other) for other in longer):
            out.append(entry)
    return out


def _live_paths() -> list[str]:
    """Where every process stands or has files open, and what it was started with: one scan of /proc."""
    out = []
    for proc in Path("/proc").glob("[0-9]*"):
        if proc.name == str(os.getpid()):
            continue
        with contextlib.suppress(OSError):
            out.append(os.readlink(proc / "cwd"))
        with contextlib.suppress(OSError):
            out.append((proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace"))
        with contextlib.suppress(OSError):
            for fd in (proc / "fd").iterdir():
                with contextlib.suppress(OSError):
                    out.append(os.readlink(fd))
    return out


def _busy(path: Path, live: list[str]) -> bool:
    text = str(path)
    return any(
        item == text or item.startswith(text + "/") or text + "/" in item or item.endswith(text) for item in live
    )


def _salvage(tree: Path, folder: Path) -> Path | None:
    """The uncommitted work of a tree that is about to go, as a patch (build outputs left out): a kept product."""
    with contextlib.suppress(WorktreeError, OSError):
        parts = [_git(tree, "diff", "HEAD")]
        for name in _git(tree, "ls-files", "--others", "--exclude-standard", "--", ".", ":!build", ":!.unbake").split(
            "\n"
        ):
            if name:
                done = subprocess.run(
                    ["git", "-C", str(tree), "diff", "--no-index", "/dev/null", name], capture_output=True, text=True
                )
                parts.append(done.stdout)
        text = "".join(parts)
        if text.strip():
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{tree.name}.patch"
            target.write_text(text)
            return target
    return None


def prune(
    shared: Path,
    ended: list[str],
    results: Path | None = None,
    scratch: list[Path] | None = None,
    salvage: Path | None = None,
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    """Remove what ended lanes left: the names given, and every lane with a RESULT.md saying DONE under `results`.
    Each lane's worktree goes, with its tool-tree worktrees, and its folders and loose files under every `scratch` root.
    A worktree a queued ship still names, or any path a live process stands in or has open, is kept and reported. A
    tree with uncommitted work leaves it as a patch under `salvage` first. The disk freed is the result's `freed_mb`."""
    shared = shared.expanduser().resolve()
    folder = base(shared)
    names = set(ended)
    if results is not None:
        names |= ended_lanes(results)
    removed, kept, saved = [], [], []
    probe = folder if folder.is_dir() else Path("/")
    before = shutil.disk_usage(probe).free
    live = _live_paths()
    for name in sorted(names):
        trees = [folder / name] + [folder / f"{name}-{t}" for t in TOOL_TREES]
        for path in (t for t in trees if t.is_dir()):
            if why := _referenced(shared, path) or (["a live process uses it"] if _busy(path, live) else []):
                kept.append({"worktree": str(path), "why": why})
                continue
            if salvage and (patch := _salvage(path, salvage)):
                saved.append(str(patch))
            try:
                _git(shared, "worktree", "remove", "--force", str(path))
            except (
                WorktreeError
            ):  # a half-removed tree (git forgot it, its files stay) would refuse every later prune: delete it
                shutil.rmtree(path, ignore_errors=True)
            removed.append(str(path))
        for root in scratch or []:
            for path in lane_scratch(root.expanduser(), name):
                if _busy(path, live):
                    kept.append({"worktree": str(path), "why": ["a live process uses it"]})
                    continue
                shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
                removed.append(str(path))
    _git(shared, "worktree", "prune")
    freed = (shutil.disk_usage(probe).free - before) // 2**20
    findings = [
        {"key": "worktree.referenced", "message": f"{k['worktree']} kept: {', '.join(k['why'])}", "blocking": False}
        for k in kept
    ]
    return {"removed": removed, "kept": kept, "salvaged": saved, "freed_mb": freed}, 0, findings


MAX_BEHIND = 25  # commits: the pool is refreshed after each landing (~20 an hour), so more means it was not


def behind(tree: Path) -> int | None:
    """Commits the remote main has that the tree HEAD lacks (None: no remote main ref or no git tree). A seed or
    evidence tree 899 commits behind failed `data stamp --build` twice and the member gate read stale asm
    (10-24 REV1-ONE-24)."""
    import subprocess

    refs = subprocess.run(
        ["git", "-C", str(tree), "for-each-ref", "--format=%(refname:short)", "refs/remotes/*/main"],
        capture_output=True,
        text=True,
    ).stdout.split()
    if not refs:
        return None
    ref = "github/main" if "github/main" in refs else refs[0]
    cmd = ["git", "-C", str(tree), "rev-list", "--count", f"HEAD..{ref}"]
    out = subprocess.run(cmd, capture_output=True, text=True)
    return int(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip().isdigit() else None
