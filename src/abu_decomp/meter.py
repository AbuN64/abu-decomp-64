"""The CPU every process under this one used, workers that outlived the tool that started them included.

`getrusage(RUSAGE_CHILDREN)` counts a child only when its parent waited for it. A tool that is stopped (a cap, a
timeout, ^C) leaves its workers and their compilers behind: they reparent to init, finish unseen and their CPU is in no
meter, so a stopped `recipe work` read as a few seconds while its workers had used a hundred. Two readings close the
gap, and neither waits for a child (a wait here could take the exit status of a child another thread watches): `cpu()`
adds the CPU of the processes still running below this one to the reaped total, and `stop_marked()` finds the workers a
stopped tool left behind by the mark its environment carried, reads their CPU and ends them.
"""

from __future__ import annotations

import contextlib
import os
import resource
import signal
import time

_TICKS = os.sysconf("SC_CLK_TCK")


def _table() -> dict[int, tuple[int, int, float]]:
    """pid -> (parent, process group, CPU seconds the process and the children it waited for used), from /proc."""
    rows: dict[int, tuple[int, int, float]] = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as handle:
                text = handle.read().decode(errors="replace")
        except OSError:
            continue
        fields = text.rpartition(")")[2].split()  # the name may hold spaces: the fields after its `)` are fixed
        if len(fields) > 15:
            rows[int(name)] = (int(fields[1]), int(fields[2]), sum(int(f) for f in fields[11:15]) / _TICKS)
    return rows


def below(root: int, rows: dict[int, tuple[int, int, float]]) -> list[int]:
    """Pids of every process below `root`, parents before their children."""
    children: dict[int, list[int]] = {}
    for pid, (parent, _, _) in rows.items():
        children.setdefault(parent, []).append(pid)
    found: list[int] = []
    queue = [root]
    while queue:
        for child in children.get(queue.pop(0), ()):
            found.append(child)
            queue.append(child)
    return found


def cpu() -> float:
    """CPU seconds (user+system) of every finished child and of everything still running below this process."""
    used = resource.getrusage(resource.RUSAGE_CHILDREN)
    rows = _table()
    return used.ru_utime + used.ru_stime + sum(rows[pid][2] for pid in below(os.getpid(), rows))


def stop_marked(marker: bytes, wait: float = 5.0) -> float:
    """SIGKILL every process whose environment holds `marker` (the call this process started marks the environment it
    gives the tool, and every descendant inherits it, the workers an exited tool left behind among them) and return
    the CPU they had used. A tool that was stopped leaves them running, burning CPU no meter reads."""
    rows = _table()
    victims = []
    for pid in rows:
        if pid == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/environ", "rb") as handle:
                if marker in handle.read().split(b"\0"):
                    victims.append(pid)
        except OSError:
            continue
    spent = sum(rows[pid][2] for pid in victims)
    for pid in victims:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
    end = time.monotonic() + wait
    while victims and time.monotonic() < end:
        victims = [pid for pid in victims if os.path.exists(f"/proc/{pid}") and not _zombie(pid)]
        if victims:
            time.sleep(0.02)
    return spent


def _zombie(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            return handle.read().decode(errors="replace").rpartition(")")[2].split()[0] == "Z"
    except (OSError, IndexError):
        return True
