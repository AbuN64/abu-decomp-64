"""Headless compositor config and a column tiling tree built from profile tiles (2x2, 3x2, a tall tile, ...)."""

from __future__ import annotations

import re
import shlex
from typing import Any


class LayoutError(ValueError):
    pass


HUD_HEIGHT = 50


def validate(up: dict[str, Any]) -> tuple[list[str], ...]:
    """Return the role ids of each column, top to bottom, from a nonempty rectangular grid.

    A role id repeated in consecutive rows of one column is one tile spanning those rows (a tall tile); any other
    repeat is refused, as is an undefined role or a ragged row.
    """
    rows = up["tiles"]
    if not rows or not rows[0] or any(len(row) != len(rows[0]) for row in rows):
        raise LayoutError("tiles must be a nonempty rectangular grid of role ids")
    columns: list[list[str]] = []
    for column in range(len(rows[0])):
        stack: list[str] = []
        for row in rows:
            if not stack or stack[-1] != row[column]:
                stack.append(row[column])
        columns.append(stack)
    flat = [role for stack in columns for role in stack]
    if len(set(flat)) != len(flat):
        raise LayoutError("tile roles must be distinct; only consecutive rows of one column may repeat a role")
    missing = [role for role in flat if role not in up["roles"]]
    if missing:
        raise LayoutError("tiles name undefined roles: " + ", ".join(missing))
    return tuple(columns)


def rectangles(up: dict[str, Any], area: tuple[int, int, int, int]) -> dict[str, tuple[int, int, int, int]]:
    """Partition the workspace by configured weights; repeated rows span their combined weight."""
    from .tiles import weights

    columns = validate(up)
    rows = up["tiles"]
    cw = weights(up, "column_weights", len(columns))
    rw = weights(up, "row_weights", len(rows))
    ox, oy, aw, ah = area
    xs = [ox + aw * sum(cw[:i]) // sum(cw) for i in range(len(cw) + 1)]
    ys = [oy + ah * sum(rw[:i]) // sum(rw) for i in range(len(rw) + 1)]
    found = {}
    for col, stack in enumerate(columns):
        for role in stack:
            indices = [i for i, row in enumerate(rows) if row[col] == role]
            start, end = indices[0], indices[-1] + 1
            found[role] = xs[col], ys[start], xs[col + 1] - xs[col], ys[end] - ys[start]
    return found


def app_id(role: str, prefix: str = "tile-") -> str:
    return prefix + role


def exact(value: str) -> str:
    """Quoted, anchored regular-expression criterion matching exactly this app_id."""
    return '"^' + re.escape(value) + '$"'


def sway_config(up: dict[str, Any], bar_command: list[str] | None = None, cursor_theme: str | None = None) -> str:
    """Sway config: one fixed-size virtual output, no decoration, overlay rules for role windows.

    Criteria are regular expressions, so each is anchored: a role named like its tile's app_id must not match it.
    An overlay is a window a role opens itself (a game); it floats exactly over its role's tile, so the tile's
    terminal shows through (black, for a placeholder role) whenever the window is absent.
    A bar, when the profile declares one, runs bar_command as its status line. cursor_theme names the XCursor theme the
    compositor's own cursor uses (the transparent one when the profile hides it).
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
    if cursor_theme:
        lines.append(f"seat * xcursor_theme {cursor_theme} 24")
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

    area is the workspace rectangle (x, y, w, h); without it the whole output is used. A role with a `hud` object
    leaves the bottom `hud.height` pixels (default HUD_HEIGHT, two terminal rows) of its tile uncovered: the tile's own
    terminal prints its overlay text there (see abu_decomp.console.hud).
    """
    ox, oy, aw, ah = area or (0, 0, int(up["output"]["width"]), int(up["output"]["height"]))
    found = []
    for role, (x, y, w, h) in rectangles(up, (ox, oy, aw, ah)).items():
        spec = up["roles"][role]
        overlay = spec.get("overlay_app_id")
        if overlay:
            h -= int(spec["hud"].get("height", HUD_HEIGHT)) if "hud" in spec else 0
            pixels = spec.get("pixel_size")
            if pixels is not None:
                if (
                    not isinstance(pixels, list)
                    or len(pixels) != 2
                    or any(type(v) is not int or v <= 0 for v in pixels)
                ):
                    raise LayoutError(f"role {role}: pixel_size requires two positive integers")
                scale = min(w // pixels[0], h // pixels[1])
                if scale < 1:
                    raise LayoutError(f"role {role}: tile cannot fit one integer pixel_size")
                w, h = pixels[0] * scale, pixels[1] * scale
            if h < 1:
                raise LayoutError(f"role {role}: HUD leaves no overlay space")
            found.append((role, overlay, x, y, w, h))
    return found


def size_tiles(ipc: Any, up: dict[str, Any]) -> None:
    """Set column and row sizes on the tiling containers, then place the floating pictures."""
    from .sway import _walk

    tree = ipc.tree()
    area = _workspace_area(tree)
    if area is None:
        raise LayoutError("stream workspace is absent")
    rects = rectangles(up, area)
    prefix = up.get("app_id_prefix", "tile-")
    nodes = {node.get("app_id"): node for node in _walk(tree) if node.get("app_id")}
    columns = validate(up)
    for stack in columns:
        top = nodes[app_id(stack[0], prefix)]
        target = top
        if len(stack) > 1:
            target = next(node for node in _walk(tree) if top["id"] in [c["id"] for c in node.get("nodes", [])])
        ipc.command(f"[con_id={target['id']}] resize set width {rects[stack[0]][2]} px")
        for role in stack:
            ipc.command(f"[con_id={nodes[app_id(role, prefix)]['id']}] resize set height {rects[role][3]} px")
    for command in overlay_fixes(ipc.tree(), up):
        ipc.command(command)


def focus_default(ipc: Any, up: dict[str, Any]) -> None:
    from .sway import _walk

    role = up.get("focus_role")
    if not role:
        return
    nodes = list(_walk(ipc.tree()))
    candidates = [up["roles"][role].get("overlay_app_id"), app_id(role, up.get("app_id_prefix", "tile-"))]
    for candidate in candidates:
        node = next((node for node in nodes if candidate and node.get("app_id") == candidate), None)
        if node:
            ipc.command(f"[con_id={node['id']}] focus")
            return


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
    """Create the column tree deterministically: each column's top tile side by side, then the rest of each column
    split under its top tile, left to right.

    spawn(role) starts the role's terminal; wait_window(app_id) blocks until its window exists.
    """
    columns = validate(up)
    prefix = up.get("app_id_prefix", "tile-")
    for stack in columns:
        spawn(stack[0])
        wait_window(app_id(stack[0], prefix))
    for stack in columns:
        if len(stack) > 1:
            ipc.command(f"[app_id={exact(app_id(stack[0], prefix))}] focus; splitv")
        for role in stack[1:]:
            spawn(role)
            wait_window(app_id(role, prefix))
    focus = up.get("focus_role")
    if focus:
        ipc.command(f"[app_id={exact(app_id(focus, prefix))}] focus")
