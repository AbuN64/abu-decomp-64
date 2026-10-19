"""Declarative per-tile console sources: one `console` object on a tile in the seat `tiles` table.

    "tiles": {"roles": {"oracle-console": {"title": "oracle console", "console": {
        "title": "ORACLE / MISSION",
        "kinds": ["RUN-FAIL", "PROBE", "MATCH", "HANDOFF", "LAND"],
        "host_log": "/path/oracle.log",
        "sources": {"run_status": "/path/status.json"},
        "panes": {"progress": {...}, "landings": {"limit": 12}, "lanes": {...}}}}}}

`sources` are the keys dashboard_sources.Sources reads (build_profile, supervisor_status, run_status, events,
boot_plan, ...); `panes` select the lifecycle panes. Relative paths are relative to the profile file. The feed is the
seat `events` path unless the tile names its own `feed`; `sources.events` adds further feeds.
Nothing is generated: the tile process reads the profile directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from abu_decomp import DecompError

from .feed import events_path, parse_kinds

PATH_KEYS = ("build_profile", "supervisor_status", "release_current", "run_status", "host_log", "boot_plan")
CONSOLE_KEYS = frozenset({"title", "feed", "kinds", "actors", "host_log", "sources", "panes"})
SECTION_KEYS = frozenset({"max_lines", "git_refs"})


def _path(profile: Any, value: str) -> str:
    return value if Path(value).is_absolute() else str(profile.resolve_path(value))


def tile_console(profile: Any, tile: str) -> dict[str, Any]:
    roles = profile.section("tiles")["roles"]
    if tile not in roles:
        raise DecompError("console.tile", f"unknown tile {tile}; the tiles table has {', '.join(sorted(roles))}")
    spec = roles[tile]
    if "console" not in spec:
        raise DecompError("console.tile", f"tile {tile} has no console object")
    unknown = spec["console"].keys() - CONSOLE_KEYS
    if unknown:
        raise DecompError("config.schema", f"tiles.roles.{tile}.console: unknown keys: {', '.join(sorted(unknown))}")
    return spec


def tile_sources(profile: Any, tile: str) -> dict[str, Any]:
    """The arguments of run_console for a tile, from the profile alone."""
    spec = tile_console(profile, tile)
    console = spec["console"]
    sources = dict(console.get("sources", {}))
    for key in PATH_KEYS:
        if key in sources:
            sources[key] = _path(profile, sources[key])
    if "events" in sources:
        sources["events"] = [_path(profile, value) for value in sources["events"]]
    if "panes" in console:
        sources["panes"] = console["panes"]
    try:
        section = profile.section("console")
    except DecompError:
        section = {}
    unknown = section.keys() - SECTION_KEYS
    if unknown:
        raise DecompError("config.schema", f"console: unknown keys: {', '.join(sorted(unknown))}")
    kinds = console.get("kinds")
    host_log = console.get("host_log")
    return {
        "path": Path(_path(profile, console["feed"])) if "feed" in console else events_path(profile),
        "title": console.get("title", str(spec.get("title", tile)).upper()),
        "max_lines": int(section.get("max_lines", 4000)),
        "kinds": parse_kinds(",".join(kinds)) if kinds is not None else None,
        "sources": sources,
        "host_log": Path(_path(profile, host_log)) if host_log else None,
        "actors": tuple(console.get("actors", ())),
    }


def run_tile(profile: Any, tile: str) -> dict[str, Any]:
    from .app import run_console

    arguments = tile_sources(profile, tile)
    run_console(**arguments)
    return {"closed": True, "tile": tile, "path": str(arguments["path"])}


def run_follower(profile: Any, action: str) -> dict[str, Any]:
    """`refs` appends LAND events for new commits; `status-line` prints the newest event for the bar."""
    path = events_path(profile)
    if action == "status-line":
        from .statusline import follow

        follow(path)
    else:
        from .gitrefs import RefSource

        section = profile.section("console")
        if "git_refs" not in section:
            raise DecompError("config.schema", "console.git_refs: required by `decomp console refs`")
        RefSource(profile, section["git_refs"]).serve()
    return {"closed": True, "path": str(path)}
