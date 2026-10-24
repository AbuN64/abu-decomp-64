"""The host's one slot pool: every heavy process (tas play, oracle, behave replay, coverage union, port builds) holds
slots while it runs, so many lanes' worth of work queues instead of oversubscribing the cores.

A slot is one flock'd file `slot-NN` in the pool folder (`$ABU_SLOTS_DIR`, default ~/rig-state/slots). The kernel drops
the lock when the holder dies, so a crashed job never leaks a slot. Capacity is derived from the host, never from a
lane count: cores minus a reserve, and memory (total minus a floor, over a measured per-slot RSS); a job also waits
while free memory is below the floor plus its own need. Waiters write a ticket (`tickets/<class>-<time>-<pid>`) and
sleep on inotify; releases and ticket changes wake them (a 5 s timeout only covers a holder that crashed, which fires
no event). Classes, best first: owner (the owner's watched play) > interactive (a lane's single play) > batch (play
--all, monkey, behave) > background (coverage union, cov-refresh). Owner work never waits behind queued work and may
use the reserve slots above the soft capacity, so it always gets one. A heavy ticket at the head of the queue blocks
smaller later ones only after it has waited BACKFILL seconds (until then they backfill the free slots).

    with slots.hold("batch", weight=1, label="play boot"): ...        # in-process (re-entrant per thread)
    python3 slots.py run --class background --weight 4 --label cov -- cmd ...   # SLOT_JOBS=<weight> for make -j
    python3 slots.py status | free | capacity
"""

from __future__ import annotations

import contextlib
import ctypes
import fcntl
import json
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

CLASSES = ("owner", "interactive", "batch", "background")
RESERVE_CORES = float(os.environ.get("ABU_SLOT_RESERVE", "2"))  # cores left for the OS, the page and ssh
MEM_FLOOR_GIB = float(os.environ.get("ABU_SLOT_MEM_FLOOR", "6"))  # never admit below this much free memory
RSS_GIB = float(os.environ.get("ABU_SLOT_RSS_GIB", "0.75"))  # measured per-slot RSS (a port play ~0.25, a compile ~1)
BACKFILL = float(os.environ.get("ABU_SLOT_BACKFILL", "60"))
WAKE_TIMEOUT = 5.0
_local = threading.local()
CANCEL = None  # a callable that raises when the caller was told to stop (tas sets it): checked on every wake


def pool_dir() -> Path:
    return Path(os.environ.get("ABU_SLOTS_DIR") or Path.home() / "rig-state/slots")


def _mem_gib(field: str) -> float:
    with open("/proc/meminfo") as handle:
        for line in handle:
            if line.startswith(field + ":"):
                return int(line.split()[1]) / 1048576
    return 0.0


def cores() -> int:
    return os.cpu_count() or 1


def capacity() -> int:
    """Soft slot count for non-owner work: min(cores - reserve, (total memory - floor) / per-slot RSS), at least 1."""
    by_cpu = int(cores() - RESERVE_CORES)
    by_mem = int((_mem_gib("MemTotal") - MEM_FLOOR_GIB) / RSS_GIB)
    return max(1, min(by_cpu, by_mem))


def _paths() -> tuple[Path, Path]:
    root = pool_dir()
    tickets = root / "tickets"
    tickets.mkdir(parents=True, exist_ok=True)
    return root, tickets


def _try_lock(path: Path, create: bool = True):
    """An open fd holding the lock on `path`, or None when somebody else holds it (or it is gone and not `create`)."""
    try:
        fd = os.open(path, os.O_RDWR | (os.O_CREAT if create else 0) | os.O_CLOEXEC, 0o644)
    except FileNotFoundError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _held(path: Path, create: bool = True) -> bool:
    fd = _try_lock(path, create)
    if fd is None:
        return path.exists()
    os.close(fd)
    return False


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return {}


def _live_tickets(tickets: Path) -> list[tuple[str, dict]]:
    """Tickets whose owner is alive (an owner holds a flock on its ticket; a dead one's ticket is removed)."""
    out = []
    for path in sorted(tickets.iterdir()):
        if path.name.startswith("."):
            continue
        if not _held(path, False):
            path.unlink(missing_ok=True)
            continue
        out.append((path.name, _read(path)))
    return out


def _slot_files(root: Path) -> list[Path]:
    return [root / f"slot-{i:02d}" for i in range(cores())]


def free_count(limit: int | None = None) -> int:
    root, _ = _paths()
    files = _slot_files(root)[: limit if limit is not None else capacity()]
    return sum(1 for f in files if not _held(f))


def _wake(root: Path) -> None:
    with contextlib.suppress(OSError):
        (root / ".wake").write_text(str(time.time()))


class _Wait:
    """Sleep until the pool folder changes (a release or a ticket event) or the timeout; no polling."""

    def __init__(self, root: Path, tickets: Path) -> None:
        self.fd = -1
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            mask = 0x2 | 0x8 | 0x40 | 0x80 | 0x100 | 0x200  # modify close_write moved_from moved_to create delete
            for folder in (root, tickets):
                libc.inotify_add_watch(self.fd, os.fsencode(folder), mask)
        except (OSError, AttributeError):
            self.fd = -1

    def sleep(self, seconds: float) -> None:
        if self.fd < 0:
            time.sleep(min(seconds, 1.0))
            return
        ready, _, _ = select.select([self.fd], [], [], seconds)
        if ready:
            with contextlib.suppress(OSError):
                os.read(self.fd, 65536)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)


class Grant:
    def __init__(self, fds: list[int], files: list[Path], root: Path, waited: float, info: dict) -> None:
        self.fds, self.files, self.root, self.waited, self.info = fds, files, root, waited, info
        self.weight = len(fds)

    def release(self) -> None:
        for fd in self.fds:
            with contextlib.suppress(OSError):
                os.ftruncate(fd, 0)
            os.close(fd)
        self.fds = []
        _wake(self.root)


