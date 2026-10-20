"""The CPU every process under this one used, descendants that were never waited for included.

`getrusage(RUSAGE_CHILDREN)` counts a child only when its parent waits for it. A tool that is killed (a cap, a timeout)
leaves its workers and their compilers behind: they reparent to init, finish unseen and their CPU is in no meter, so a
killed `recipe work` read as a few seconds while its workers had used a hundred. This process becomes the subreaper of
its tree (`prctl(PR_SET_CHILD_SUBREAPER)`): an orphan reparents here, and `cpu()` waits for the ones that already ended
before it reads the meter. `kill_descendants()` ends whatever still runs under this process after its tool was
stopped, so the CPU stops growing unseen and is then counted.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import resource
import signal
import time

PR_SET_CHILD_SUBREAPER = 36
_adopted = False


def adopt() -> bool:
    """Make this process the parent of every orphan below it; False where the kernel cannot (the meter then counts what
    it always did). Idempotent."""
    global _adopted
    if not _adopted:
        try:
            _adopted = ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
        except (OSError, AttributeError):
            _adopted = False
    return _adopted


def reap() -> int:
    """Wait for every child that already ended (the orphans adopted above among them) so its CPU is in the meter. Call
    it only where no subprocess of this process is running: it would take the exit status of one the caller waits
    for."""
    count = 0
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return count
        if not pid:
            return count
        count += 1


def cpu() -> float:
    """CPU seconds (user+system) of every finished descendant, orphans included."""
    reap()
    used = resource.getrusage(resource.RUSAGE_CHILDREN)
    return used.ru_utime + used.ru_stime


def descendants() -> list[int]:
    """Pids of every process below this one, read from /proc (parents before their children)."""
    parents: dict[int, list[int]] = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as handle:
                text = handle.read().decode(errors="replace")
        except OSError:
            continue
        fields = text.rpartition(")")[2].split()  # the name may hold spaces: the fields after its `)` are fixed
        if len(fields) > 1:
            parents.setdefault(int(fields[1]), []).append(int(name))
    found: list[int] = []
    queue = [os.getpid()]
    while queue:
        for child in parents.get(queue.pop(0), ()):
            found.append(child)
            queue.append(child)
    return found


def kill_descendants(wait: float = 5.0) -> int:
    """SIGKILL everything still running below this process and wait for it, so its CPU is counted; how many it killed.
    For a tool this process stopped itself: a worker left running would burn CPU nobody reads."""
    victims = descendants()
    killed = len(victims)
    for pid in victims:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
    end = time.monotonic() + wait
    while victims and time.monotonic() < end:
        reap()
        victims = [pid for pid in victims if os.path.exists(f"/proc/{pid}")]
        if victims:
            time.sleep(0.02)
    reap()
    return killed
