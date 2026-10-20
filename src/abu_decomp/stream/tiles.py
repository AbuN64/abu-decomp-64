"""The seat profile's one `tiles` table: the grid, the focused tile and every tile's role.

It replaces the old `panes.roles` and `stream.up.roles` pair. `stream.up` keeps compositor, output, services,
audio and bar settings; the moved stream code still reads the tile grid, the roles and the focus role from one
config object, so `up_config` merges the table into the `up` section and refuses a second copy of any of it.

    "tiles": {
      "layout": [["port-video", "oracle-video"], ["port-console", "oracle-console"]],
      "focus": "oracle-video",
      "roles": {"<id>": {"title", "command", "env", "overlay_app_id", "headless", "console"}}
    }

A role with a `console` object is a console tile: `decomp console tile ID` reads its sources (see
abu_decomp.console.tiles). `command` may then be omitted and the tile runs `decomp console` itself.
A role with a `hud` object ({"console": "<console tile id>", "height": px}) and an `overlay_app_id` prints a one-line
overlay in the bottom rows its window leaves uncovered (abu_decomp.console.hud); with no `command`, the tile runs
`decomp console hud ID` for it.
"""

from __future__ import annotations

from typing import Any

from abu_decomp import DecompError

from .services import stable_python

DUPLICATED = ("tiles", "roles", "focus_role")


def table(profile: Any) -> dict[str, Any]:
    tiles = profile.section("tiles")
    for key in ("layout", "roles"):
        if key not in tiles:
            raise DecompError("config.schema", f"tiles.{key}: required")
    unknown = tiles.keys() - {"layout", "focus", "roles", "desktop"}
    if unknown:
        raise DecompError("config.schema", "tiles: unknown keys: " + ", ".join(sorted(unknown)))
    return tiles


def console_command(profile: Any, role: str, action: str = "tile") -> list[str]:
    return [stable_python(), "-m", "abu_decomp", "--profile", str(profile.source_file), "console", action, role]


def up_config(profile: Any) -> dict[str, Any]:
    """stream.up with the tiles table merged in; a console tile without a command runs `decomp console`."""
    up = profile.section("stream")["up"]
    for key in DUPLICATED:
        if key in up:
            raise DecompError("config.schema", f"stream.up.{key}: move it to the tiles table")
    tiles = table(profile)
    roles = {}
    for role, spec in tiles["roles"].items():
        spec = dict(spec)
        if "command" not in spec and "console" in spec:
            spec["command"] = console_command(profile, role)
        elif "command" not in spec and "hud" in spec:
            spec["command"] = console_command(profile, role, "hud")
        roles[role] = spec
    merged = {**up, "tiles": tiles["layout"], "roles": roles}
    if "focus" in tiles:
        merged["focus_role"] = tiles["focus"]
    return merged
