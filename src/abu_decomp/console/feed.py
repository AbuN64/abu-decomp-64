"""The console's one door to event feeds: the Cake Taste writer's reader and formatter, never a second copy.

`abu_decomp.events.writer()` imports the installed writer and requires schema 2. The seat profile's `events`
section names the feed as {"path": ...}; the writer reads its feed path through a profile object, so
`_WriterProfile` presents that one path in the shape it expects.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from abu_decomp.events import writer


def events_path(profile: Any) -> Path:
    return profile.resolve_path(profile.section("events")["path"])


class _WriterProfile:
    def __init__(self, path: Path) -> None:
        self._path = path

    def section(self, name: str) -> dict[str, str]:
        return {"events_path": str(self._path)}

    def resolve_path(self, value: str) -> Path:
        return Path(value)


def canonical_kind(value: dict[str, Any]) -> str | None:
    return writer().canonical_kind(value)


def format_event(value: dict[str, Any]) -> list[str]:
    return writer().format_event(value)


def parse_kinds(text: str) -> set[str]:
    return writer().parse_kinds(text)


def EventReader(path: Path) -> Any:
    return writer().EventReader(path)


def append_event(profile: Any, actor: str, status: str, message: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Append through the one writer to the profile's feed."""
    return writer().append_event(_WriterProfile(events_path(profile)), actor, status, message, *args, **kwargs)
