"""Children that die with their owner: a killed lane, timeout or xargs must not leave gdb or game trees behind.

Every long-lived spawn (play, oracle, behave, statediff, the gdb bridge, gdb and the game under it) takes `owned()`:
its own session, so the owner can kill the whole group, and PR_SET_PDEATHSIG, so the kernel kills the child at the
instant its parent dies, even by SIGKILL when no handler runs. The signal follows the parent *thread*; spawn from the
thread that waits for the child (every caller does)."""

from __future__ import annotations

import ctypes
import os
import signal
from typing import Any

PR_SET_PDEATHSIG = 1
_libc = ctypes.CDLL(None, use_errno=True)


def die_with_parent(parent: int | None = None) -> None:
    """Arm PDEATHSIG for this process; exit at once if the parent is already gone (the arm-after-death race)."""
    if _libc.prctl(PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
    if parent is not None and os.getppid() != parent:
        os._exit(1)


def owned() -> dict[str, Any]:
    """Popen/create_subprocess_exec keywords: own process group plus death with the parent."""
    owner = os.getpid()
    return {"start_new_session": True, "preexec_fn": lambda: die_with_parent(owner)}


def wrap(argv: list[str]) -> list[str]:
    """argv run under util-linux setpriv so a grandchild gdb launches (PDEATHSIG survives exec) dies with gdb."""
    return ["setpriv", "--pdeathsig", "kill", *argv]
