"""Start decomp's own work inside the seat's run slice, so a host that splits cores between users of the machine
splits them by slice weight and not by how many ssh sessions happen to be busy.

The seat profile names the slice (`tools.run_slice`, a systemd user slice such as `decomp.slice`). When the process is
not already in it, it re-execs itself under `systemd-run --user --scope`; make, gcc and the pools it starts inherit
the slice. A host without systemd-run, or one that refuses, runs the command in place with one warning line."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

ENTERED = "ABU_RUN_SLICE_ENTERED"  # the slice a parent already tried, so a refusal can never loop
PINNED = "ABU_VERSION_PINNED"  # set by the exec into the resolved venv, so the pin can never loop


def in_slice(slice_name: str, cgroup: str) -> bool:
    """Whether a /proc/<pid>/cgroup text puts the process under a cgroup directory named `slice_name`."""
    return any(slice_name in line.partition(":")[2].partition(":")[2].split("/") for line in cgroup.splitlines())


def in_service(cgroup_text: str) -> bool:
    """Whether the unified (`0::`) cgroup path of a /proc/<pid>/cgroup text ends in a `.service` unit."""
    for line in cgroup_text.splitlines():
        if line.startswith("0::"):
            return line[3:].rstrip("/").rpartition("/")[2].endswith(".service")
    return False


def _read_cgroup() -> str:
    try:
        return Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return ""


def _systemd_run(slice_name: str, command: Sequence[str]) -> list[str]:
    return ["systemd-run", "--user", "--scope", "--quiet", f"--slice={slice_name}", "--", *command]


def _probe(slice_name: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _systemd_run(slice_name, ["true"]), capture_output=True, text=True, timeout=15, check=False, env=_clean(None)
    )


def _clean(slice_name: str | None, pinned: bool = False) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != ENTERED}
    if slice_name:
        env[ENTERED] = slice_name
    if pinned:
        env[PINNED] = "1"
    return env


def pinned_python(prefix: str | None = None) -> str | None:
    """The resolved venv's python when this process runs through a symlinked venv path (a deploy swaps that link, so
    a later import would load the next version's modules); None when the prefix is a real directory or the pin was
    already done."""
    if os.environ.get(PINNED):
        return None
    path = Path(sys.prefix if prefix is None else prefix)
    resolved = path.resolve()
    return str(resolved / "bin" / "python") if resolved != path else None


def enter(
    slice_name: str | None,
    argv: Sequence[str],
    *,
    cgroup: Callable[[], str] = _read_cgroup,
    which: Callable[[str], str | None] = shutil.which,
    probe: Callable[[str], subprocess.CompletedProcess[str]] = _probe,
    execvpe: Callable[..., None] = os.execvpe,
    warn: Callable[[str], None] = lambda text: sys.stderr.write(text + "\n"),
    pinned: bool = False,
) -> bool:
    """Re-execs `argv` inside `slice_name` and does not return when it does. Returns False when the command should
    run in place: no slice named, already in it, already tried by a parent, running as a service, or the host cannot
    do it (one warning). A service is placed by its unit's `Slice=`; only session processes are moved."""
    if not slice_name or os.environ.get(ENTERED) == slice_name:
        return False
    text = cgroup()
    if in_slice(slice_name, text) or in_service(text):
        return False
    if which("systemd-run") is None:
        warn(f"decomp: run slice {slice_name} not entered: systemd-run is missing; running in place")
        return False
    try:
        tried = probe(slice_name)
        reason = (
            "" if tried.returncode == 0 else (tried.stderr.strip().splitlines() or [f"exit {tried.returncode}"])[-1]
        )
    except (OSError, subprocess.SubprocessError) as exc:
        reason = str(exc)
    if reason:
        warn(f"decomp: run slice {slice_name} not entered: systemd-run refused ({reason}); running in place")
        return False
    command = _systemd_run(slice_name, argv)
    try:
        execvpe(command[0], command, _clean(slice_name, pinned))
    except OSError as exc:
        warn(f"decomp: run slice {slice_name} not entered: {exc}; running in place")
    return False


def slice_named(args: Sequence[str]) -> str | None:
    """`tools.run_slice` of the profile a command line names (`--profile P` first); None when there is none, the
    profile does not load or the key is absent. The command itself reports a bad profile; this never fails."""
    rest = list(args)
    if rest[:1] == ["--profile"] and len(rest) > 1:
        path = rest[1]
    elif rest[:1] and rest[0].startswith("--profile="):
        path = rest[0].partition("=")[2]
    else:
        return None
    try:
        document = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        value = document["tools"]["run_slice"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return value if isinstance(value, str) and value.endswith(".slice") else None


def start(
    args: Sequence[str],
    *,
    prefix: str | None = None,
    orig_argv: Sequence[str] | None = None,
    execve: Callable[..., None] = os.execve,
    **hooks: Any,
) -> None:
    """The one call at the top of `decomp`'s main. It builds the final command line first (the resolved venv's python,
    wrapped in `systemd-run` when the seat's slice applies) and execs it once, so one command never execs twice."""
    python = pinned_python(prefix)
    rest = list(sys.orig_argv[1:] if orig_argv is None else orig_argv[1:])
    argv = [python or sys.executable, *rest]
    name = slice_named(args)
    if name:
        enter(name, argv, pinned=python is not None, **hooks)  # does not return when it execs
    if python:
        try:
            execve(python, argv, _clean(None, True))
        except OSError as exc:
            sys.stderr.write(f"decomp: venv pin not applied: {exc}; running in place\n")