def _take(root: Path, weight: int, limit: int) -> list[tuple[int, Path]] | None:
    got: list[tuple[int, Path]] = []
    for path in _slot_files(root)[:limit]:
        fd = _try_lock(path)
        if fd is not None:
            got.append((fd, path))
            if len(got) == weight:
                return got
    for fd, _ in got:
        os.close(fd)
    return None


def _can_run(weight: int, limit: int, rank: int) -> bool:
    if _mem_gib("MemAvailable") < MEM_FLOOR_GIB + RSS_GIB * weight and rank > 0:
        return False
    return free_count(limit) >= weight


def acquire(klass: str = "batch", weight: int = 1, label: str = "", timeout: float | None = None) -> Grant:
    rank = CLASSES.index(klass)
    weight = max(1, min(int(weight), cores()))
    root, tickets = _paths()
    soft = capacity()
    weight = min(weight, soft if rank else cores())
    limit = soft if rank else cores()
    began = time.time()
    name = f"{rank}-{time.time_ns():020d}-{os.getpid()}-{threading.get_ident() % 100000}"
    ticket = tickets / name
    info = {
        "class": klass,
        "weight": weight,
        "label": label,
        "pid": os.getpid(),
        "queued": began,
        "cmd": " ".join(sys.argv[:4])[:120],
    }
    tfd = os.open(ticket, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    fcntl.flock(tfd, fcntl.LOCK_EX)
    os.write(tfd, json.dumps(info).encode())
    waiter = _Wait(root, tickets)
    try:
        while True:
            ahead = [(n, t) for n, t in _live_tickets(tickets) if n < name]
            blocked = False
            for n, t in ahead:
                # an earlier ticket blocks us unless it cannot run now and has not waited past BACKFILL
                older = time.time() - t.get("queued", time.time())
                fits = _can_run(int(t.get("weight", 1)), soft if not n.startswith("0-") else cores(), int(n[0]))
                if fits or older > BACKFILL:
                    blocked = True
                    break
            if not blocked and _can_run(weight, limit, rank):
                got = _take(root, weight, limit)
                if got:
                    info.update(since=time.time(), waited=round(time.time() - began, 2))
                    for fd, _ in got:
                        os.ftruncate(fd, 0)
                        os.pwrite(fd, json.dumps(info).encode(), 0)
                    return Grant([fd for fd, _ in got], [p for _, p in got], root, time.time() - began, info)
            if CANCEL:
                CANCEL()
            if timeout is not None and time.time() - began > timeout:
                raise TimeoutError(f"no {weight} slots for {klass} within {timeout:.0f}s")
            waiter.sleep(WAKE_TIMEOUT)
    finally:
        waiter.close()
        os.close(tfd)
        ticket.unlink(missing_ok=True)
        _wake(root)


def klass_from_env(default: str) -> str:
    value = os.environ.get("ABU_SLOT_CLASS", default)
    return value if value in CLASSES else default


@contextlib.contextmanager
def hold(klass: str = "batch", weight: int = 1, label: str = "", timeout: float | None = None):
    """Hold `weight` slots while the block runs. Re-entrant per thread and per process tree (ABU_SLOT_HELD): a play
    that runs its coverage step inside the block does not queue a second time, so a full pool cannot deadlock itself."""
    if getattr(_local, "depth", 0) or os.environ.get("ABU_SLOT_HELD") or os.environ.get("ABU_SLOTS_OFF"):
        yield None
        return
    grant = acquire(klass_from_env(klass), weight, label, timeout)
    _local.depth = 1
    try:
        yield grant
    finally:
        _local.depth = 0
        grant.release()


def admitted(klass: str, weight: int = 1):
    """Decorator: run the function while holding `weight` slots of `klass` (label = the function's name)."""
    import functools

    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            with hold(klass, weight, fn.__name__):
                return fn(*args, **kwargs)

        return inner

    return wrap


def status() -> dict:
    root, tickets = _paths()
    held: dict[str, dict] = {}
    for path in _slot_files(root):
        if _held(path):
            info = _read(path)
            key = f"{info.get('pid')}:{info.get('since')}"
            row = held.setdefault(key, {**info, "slots": []})
            row["slots"].append(path.name)
    queue = [t | {"ticket": n} for n, t in _live_tickets(tickets)]
    now = time.time()
    for row in [*held.values(), *queue]:
        row["age"] = round(now - row.get("since", row.get("queued", now)))
    cap = capacity()
    return {
        "cores": cores(),
        "capacity": cap,
        "used": sum(len(r["slots"]) for r in held.values()),
        "mem_available_gib": round(_mem_gib("MemAvailable"), 1),
        "holders": sorted(held.values(), key=lambda r: r.get("since", 0)),
        "queue": queue,
        "load1": os.getloadavg()[0],
    }


def _arg(argv: list[str], flag: str, default: str) -> str:
    return argv[argv.index(flag) + 1] if flag in argv else default


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        print(json.dumps(status(), indent=1))
        return 0
    if cmd == "capacity":
        print(capacity())
        return 0
    if cmd == "free":
        print(free_count())
        return 0
    if cmd == "run" and "--" in argv:
        split = argv.index("--")
        opts, child = argv[1:split], argv[split + 1 :]
        weight = int(_arg(opts, "--weight", "1"))
        with hold(_arg(opts, "--class", "batch"), weight, _arg(opts, "--label", child[0])) as grant:
            env = dict(os.environ, ABU_SLOT_HELD="1", SLOT_JOBS=str(grant.weight if grant else weight))
            return subprocess.call(child, env=env)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
