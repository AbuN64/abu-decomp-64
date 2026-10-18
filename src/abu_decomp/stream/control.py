"""Control surface for running roles: restart, stop and send keys, one call per role."""

from __future__ import annotations

import os
import re
import signal
import subprocess
from pathlib import Path
from typing import Any

from .layout import app_id, exact

MODIFIERS = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift", "super": "logo", "logo": "logo"}
KEY_NAMES = frozenset(
    {"Return", "Escape", "Tab", "BackSpace", "Delete", "Up", "Down", "Left", "Right", "Home", "End", "space"}
)


def pidfile(run_dir: Path, role: str) -> Path:
    return run_dir / "roles" / (role + ".pid")


def signal_role(run_dir: Path, role: str, number: int) -> int:
    """Signal the role's runner; return its pid. Raises when the role is not running."""
    path = pidfile(run_dir, role)
    if not path.is_file():
        raise ValueError(f"role {role} has no runner (is `decomp stream up` running?)")
    pid = int(path.read_text())
    try:
        os.kill(pid, number)
    except ProcessLookupError as exc:
        raise ValueError(f"role {role} runner {pid} is gone") from exc
    return pid


def restart(run_dir: Path, role: str) -> dict[str, Any]:
    return {"role": role, "action": "restart", "runner": signal_role(run_dir, role, signal.SIGUSR1)}


def stop(run_dir: Path, role: str) -> dict[str, Any]:
    return {"role": role, "action": "stop", "runner": signal_role(run_dir, role, signal.SIGUSR2)}


def wtype_args(tokens: list[str]) -> list[str]:
    """Translate key tokens to wtype arguments: `ctrl+c`, `F5`, `Return` are keys, anything else is typed text."""
    args: list[str] = []
    for token in tokens:
        parts = token.split("+") if len(token) > 1 else [token]
        mods = [MODIFIERS[p.lower()] for p in parts[:-1] if p.lower() in MODIFIERS]
        last = parts[-1]
        if len(mods) == len(parts) - 1 and len(parts) > 1:
            for mod in mods:
                args += ["-M", mod]
            args += ["-k", last]
            for mod in reversed(mods):
                args += ["-m", mod]
        elif token in KEY_NAMES or re.fullmatch(r"F([1-9]|1[0-2])", token):
            args += ["-k", token]
        else:
            args += ["--", token]
    return args


def target_app_ids(up: dict[str, Any], role: str) -> list[str]:
    """Windows that carry a role's input: its own overlay window first, then its tile terminal."""
    spec = up["roles"][role]
    found = [spec["overlay_app_id"]] if spec.get("overlay_app_id") else []
    return [*found, app_id(role, up.get("app_id_prefix", "tile-"))]


def send(run_dir: Path, up: dict[str, Any], role: str, tokens: list[str], run: Any = subprocess.run) -> dict[str, Any]:
    """Focus the role's window through sway, type the keys with wtype, then restore the default focus."""
    if role not in up["roles"]:
        raise ValueError("unknown stream role: " + role)
    sockets = sorted(run_dir.glob("sway-ipc.*"), key=lambda p: p.stat().st_mtime)
    displays = sorted(p.name for p in run_dir.glob("wayland-*") if not p.name.endswith(".lock"))
    if not sockets or not displays:
        raise ValueError("compositor is not running")
    env = {**os.environ, "XDG_RUNTIME_DIR": str(run_dir), "WAYLAND_DISPLAY": displays[0]}
    sway = ["swaymsg", "-s", str(sockets[-1])]
    focused = None
    for candidate in target_app_ids(up, role):
        reply = run([*sway, f"[app_id={exact(candidate)}] focus"], env=env, capture_output=True, text=True, check=False)
        if reply.returncode == 0 and '"success": true' in reply.stdout:
            focused = candidate
            break
    if focused is None:
        raise ValueError(f"role {role} has no window to receive keys")
    run(["wtype", *wtype_args(tokens)], env=env, check=True)
    default = up.get("focus_role")
    if default and default != role:
        for candidate in target_app_ids(up, default):
            reply = run(
                [*sway, f"[app_id={exact(candidate)}] focus"], env=env, capture_output=True, text=True, check=False
            )
            if reply.returncode == 0 and '"success": true' in reply.stdout:
                break
    return {"role": role, "action": "send", "window": focused, "tokens": tokens}
