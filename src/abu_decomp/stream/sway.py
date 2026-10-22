"""Event-driven Sway IPC layout for four configured, resizable roles."""

from __future__ import annotations

import json
import socket
import struct
from pathlib import Path
from typing import Any

from . import tiles

MAGIC = b"i3-ipc"
COMMAND = 0
SUBSCRIBE = 2
GET_TREE = 4
WINDOW_EVENT = 0x80000003


def _receive(sock: socket.socket) -> tuple[int, Any]:
    header = bytearray()
    while len(header) < 14:
        chunk = sock.recv(14 - len(header))
        if not chunk:
            raise ConnectionError("Sway IPC connection closed")
        header.extend(chunk)
    if header[:6] != MAGIC:
        raise ValueError("invalid Sway IPC header")
    length, kind = struct.unpack("<II", header[6:14])
    body = bytearray()
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            raise ConnectionError("Sway IPC payload incomplete")
        body.extend(chunk)
    return kind, json.loads(body) if body else None


def _send(sock: socket.socket, kind: int, value: Any) -> Any:
    payload = json.dumps(value).encode() if not isinstance(value, str) else value.encode()
    sock.sendall(MAGIC + struct.pack("<II", len(payload), kind) + payload)
    return _receive(sock)[1]


class SwayIPC:
    def __init__(self, path: Path):
        self.path = path

    def request(self, kind: int, value: Any = "") -> Any:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(str(self.path))
            return _send(connection, kind, value)

    def command(self, value: str) -> None:
        reply = self.request(COMMAND, value)
        if not isinstance(reply, list) or not all(item.get("success") for item in reply):
            raise RuntimeError("Sway command failed: " + value + " " + str(reply))

    def tree(self) -> dict[str, Any]:
        return self.request(GET_TREE)

    def subscribe(self) -> socket.socket:
        connection = socket.socket(socket.AF_UNIX)
        connection.connect(str(self.path))
        reply = _send(connection, SUBSCRIBE, ["window"])
        if not reply.get("success"):
            connection.close()
            raise RuntimeError("Sway window subscription failed")
        return connection


def _walk(node: dict[str, Any]):
    yield node
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        yield from _walk(child)


def _workspace(tree: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((node for node in _walk(tree) if node.get("type") == "workspace" and node.get("name") == name), None)


def _roles(tree: dict[str, Any], app_ids: dict[str, str]) -> dict[str, dict[str, Any]]:
    result = {}
    for node in _walk(tree):
        for role, app_id in app_ids.items():
            if node.get("app_id") == app_id and (role not in result or node["id"] > result[role]["id"]):
                result[role] = node
    return result


def _common_parent(node: dict[str, Any], ids: set[int]) -> dict[str, Any] | None:
    children = node.get("nodes", [])
    if ids.issubset({child.get("id") for child in children}):
        return node
    for child in children:
        found = _common_parent(child, ids)
        if found:
            return found
    return None


class Layout:
    def __init__(self, profile: Any, client: SwayIPC | None = None):
        config = tiles.table(profile)
        self.config = config["desktop"]
        self.roles = config["roles"]
        self.workspace = str(self.config["workspace"])
        self.scratch = str(self.config["scratch_workspace"])
        rows = config["layout"]
        if len(rows) != 2 or any(len(row) != 2 for row in rows):
            raise ValueError("layout.tiles must be two rows of two role IDs")
        self.top_left, self.top_right = rows[0]
        self.bottom_left, self.bottom_right = rows[1]
        self.order = [self.top_left, self.bottom_left, self.top_right, self.bottom_right]
        if len(set(self.order)) != 4:
            raise ValueError("four tile roles must be distinct")
        self.overlay = list(self.config.get("graphic_overlays", []))
        app_ids = {role: self.roles[role].get("app_id", role) for role in self.order + self.overlay}
        self.app_ids = app_ids
        self.client = client or SwayIPC(profile.resolve_path(self.config["ipc_socket"]))

    def _nodes(self) -> dict[str, dict[str, Any]]:
        return _roles(self.client.tree(), self.app_ids)

    def _flat_parent(self, ids: list[int]) -> dict[str, Any]:
        tree = self.client.tree()
        workspace = _workspace(tree, self.workspace)
        if not workspace:
            raise RuntimeError("workspace absent after moving roles")
        parent = _common_parent(workspace, set(ids))
        if not parent:
            raise RuntimeError("managed roles did not share a tiling parent")
        return parent

    def _reorder(self, ids: list[int]) -> None:
        for index, wanted in enumerate(ids):
            for _ in range(len(ids) * 2):
                parent = self._flat_parent(ids)
                present = [child["id"] for child in parent["nodes"]]
                actual = present.index(wanted)
                if actual == index:
                    break
                direction = "left" if actual > index else "right"
                self.client.command(f"[con_id={wanted}] move {direction}")
            else:
                raise RuntimeError("could not order Sway tiles")

    def _group_vertical(self, top: int, bottom: int) -> None:
        self.client.command(f"[con_id={top}] focus")
        self.client.command("splitv")
        self.client.command(f"[con_id={bottom}] move left")
        tree = self.client.tree()
        parent = next(
            (
                node
                for node in _walk(tree)
                if {top, bottom}.issubset({child.get("id") for child in node.get("nodes", [])})
            ),
            None,
        )
        if not parent or parent.get("layout") != "splitv":
            raise RuntimeError("Sway did not create vertical tile pair")
        if [child["id"] for child in parent["nodes"]].index(top) > 0:
            self.client.command(f"[con_id={top}] move up")

    def reconcile(self) -> dict[str, Any]:
        nodes = self._nodes()
        missing = [role for role in self.order if role not in nodes]
        if missing:
            return {"ready": False, "missing": missing}
        selected = next((node["id"] for node in nodes.values() if node.get("focused")), None)
        graphic_roles = [self.top_right, *self.overlay]
        selected_graphic = next(
            (nodes[role]["id"] for role in graphic_roles if role in nodes and nodes[role].get("visible")), None
        )
        for role in self.order + [role for role in self.overlay if role in nodes]:
            self.client.command(f"[con_id={nodes[role]['id']}] move container to workspace number {self.scratch}")
        self.client.command(f"workspace number {self.workspace}")
        for role in self.order:
            self.client.command(f"[con_id={nodes[role]['id']}] move container to workspace number {self.workspace}")
        first = nodes[self.top_left]["id"]
        self.client.command(f"[con_id={first}] focus")
        self.client.command("layout splith")
        ids = [nodes[role]["id"] for role in self.order]
        self._reorder(ids)
        self._group_vertical(ids[0], ids[1])
        self._group_vertical(ids[2], ids[3])
        present_overlays = [role for role in self.overlay if role in nodes]
        if present_overlays:
            self.client.command(f"[con_id={ids[2]}] focus")
            self.client.command("splitv")
            self.client.command(f"[con_id={ids[2]}] focus")
            self.client.command("layout tabbed")
            for role in present_overlays:
                self.client.command(f"[con_id={nodes[role]['id']}] move container to workspace number {self.workspace}")
        if selected_graphic:
            self.client.command(f"[con_id={selected_graphic}] focus")
        if selected:
            self.client.command(f"[con_id={selected}] focus")
        return {"ready": True, "roles": {role: nodes[role]["id"] for role in self.order}, "workspace": self.workspace}

    def watch(self) -> dict[str, Any]:
        subscriber = self.client.subscribe()
        try:
            self.reconcile()
            while True:
                kind, event = _receive(subscriber)
                if kind == WINDOW_EVENT and event.get("change") in {"new", "close"}:
                    self.reconcile()
        finally:
            subscriber.close()
