"""The rig's scheduled reap: idle browser trees and ended lanes' worktrees go, live lanes stay.

`decomp worktree reap` removes (1) every orphaned chromium tree that used no CPU over a sample window and is older
than the seat's minimum age, and (2) every lane worktree no process stands in, no queued ship names and nothing has
touched for the seat's idle hours. `enable` installs it as a systemd user timer, `disable` removes that. All figures
(idle hours, sample seconds, minimum age, interval) are the seat profile's `land.reap`; none is a default here."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, worktree
from abu_decomp.config import Profile

UNIT = "decomp-reap"
TERM_GRACE = 5.0  # seconds between SIGTERM and SIGKILL of a reaped tree
KEYS = (
    "worktree_idle_hours",
    "chromium_sample_seconds",
    "chromium_min_age_minutes",
    "chromium_idle_cpu_percent",
    "interval_minutes",
)


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    comm: str
    args: str
    cwd: str
    ticks: int  # utime + stime
    age: float  # seconds since the process started


def settings(profile: Profile) -> dict[str, float]:
    section = profile.section("land").get("reap")
    missing = [k for k in KEYS if not isinstance(section, dict) or k not in section]
    if missing:
        raise DecompError("reap.config", f"seat profile land.reap lacks {', '.join(missing)}")
    return {k: float(section[k]) for k in KEYS}


def places(profile: Profile) -> dict[str, Any]:
    """The optional `land.reap` places: `ended_results` (lanes' RESULT.md folder), `lane_scratch` (folders a
    lane leaves files in, by lane name) and `salvage` (where an ended tree's uncommitted work is saved first)."""
    section = profile.section("land").get("reap") or {}
    expand = lambda value: Path(str(value)).expanduser()  # noqa: E731
    return {
        "results": expand(section["ended_results"]) if section.get("ended_results") else None,
        "scratch": [expand(v) for v in section.get("lane_scratch", [])],
        "salvage": expand(section["salvage"]) if section.get("salvage") else None,
    }


def snapshot(proc: Path = Path("/proc")) -> list[Proc]:
    """Every process the caller can read."""
    hertz = os.sysconf("SC_CLK_TCK")
    # the boot time, not /proc/uptime: a container's uptime is its own while a process start is counted from the host's
    boot = next(int(line.split()[1]) for line in (proc / "stat").read_text().splitlines() if line.startswith("btime"))
    now = time.time()
    out = []
    for entry in proc.glob("[0-9]*"):
        try:
            stat = (entry / "stat").read_text()
            comm = stat[stat.index("(") + 1 : stat.rindex(")")]
            rest = stat[stat.rindex(")") + 2 :].split()  # field 3 (state) is rest[0]
            args = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
            try:
                cwd = os.readlink(entry / "cwd")
            except OSError:
                cwd = ""
            out.append(
                Proc(
                    int(entry.name),
                    int(rest[1]),
                    comm,
                    args,
                    cwd,
                    int(rest[11]) + int(rest[12]),
                    now - (boot + int(rest[19]) / hertz),
                )
            )
        except (OSError, ValueError, IndexError):
            continue
    return out


def _tree(root: int, procs: list[Proc]) -> list[Proc]:
    kids: dict[int, list[Proc]] = {}
    for p in procs:
        kids.setdefault(p.ppid, []).append(p)
    by_pid = {p.pid: p for p in procs}
    out, todo = [], [by_pid[root]]
    while todo:
        p = todo.pop()
        out.append(p)
        todo.extend(kids.get(p.pid, []))
    return out


def _is_browser(p: Proc) -> bool:
    return p.comm.startswith(("chrom", "headless_shell")) or "/chromium" in p.args.split(" ", 1)[0]


def idle_browsers(
    before: list[Proc], after: list[Proc], min_age: float, seconds: float, max_cpu_percent: float
) -> list[list[Proc]]:
    """Orphaned browser trees (the root has no `--type=` and its parent is init or the user manager) whose whole
    tree used at most MAX_CPU_PERCENT of one core over the SECONDS between the two snapshots, started no process in
    it, and whose root is at least MIN_AGE seconds old."""
    names = {p.pid: p.comm for p in after}
    old = {p.pid: p.ticks for p in before}
    allowed = max_cpu_percent / 100 * seconds * os.sysconf("SC_CLK_TCK")
    found = []
    for root in after:
        if not _is_browser(root) or "--type=" in root.args or "crashpad" in root.comm or root.age < min_age:
            continue
        if root.ppid != 1 and names.get(root.ppid) != "systemd":
            continue
        members = _tree(root.pid, after)
        # a process missing from the first snapshot started inside the window: the tree is not idle
        if all(p.pid in old for p in members) and sum(p.ticks - old[p.pid] for p in members) <= allowed:
            found.append(members)
    return found


def _holds(path: Path, procs: list[Proc], me: int) -> bool:
    text = str(path)
    return any(
        p.pid != me and (p.cwd == text or p.cwd.startswith(text + "/") or text + "/" in p.args or p.args.endswith(text))
        for p in procs
    )


def _touched(path: Path) -> float:
    """The newest of the worktree folder, its git index and HEAD: a lane that is committing or staging moves one."""
    stamps = [path.stat().st_mtime]
    gitfile = path / ".git"
    if gitfile.is_file():
        head = gitfile.read_text().strip().removeprefix("gitdir:").strip()
        for name in ("index", "HEAD"):
            target = Path(head) / name
            if target.exists():
                stamps.append(target.stat().st_mtime)
    return max(stamps)


def stale_worktrees(shared: Path, procs: list[Proc], idle_hours: float, now: float, me: int) -> list[str]:
    folder = worktree.base(shared)
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(p for p in folder.iterdir() if p.is_dir()):
        if now - _touched(path) < idle_hours * 3600 or _holds(path, procs, me) or worktree._referenced(shared, path):
            continue
        out.append(path.name)
    return out


def _kill_trees(trees: list[list[Proc]], kill: Callable[[int, int], None], sleep: Callable[[float], None]) -> None:
    pids = [p.pid for tree in trees for p in tree]
    for sig, wait in ((signal.SIGTERM, TERM_GRACE), (signal.SIGKILL, 0.0)):
        for pid in pids:
            with contextlib.suppress(ProcessLookupError):
                kill(pid, sig)
        sleep(wait)


def run(
    profile: Profile,
    shared: Path,
    *,
    dry_run: bool = False,
    read: Callable[[], list[Proc]] = snapshot,
    kill: Callable[[int, int], None] = os.kill,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    """Reap what is idle; with DRY_RUN only report it. Returns the result document the CLI prints."""
    cfg = settings(profile)
    shared = shared.expanduser().resolve()
    before = read()
    sleep(cfg["chromium_sample_seconds"])
    after = read()
    trees = idle_browsers(
        before,
        after,
        cfg["chromium_min_age_minutes"] * 60,
        cfg["chromium_sample_seconds"],
        cfg["chromium_idle_cpu_percent"],
    )
    where = places(profile)
    ended = sorted(worktree.ended_lanes(where["results"])) if where["results"] else []
    stale = sorted(set(stale_worktrees(shared, after, cfg["worktree_idle_hours"], now(), os.getpid())) | set(ended))
    if not dry_run:
        _kill_trees(trees, kill, sleep)
    gone: dict[str, Any] = {"removed": [], "kept": [], "freed_mb": 0}
    if stale and not dry_run:
        gone = worktree.prune(shared, stale, None, where["scratch"], where["salvage"])[0]
    result = {
        "dry_run": dry_run,
        "chromium": {"trees": len(trees), "processes": sum(len(t) for t in trees)},
        "worktrees": {"stale": stale, **({} if dry_run else gone)},
    }
    return result, 0, []


def _unit_dir() -> Path:
    return Path.home() / ".config/systemd/user"


def _systemctl(*args: str) -> None:
    done = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=False)
    if done.returncode:
        raise DecompError("reap.schedule", f"systemctl --user {' '.join(args)}: {done.stderr.strip()[:200]}")


def _interpreter() -> str:
    """The python the timer runs: the venv's stable symlink when this venv is its current target (a deploy switches the
    link and prunes the versioned directory this process runs from), else this interpreter."""
    here = Path(sys.prefix).resolve()
    link = Path.home() / "venv-decomp"
    return str(link / "bin/python") if link.exists() and link.resolve() == here else sys.executable


def enable(
    profile: Profile, shared: Path, unit_dir: Path | None = None
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    """Install and start the reap timer for this seat (a systemd user timer; lingering keeps it across logouts)."""
    cfg = settings(profile)
    folder = unit_dir or _unit_dir()
    folder.mkdir(parents=True, exist_ok=True)
    tree = shared.expanduser().resolve()
    command = f"{_interpreter()} -m abu_decomp --profile {profile.source_file} worktree --tree {tree} reap"
    minutes = int(cfg["interval_minutes"])
    (folder / f"{UNIT}.service").write_text(
        f"[Unit]\nDescription=decomp reap: idle chromium and ended lanes' worktrees\n\n"
        f"[Service]\nType=oneshot\nNice=10\nExecStart={command}\n"
    )
    (folder / f"{UNIT}.timer").write_text(
        f"[Unit]\nDescription=decomp reap every {minutes} min\n\n"
        f"[Timer]\nOnBootSec={minutes}min\nOnUnitActiveSec={minutes}min\n\n[Install]\nWantedBy=timers.target\n"
    )
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", f"{UNIT}.timer")
    return {"enabled": f"{UNIT}.timer", "every_minutes": minutes, "command": command}, 0, []


def disable(unit_dir: Path | None = None) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    folder = unit_dir or _unit_dir()
    _systemctl("disable", "--now", f"{UNIT}.timer")
    for suffix in ("service", "timer"):
        (folder / f"{UNIT}.{suffix}").unlink(missing_ok=True)
    _systemctl("daemon-reload")
    return {"disabled": f"{UNIT}.timer"}, 0, []
