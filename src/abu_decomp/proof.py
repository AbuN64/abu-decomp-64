"""`decomp make`: a proof build of a tree, through the shared build pool.

A lane that needs the project's own make (a proof build of a patch, one target of the Makefile) runs it here instead of
raw: the build runs in the project tree under the unbake slot pool (`unbake-run`, the pinned
unbake's own wrapper), capped by the seat's `land.jobs` admit command, and its output streams to stderr.
The result carries the exit code and wall time; project and host come the same way as for `decomp unbake`."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from unbake import treelock

from abu_decomp import DecompError, land, slots, tools
from abu_decomp.config import Profile

TAIL_LINES = 12


def job_count(profile: Profile, project: Path) -> str | None:
    """The `-j` value the seat's admit command gives right now; None when the seat names none (make's default)."""
    jobs = land.settings(profile)["jobs"]
    if not jobs:
        return None
    code, out, err = land.run_process(jobs, project, dict(os.environ), 60)
    if code != 0 or not out.split():
        raise DecompError("proof.admit", f"{jobs[0]} gave no job count (exit {code}): {err.strip()[:200]}")
    return out.split()[0]


def command(profile: Profile, project: Path, given: Sequence[str]) -> list[str]:
    """`unbake-run [--work N] -- make -j{j} ARGS` through the one builder `land.build_command`; N is the seat's admit
    answer (a cap), asked only when the caller gave no `-j`/`--jobs`; a given N is the cap instead."""
    own, _ = land.explicit_jobs(given)
    return land.build_command(["make", *given], None if own else job_count(profile, project))


def run(
    profile: Profile, args: Sequence[str], *, project: Path | None = None, host: Path | None = None
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    chosen = tools.settings(profile, project, host)
    argv = command(profile, chosen.project, list(args))
    started, tail = time.monotonic(), deque(maxlen=TAIL_LINES)
    print(f"decomp: make in {chosen.project}", file=sys.stderr)
    try:
        # make rewrites build/ and the extraction a ship's check reads: it holds the tree lock as the ship does
        # a stamp beyond the free slots or the measured memory queues in the host pool, before it takes the tree lock
        with (
            slots.hold("batch", slots.stamp_weight(), "decomp make", mem_gib=slots.STAMP_GIB or None) as grant,
            treelock.hold(chosen.project, exclusive=True, who="make"),
        ):
            if grant and grant.waited > 1:
                print(f"decomp: queued {grant.waited:.0f}s in the host pool for a stamp slot", file=sys.stderr)
            environment = dict(os.environ, UNBAKE_HOST=str(chosen.host))  # after the hold: it marks its children
            with subprocess.Popen(
                argv,
                cwd=chosen.project,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            ) as child:
                assert child.stdout is not None
                for line in child.stdout:
                    sys.stderr.write(line)
                    tail.append(line.rstrip())
                code = child.wait()
    except OSError as exc:
        raise DecompError("proof.process", f"cannot execute {argv[0]}: {exc}") from exc
    wall = round(time.monotonic() - started, 1)
    result = {"exit": code, "wall_seconds": wall, "command": argv[1:], "project": str(chosen.project)}
    if code == 0:
        return result, 0, []
    return (
        result,
        1,
        [{"key": "proof.failed", "blocking": True, "message": f"make exited {code}: {tail[-1] if tail else ''}"}],
    )
