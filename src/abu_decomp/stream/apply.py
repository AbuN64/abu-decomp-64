"""Apply the seat's active tiles to an existing compositor, preserving enabled applications."""

from __future__ import annotations

import json
import os
import select
import signal
from pathlib import Path
from typing import Any

from abu_decomp.config import load_profile

from . import layout, tiles
from .control import pidfile
from .sway import SwayIPC, _receive, _walk


def stop_disabled(run_dir: Path, disabled: set[str]) -> list[str]:
    """Terminate only verified role runners; they reap their own child process groups."""
    stopped = []
    for role in sorted(disabled):
        path = pidfile(run_dir, role)
        if not path.exists():
            continue
        pid = int(path.read_text())
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            continue
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if [b"stream", b"role", b"--id", role.encode()] != argv[-5:-1]:
                raise RuntimeError(f"refusing stale runner PID for role {role}")
            signal.pidfd_send_signal(fd, signal.SIGTERM)
            if not select.select([fd], [], [], 5)[0]:
                raise TimeoutError(f"disabled role {role} did not stop")
        finally:
            os.close(fd)
        stopped.append(role)
    return stopped


def reconcile(ipc: Any, config: dict[str, Any], disabled: set[str]) -> None:
    """Rebuild columns from existing terminals; no enabled role is restarted or respawned."""
    columns = layout.validate(config)
    prefix = config.get("app_id_prefix", "tile-")
    tree = ipc.tree()
    nodes = {node.get("app_id"): node for node in _walk(tree) if node.get("app_id")}
    ordered = [role for stack in columns for role in stack]
    missing = [role for role in ordered if layout.app_id(role, prefix) not in nodes]
    if missing:
        raise layout.LayoutError("enabled tile windows are absent: " + ", ".join(missing))
    first = nodes[layout.app_id(ordered[0], prefix)]
    focus_role = config.get("focus_role", ordered[0])
    anchor = nodes.get(config["roles"][focus_role].get("overlay_app_id")) or first
    workspace = next(
        node
        for node in _walk(tree)
        if node.get("type") == "workspace" and any(child.get("id") == anchor["id"] for child in _walk(node))
    )
    if workspace["name"] == "__stream_apply":
        workspace = next(
            node
            for node in _walk(tree)
            if node.get("type") == "workspace" and not str(node.get("name", "")).startswith("__")
        )
    for role in sorted(disabled):
        node = nodes.get(layout.app_id(role, prefix))
        if node:
            ipc.command(f"[con_id={node['id']}] kill")
    # Move only managed terminals out, leaving all game windows and unrelated clients intact.
    for role in ordered:
        ipc.command(f"[con_id={nodes[layout.app_id(role, prefix)]['id']}] move container to workspace __stream_apply")
    workspace_name = json.dumps(str(workspace["name"]))
    ipc.command(f"workspace {workspace_name}")
    for stack in columns:
        top = nodes[layout.app_id(stack[0], prefix)]["id"]
        ipc.command(f"[con_id={top}] move container to workspace {workspace_name}")
        ipc.command(f"[con_id={top}] focus; layout splith")
    for stack in columns:
        if len(stack) < 2:
            continue
        top = nodes[layout.app_id(stack[0], prefix)]["id"]
        mark = f"__stream_column_{top}"
        ipc.command(f"[con_id={top}] focus; splitv; mark --add {mark}")
        for role in stack[1:]:
            ipc.command(f"[con_id={nodes[layout.app_id(role, prefix)]['id']}] move container to mark {mark}")
        ipc.command(f"unmark {mark}")
    layout.size_tiles(ipc, config)
    layout.focus_default(ipc, config)


def apply(profile: Any, *, watch: bool = False, client: Any = None) -> dict[str, Any]:
    config = tiles.up_config(profile)
    # Preflight geometry and group references before stopping any disabled application.
    layout.overlays(config)
    disabled = tiles.disabled_roles(tiles.table(profile))
    run_dir = profile.resolve_path(config["run_dir"])
    sockets = list(run_dir.glob("sway-ipc.*.sock"))
    if client is None and len(sockets) != 1:
        raise RuntimeError("expected one live stream compositor socket")
    ipc = client or SwayIPC(sockets[0])
    active = {node.get("app_id") for node in _walk(ipc.tree())}
    for stack in layout.validate(config):
        for role in stack:
            if layout.app_id(role, config.get("app_id_prefix", "tile-")) not in active:
                raise layout.LayoutError("enabled tile window is absent: " + role)
    stopped = stop_disabled(run_dir, disabled)
    reconcile(ipc, config, disabled)
    result = {"applied": True, "disabled": sorted(disabled), "stopped": stopped, "tiles": config["tiles"]}
    if not watch:
        return result
    events = ipc.subscribe()
    try:
        while True:
            change = _receive(events)[1]
            if not isinstance(change, dict) or change.get("change") == "close":
                continue
            current = tiles.up_config(load_profile(profile.source_file))
            focus = layout.focus_command(change, current)
            if focus:
                ipc.command(focus)
            for command in layout.overlay_fixes(ipc.tree(), current):
                ipc.command(command)
    finally:
        events.close()
