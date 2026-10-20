"""What runs from which venv: the stable python path, the unit registry and the deploy-time restarts.

`decomp tools deploy` swaps a live venv symlink (`venv-decomp`) to a fresh versioned venv, but its console scripts
carry the versioned python in their shebang, so a service started through `venv-decomp/bin/decomp` reports a
versioned `sys.executable`. `stable_python` maps that back to the live symlink, so every command the stream spawns
resolves the symlink again on its next start. Each unit the stream starts records `{pid, live, venv}` under
`<run_dir>/units/`; a unit whose recorded venv differs from where its live symlink points now runs old code and
`restart_stale` restarts it (one at a time, waiting for it to come back), never while a configured hold lock is held.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

VERSIONED = re.compile(r"^(?P<live>.+)\.[0-9a-f]{12}(-\d+)?$")
UP = "stream-up"
UP_FILES = ("stream/up.py", "stream/supervise.py", "stream/control.py", "stream/services.py", "stream/tiles.py")


def live_of(venv: Path) -> Path | None:
    """The live symlink a versioned venv path (`NAME.<sha12>[-N]`) is deployed behind, when it points here now."""
    match = VERSIONED.match(str(venv))
    if match is None:
        return None
    live = Path(match["live"])
    return live if live.is_symlink() and os.path.realpath(live) == os.path.realpath(venv) else None


def stable_python(executable: str | None = None) -> str:
    """`sys.executable`, spelled through the live venv symlink when it runs from that symlink's current target."""
    python = Path(executable or sys.executable)
    live = live_of(python.parent.parent)
    return str(live / "bin" / python.name) if live else str(python)


def live_venv(python: str) -> Path:
    return Path(python).parent.parent


def venv_now(python: str) -> str:
    """Where the venv of this python path points right now (resolved venv directory)."""
    return os.path.realpath(live_venv(python))


def units_dir(run_dir: Path) -> Path:
    return run_dir / "units"


