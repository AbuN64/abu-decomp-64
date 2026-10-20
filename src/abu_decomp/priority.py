"""CPU and IO priority by kind of work: the gates everything waits on outrank the trials lanes run in bulk.

A landing or a full proof build is the one job every lane's next tree waits on, so it runs raised; score, try and
the other tool calls lanes repeat by the hundred run niced. The class comes from the command line alone, so a job
cannot start at the wrong priority because its launcher wrapped it in `nice`: a proof lowers its nice value as far as
the host's RLIMIT_NICE allows, and a trial only ever raises its own."""

from __future__ import annotations

import ctypes
import os
import resource
import sys
from collections.abc import Callable, Sequence

PROOF_NICE = -5  # landing, full proof builds, setup of a tree: what the other jobs queue behind
TRIAL_NICE = 5  # score, try, recipe, behave: many small jobs a lane repeats
PROOF_COMMANDS = frozenset({"land", "ship", "make"})
PROOF_VERBS = frozenset({("unbake", "setup"), ("unbake", "land")})
TRIAL_COMMANDS = frozenset({"unbake", "recipe", "taste", "behave", "probe"})
_IOPRIO_SET = {"x86_64": 251, "aarch64": 30}
_IOPRIO_WHO_PROCESS, _IOPRIO_CLASS_BE = 1, 2


def kind(args: Sequence[str]) -> str | None:
    """The kind ("proof", "trial" or None: left as started) of a `decomp [--profile P] COMMAND ...` line."""
    rest = list(args)
    if rest[:1] == ["--profile"]:
        rest = rest[2:]
    elif rest[:1] and rest[0].startswith("--profile="):
        rest = rest[1:]
    if not rest:
        return None
    command, rest = rest[0], rest[1:]
    if command in PROOF_COMMANDS:
        return "proof"
    if command not in TRIAL_COMMANDS:
        return None
    rest = rest[1:] if rest[:1] == ["--"] else rest
    while rest and rest[0].startswith("--"):  # the tool's own and decomp's tree flags come before the verb
        rest = rest[1:] if "=" in rest[0] else rest[2:]
    return "proof" if rest and (command, rest[0]) in PROOF_VERBS else "trial"


def _lowest_nice() -> int:
    """The lowest nice value this process may set: RLIMIT_NICE's soft limit is 20 - that value (0 means none)."""
    soft, _ = resource.getrlimit(resource.RLIMIT_NICE)
    return 20 if soft == resource.RLIM_INFINITY else 20 - int(soft)


def _ioprio_be(level: int) -> None:
    number = _IOPRIO_SET.get(os.uname().machine)
    if number is None:
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.syscall(number, _IOPRIO_WHO_PROCESS, 0, (_IOPRIO_CLASS_BE << 13) | level) != 0:
        raise OSError(ctypes.get_errno(), "ioprio_set")


def apply(
    args: Sequence[str],
    *,
    getnice: Callable[[], int] = lambda: os.getpriority(os.PRIO_PROCESS, 0),
    setnice: Callable[[int], None] = lambda value: os.setpriority(os.PRIO_PROCESS, 0, value),
    lowest: Callable[[], int] = _lowest_nice,
    ioprio: Callable[[int], None] = _ioprio_be,
    warn: Callable[[str], None] = lambda text: sys.stderr.write(text + "\n"),
) -> int | None:
    """Set this process's priority for its command line before anything starts; children inherit it. Returns the
    nice value set, None when the command keeps the one it started with. Never fails the command."""
    found = kind(args)
    if found is None:
        return None
    now = getnice()
    if found == "trial":
        if now >= TRIAL_NICE:
            return None
        target = TRIAL_NICE
    else:
        target = max(PROOF_NICE, lowest())
        if target >= now:
            if now > 0:  # started niced and the host allows no lower value: say so, the job still runs
                warn(f"decomp: proof job left at nice {now}: RLIMIT_NICE allows no lower value (wanted {PROOF_NICE})")
            target = now
    try:
        if target != now:
            setnice(target)
        if found == "proof":
            ioprio(0)
    except OSError as exc:
        warn(f"decomp: priority not set ({exc}); running at nice {now}")
        return None
    return target if target != now else None
