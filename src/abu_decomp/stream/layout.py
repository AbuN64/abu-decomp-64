"""Headless compositor config and a fixed 2x2 tiling tree built from profile tiles."""

from __future__ import annotations

import re
import shlex
from typing import Any


class LayoutError(ValueError):
    pass


def validate(up: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return (column_one, column_two) role ids: tiles are [[TL, TR], [BL, BR]]."""
    rows = up["tiles"]
    if len(rows) != 2 or any(len(row) != 2 for row in rows):
        raise LayoutError("tiles must be two rows of two role ids")
    flat = [rows[0][0], rows[1][0], rows[0][1], rows[1][1]]
    if len(set(flat)) != 4:
        raise LayoutError("four tile roles must be distinct")
    missing = [role for role in flat if role not in up["roles"]]
    if missing:
        raise LayoutError("tiles name undefined roles: " + ", ".join(missing))
    return [rows[0][0], rows[1][0]], [rows[0][1], rows[1][1]]


def app_id(role: str, prefix: str = "tile-") -> str:
    return prefix + role


def exact(value: str) -> str:
    """Quoted, anchored regular-expression criterion matching exactly this app_id."""
    return '"^' + re.escape(value) + '$"'


def sway_config(up: dict[str, Any], bar_command: list[str] | None = None) -> str:
    """Sway config: one fixed-size virtual output, no decoration, overlay rules for role windows.

    Criteria are regular expressions, so each is anchored: a role named like its tile's app_id must not match it.
    An overlay is a window a role opens itself (a game); it floats exactly over its role's tile, so the tile's
    terminal shows through (black, for a placeholder role) whenever the window is absent.
    A bar, when the profile declares one, runs bar_command as its status line.
    """
    out = up["output"]
    width, height = int(out["width"]), int(out["height"])
    lines = [
        "default_border none",
        "default_floating_border none",
        "titlebar_border_thickness 0",
        "focus_follows_mouse no",
        f"output {out['name']} mode {width}x{height} position 0 0 bg #000000 solid_color",
    ]
    for _role, overlay, x, y, _w, _h in overlays(up):
        lines.append(
            f"for_window [app_id={exact(overlay)}] floating enable, border none, move absolute position {x} {y}"
        )
    bar = up.get("bar")
    if bar and bar_command:
        lines += [
            "bar {",
            f"    position {bar.get('position', 'bottom')}",
            f"    height {int(bar.get('height', 24))}",
            f"    status_command {shlex.join(bar_command)}",
            "    font monospace 11",
            "    colors {",
            "        background #11111b",
            "        statusline #cdd6f4",
            "    }",
            "}",
        ]
    return "\n".join(lines) + "\n"


def overlays(
    up: dict[str, Any], area: tuple[int, int, int, int] | None = None
) -> list[tuple[str, str, int, int, int, int]]:
    """(role, app_id, x, y, width, height) of every role window that must sit exactly over its tile.

    area is the workspace rectangle (x, y, w, h); without it the whole output is used.
    """
    ox, oy, aw, ah = area or (0, 0, int(up["output"]["width"]), int(up["output"]["height"]))
    tile_w, tile_h = aw // 2, ah // 2
    found = []
    for col, ids in enumerate(validate(up)):
        for row, role in enumerate(ids):
            overlay = up["roles"][role].get("overlay_app_id")
            if overlay:
                found.append((role, overlay, ox + col * tile_w, oy + row * tile_h, tile_w, tile_h))
    return found


def _workspace_area(tree: dict[str, Any]) -> tuple[int, int, int, int] | None:
    def walk(node: dict[str, Any]) -> dict[str, Any] | None:
        if node.get("type") == "workspace" and not str(node.get("name", "")).startswith("__") and node["rect"]["width"]:
            return node
        for child in node.get("nodes", []):
            found = walk(child)
            if found:
                return found
        return None

    node = walk(tree)
    if not node:
        return None
    rect = node["rect"]
    return rect["x"], rect["y"], rect["width"], rect["height"]


def overlay_fixes(tree: dict[str, Any], up: dict[str, Any]) -> list[str]:
    """Commands that move a role window onto its tile; a client may resize itself after the map-time rule."""
    wanted = {app: (x, y, w, h) for _role, app, x, y, w, h in overlays(up, _workspace_area(tree))}
    commands: list[str] = []

    def walk(node: dict[str, Any]) -> None:
        app = node.get("app_id")
        if app in wanted:
            x, y, w, h = wanted[app]
            rect = node["rect"]
            if (rect["x"], rect["y"], rect["width"], rect["height"]) != (x, y, w, h):
                commands.append(f"[con_id={node['id']}] resize set {w} {h}, move absolute position {x} {y}")
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            walk(child)

    walk(tree)
    return commands


def focus_command(change: dict[str, Any], up: dict[str, Any]) -> str | None:
    """Focus the configured default role's window the moment it appears."""
    role = up.get("focus_role")
    container = change.get("container") or {}
    if change.get("change") != "new" or not role:
        return None
    if container.get("app_id") == up["roles"][role].get("overlay_app_id"):
        return f"[con_id={container['id']}] focus"
    return None


def build_tree(ipc: Any, up: dict[str, Any], spawn: Any, wait_window: Any) -> None:
    """Create the 2x2 tree deterministically: TL, TR beside it, BL under TL, BR under TR.

    spawn(role) starts the role's terminal; wait_window(app_id) blocks until its window exists.
    """
    left, right = validate(up)
    top_left, bottom_left = left
    top_right, bottom_right = right
    prefix = up.get("app_id_prefix", "tile-")
    for role in (top_left, top_right):
        spawn(role)
        wait_window(app_id(role, prefix))
    ipc.command(f"[app_id={exact(app_id(top_left, prefix))}] focus; splitv")
    spawn(bottom_left)
    wait_window(app_id(bottom_left, prefix))
    ipc.command(f"[app_id={exact(app_id(top_right, prefix))}] focus; splitv")
    spawn(bottom_right)
    wait_window(app_id(bottom_right, prefix))
    focus = up.get("focus_role")
    if focus:
        ipc.command(f"[app_id={exact(app_id(focus, prefix))}] focus")
