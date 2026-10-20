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
    unknown = tiles.keys() - {"layout", "focus", "roles", "desktop", "groups", "column_weights", "row_weights"}
    if unknown:
        raise DecompError("config.schema", "tiles: unknown keys: " + ", ".join(sorted(unknown)))
    return tiles


def disabled_roles(tiles: dict[str, Any]) -> set[str]:
    """Groups own role IDs, including headless apps; disabling one removes all its consumers."""
    disabled: set[str] = set()
    assigned: set[str] = set()
    for name, group in tiles.get("groups", {}).items():
        if set(group) != {"enabled", "roles"} or type(group["enabled"]) is not bool:
            raise DecompError("config.schema", f"tiles.groups.{name}: requires enabled boolean and roles list")
        members = group["roles"]
        if not isinstance(members, list) or not members or any(not isinstance(role, str) for role in members):
            raise DecompError("config.schema", f"tiles.groups.{name}.roles: requires nonempty role IDs")
        if len(set(members)) != len(members) or assigned.intersection(members) or set(members) - tiles["roles"].keys():
            raise DecompError("config.schema", f"tiles.groups.{name}.roles: undefined or multiply grouped role")
        assigned.update(members)
        if not group["enabled"]:
            disabled.update(members)
    return disabled


def weights(tiles: dict[str, Any], key: str, count: int) -> list[int]:
    values = tiles.get(key, [1] * count)
    if not isinstance(values, list) or len(values) != count or any(type(v) is not int or v <= 0 for v in values):
        raise DecompError("config.schema", f"tiles.{key}: requires {count} positive integer weights")
    return values


def console_command(profile: Any, role: str, action: str = "tile") -> list[str]:
    return [stable_python(), "-m", "abu_decomp", "--profile", str(profile.source_file), "console", action, role]


def up_config(profile: Any) -> dict[str, Any]:
    """stream.up with the tiles table merged in; a console tile without a command runs `decomp console`."""
    up = profile.section("stream")["up"]
    for key in DUPLICATED:
        if key in up:
            raise DecompError("config.schema", f"stream.up.{key}: move it to the tiles table")
    tiles = table(profile)
    from .layout import LayoutError, validate

    validate({"tiles": tiles["layout"], "roles": tiles["roles"]})
    disabled = disabled_roles(tiles)
    rows = tiles["layout"]
    columns = [i for i in range(len(rows[0])) if any(row[i] not in disabled for row in rows)]
    indices = [i for i, row in enumerate(rows) if any(role not in disabled for role in row)]
    grid = [[rows[i][j] for j in columns] for i in indices]
    if not grid or not columns or any(role in disabled for row in grid for role in row):
        raise LayoutError("disabled groups must leave a nonempty rectangular grid without holes")
    column_weights = weights(tiles, "column_weights", len(rows[0]))
    row_weights = weights(tiles, "row_weights", len(rows))
    roles = {}
    for role, spec in tiles["roles"].items():
        if role in disabled:
            continue
        spec = dict(spec)
        if "command" not in spec and "console" in spec:
            spec["command"] = console_command(profile, role)
        elif "command" not in spec and "hud" in spec:
            spec["command"] = console_command(profile, role, "hud")
        roles[role] = spec
    merged = {
        **up,
        "tiles": grid,
        "roles": roles,
        "column_weights": [column_weights[j] for j in columns],
        "row_weights": [row_weights[i] for i in indices],
    }
    if "focus" in tiles:
        if tiles["focus"] not in roles:
            raise DecompError("config.schema", "tiles.focus: must name an enabled role")
        merged["focus_role"] = tiles["focus"]
    return merged
