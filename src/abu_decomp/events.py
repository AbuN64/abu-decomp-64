"""Event boundary. Cake Taste owns the one event writer; decomp imports it and never writes the format itself.

decomp produces the lifecycle kinds; Cake Taste instruments produce PROBE and HANDOFF.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from types import ModuleType
from typing import Any

from abu_decomp import DecompError
from abu_decomp.config import Profile

LIFECYCLE_KINDS = ("RUN-FAIL", "LANE", "LAND", "MATCH", "DECISION")
# Schema 1 names decomp retires on top of the writer's own map (owner decision 10-18): readers show them as the
# replacement, writers refuse them.
RETIRED_KINDS = {"RUNG": "DECISION"}


def writer() -> ModuleType:
    """The installed Cake Taste writer, required at event schema 2."""
    try:
        from cake_taste.logs import events
    except ImportError as exc:
        raise DecompError("events.writer", "Cake Taste is not installed; run decomp dependencies") from exc
    if getattr(events, "SCHEMA", 1) != 2 or not set(LIFECYCLE_KINDS) <= set(getattr(events, "KINDS", ())):
        raise DecompError("events.writer", "the installed Cake Taste writes event schema 1; pin a schema 2 commit")
    return events


def events_path(profile: Profile) -> Path:
    """The feed named by the profile's `events` section: {"path": ...}."""
    table = profile.section("events")
    if set(table) != {"path"}:
        raise DecompError("config.schema", f"{profile.source_file}:events: expected exactly the key path")
    return profile.path("events.path", table["path"])


class _WriterProfile:
    """Presents the decomp events section in the shape the Cake Taste writer reads its feed path from."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def section(self, name: str) -> dict[str, str]:
        return {"events_path": str(self._path)}

    def resolve_path(self, value: str) -> Path:
        return Path(value)


def canonical_kind(value: dict[str, Any]) -> str | None:
    """The schema 2 kind of any readable line: the writer's map plus the retired names above."""
    kind = writer().canonical_kind(value)
    if kind in RETIRED_KINDS and value.get("schema") == 1:
        return RETIRED_KINDS[kind]
    return kind


def append(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Append one event through the Cake Taste writer. `record` holds actor, status, message and the optional parts."""
    module = writer()
    kind = record.get("kind")
    if kind in RETIRED_KINDS:
        raise DecompError("events.kind", f"{kind} is retired; write {RETIRED_KINDS[kind]}")
    try:
        return module.append_event(
            _WriterProfile(path),
            record["actor"],
            record["status"],
            record["message"],
            record.get("evidence"),
            kind=kind,
            payload=record.get("payload"),
            subject=record.get("subject"),
            lane=record.get("lane"),
        )
    except ValueError as exc:
        raise DecompError("events.invalid", str(exc)) from exc


def tail(path: Path, count: int, kinds: set[str] | None = None) -> list[dict[str, Any]]:
    """The last `count` events (after the kind filter), each with its `canonical_kind`; unreadable lines are skipped."""
    module = writer()
    unknown = (kinds or set()) - set(module.KINDS)
    if unknown:
        raise DecompError("events.kind", f"unknown event kind {', '.join(sorted(unknown))}")
    rows: deque[dict[str, Any]] = deque(maxlen=max(count, 0))
    with path.open("rb") as stream:
        for raw in stream:
            try:
                value = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(value, dict):
                continue
            kind = canonical_kind(value)
            if kinds and kind not in kinds:
                continue
            rows.append({**value, "canonical_kind": kind})
    return list(rows)


def validate(path: Path) -> dict[str, Any]:
    """Validate a feed with the writer's own validator. The path is only read."""
    with path.open("rb") as stream:
        return writer().validate_lines(stream)