def record(run_dir: Path, name: str, pid: int, *, kind: str, python: str | None = None, venv: str | None = None) -> None:
    """Write the unit's record atomically. `python` is the stable path its next start goes through."""
    python = python or stable_python()
    path = units_dir(run_dir) / (name + ".json")
    old = read(run_dir, name)
    row = {
        "name": name,
        "kind": kind,
        "pid": pid,
        "live": str(live_venv(python)),
        "venv": venv or venv_now(python),
        "starts": (old or {}).get("starts", 0) + 1,
        "started": time.time(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f".{path.name}.{os.getpid()}")
    scratch.write_text(json.dumps(row))
    os.replace(scratch, path)


def read(run_dir: Path, name: str) -> dict[str, Any] | None:
    try:
        return json.loads((units_dir(run_dir) / (name + ".json")).read_text())
    except (OSError, ValueError):
        return None


def rows(run_dir: Path) -> list[dict[str, Any]]:
    found = (read(run_dir, p.stem) for p in sorted(units_dir(run_dir).glob("*.json")))
    return [row for row in found if row]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stale(run_dir: Path, live: Path) -> list[dict[str, Any]]:
    """Recorded units behind `live` that are still running from another venv than `live` points to now."""
    target = os.path.realpath(live)
    return [r for r in rows(run_dir) if r["live"] == str(live) and alive(r["pid"]) and r["venv"] != target]


def up_needs_restart(run_dir: Path, live: Path) -> str | None:
    """Why `stream up` itself must restart, or None. It is old code when its record is missing (started before
    the registry) or when a file it runs differs between its venv and the live one."""
    row = read(run_dir, UP)
    if row is None or not alive(row["pid"]):
        return None if row is not None else "no record: started before the unit registry"
    if row["venv"] == os.path.realpath(live):
        return None
    old = Path(row["venv"])
    for relative in UP_FILES:
        pair = [sorted((root / "lib").glob(f"python*/site-packages/abu_decomp/{relative}")) for root in (old, live)]
        if not pair[0] or not pair[1] or pair[0][0].read_bytes() != pair[1][0].read_bytes():
            return f"{relative} differs between {old.name} and {Path(os.path.realpath(live)).name}"
    return None


def held(paths: list[str]) -> list[str]:
    """The lock files among `paths` that another process holds now (non-blocking probe, released at once)."""
    busy = []
    for value in paths:
        try:
            handle = os.open(value, os.O_RDWR)
        except OSError:
            continue
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
        except BlockingIOError:
            busy.append(value)
        finally:
            os.close(handle)
    return busy


def signal_unit(run_dir: Path, up: dict[str, Any], name: str) -> dict[str, Any]:
    """Restart one unit: a role's runner gets SIGUSR1; a service (restarted by `stream up`) gets SIGTERM."""
    from . import control

    if name in up.get("roles", {}):
        return control.restart(run_dir, name)
    if name in up.get("services", {}):
        row = read(run_dir, name)
        if row is None or not alive(row["pid"]):
            raise ValueError(f"service {name} is not running (is `decomp stream up` running?)")
        os.kill(row["pid"], signal.SIGTERM)
        return {"service": name, "action": "restart", "pid": row["pid"]}
    raise ValueError("unknown stream role or service: " + name)


def wait_back(run_dir: Path, name: str, before: int, live: Path, timeout: float, settle: float, clock=time.monotonic,
              sleep=time.sleep) -> bool:
    """Wait until `name` has a newer start than `before`, runs from the venv `live` points to, and is still alive."""
    deadline = clock() + timeout
    target = os.path.realpath(live)
    while clock() < deadline:
        row = read(run_dir, name)
        if row and row["starts"] > before and row["venv"] == target and alive(row["pid"]):
            sleep(settle)
            row = read(run_dir, name)
            if row and alive(row["pid"]) and row["venv"] == target:
                return True
        sleep(1)
    return False


def restart_stale(
    run_dir: Path,
    up: dict[str, Any],
    live: Path,
    *,
    timeout: float = 120,
    settle: float = 5,
    signal_one: Callable[..., dict[str, Any]] = signal_unit,
    restart_stream: Callable[[], None] | None = None,
    wait: Callable[..., bool] = wait_back,
    held_locks: Callable[[list[str]], list[str]] = held,
    clock=time.monotonic,
    sleep=time.sleep,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Restart every unit behind `live` that runs another venv's code. (rows done, blocking findings)

    Waits (up to `timeout`) while a hold lock is held (`restart.hold_locks`: ship, promotion, release build), then acts
    one unit at a time and waits for each to come back on the new venv. `stream up` itself restarts (through its
    systemd unit, `restart_stream`) only when its own code changed; that brings every unit back, so it goes first.
    Player roles are skipped: only the owner restarts the viewer's instance."""
    config = up.get("restart", {})
    done: list[dict[str, Any]] = []

    def blocked() -> list[dict[str, Any]]:
        deadline = clock() + timeout
        while busy := held_locks(list(config.get("hold_locks", []))):
            if clock() > deadline:
                return [{"key": "deploy.restart_busy", "blocking": True, "message": f"restart waited {timeout:g}s for {busy}"}]
            sleep(2)
        return []

    if (why := up_needs_restart(run_dir, live)) and restart_stream is not None:
        if findings := blocked():
            return done, findings
        names = [r["name"] for r in rows(run_dir) if r["name"] != UP and alive(r["pid"])]
        before = {n: (read(run_dir, n) or {}).get("starts", 0) for n in [UP, *names]}
        restart_stream()
        done.append({"unit": UP, "action": "restart stream unit", "why": why})
        bad = [n for n in [UP, *names] if not wait(run_dir, n, before[n], live, timeout, settle)]
        if bad:
            return done, [{"key": "deploy.restart_failed", "blocking": True, "message": f"not back on {live}: {bad}"}]
    for row in sorted(stale(run_dir, live), key=lambda r: r["name"]):
        name = row["name"]
        if name == UP:
            continue  # its own code is unchanged (see up_needs_restart): the supervisor keeps running
        if up.get("roles", {}).get(name, {}).get("player"):
            done.append({"unit": name, "action": "skipped", "why": "the viewer's playable role"})
            continue
        if findings := blocked():
            return done, findings
        before = row["starts"]
        signal_one(run_dir, up, name)
        ok = wait(run_dir, name, before, live, timeout, settle)
        done.append({"unit": name, "action": "restart", "ok": ok})
        if not ok:
            return done, [{"key": "deploy.restart_failed", "blocking": True, "message": f"{name} not back on {live}"}]
    return done, []


def systemd_restart(unit: str, run: Callable[..., Any] = subprocess.run) -> None:
    run(["systemctl", "--user", "restart", unit], check=True, capture_output=True, text=True)
