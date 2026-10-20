"""Pure console rows derived from build receipts, gate output and mission evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from . import human

_ADDRESS = re.compile(r"(?<![0-9a-fA-F])(?:0x)?(80[0-9a-fA-F]{6})(?![0-9a-fA-F])")


@dataclass(frozen=True)
class Row:
    key: str
    title: str
    details: str
    state: str = "event"
    open: bool = False
    when: float | None = None  # epoch seconds of the entry; the console shows its age
    tag: str = ""  # short dim tag at the end of the line (a lane)
    count: int = 1  # identical results folded into this row (kit.fold)


@dataclass
class Dashboard:
    heading: str
    summary: str
    rows: list[Row] = field(default_factory=list)
    focus_key: str | None = None
    footer: str = ""
    pinned: list[str] = field(default_factory=list)  # short status values (the video hud prints them)
    facts: list[str] = field(default_factory=list)  # the title line's values after the title (kit.heading)


def ago(seconds: float) -> str:
    """The compact age of an entry: 12s, 4m, 2h, 3d."""
    seconds = max(0, int(seconds))
    for limit, unit, size in ((60, "s", 1), (3600, "m", 60), (86400, "h", 3600)):
        if seconds < limit:
            return f"{seconds // size}{unit}"
    return f"{seconds // 86400}d"


def event_epoch(event: dict) -> float | None:
    try:
        return datetime.fromisoformat(str(event.get("time"))).timestamp()
    except ValueError:
        return None


def addresses(text: str) -> list[str]:
    return ["0x" + match.upper() for match in _ADDRESS.findall(text)]


def event_addresses(event: dict) -> frozenset[str]:
    """Addresses an event mentions; computed once per event and remembered on it when the event allows."""
    found = getattr(event, "addresses", None)
    if found is None:
        found = frozenset(addresses(str(event.get("message", "")) + " " + str(event.get("payload", {}))))
    return found


class Event(dict):
    """An event record that parses its addresses once, when it is read."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.addresses = event_addresses(dict(self))


class EventLog(list):
    """Ordered events with an owner address -> events map, built once per log instead of scanned per lookup."""

    @property
    def by_owner(self) -> dict[str, list[dict]]:
        if "_by_owner" not in self.__dict__:
            index: dict[str, list[dict]] = {}
            for event in self:
                for owner in event_addresses(event):
                    index.setdefault(owner, []).append(event)
            self._by_owner = index
        return self._by_owner


def mission_model(
    events: list[dict],
    *,
    title: str,
    frames: str,
    graphics: str,
    run_id: str,
    raw_log: str,
) -> Dashboard:
    probes = [event for event in events if event.get("kind") == "PROBE"]
    question = (
        ((probes[-1].get("payload") or {}).get("question") or probes[-1].get("message")) if probes else "none yet"
    )
    rows = [Row("raw-log", "Raw host log", raw_log or "No host output recorded.", "raw")]
    for event in human.results_only(events):
        label = human.sentence(event)
        key = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
        rows.append(
            Row(
                key,
                label,
                human.block(event),
                "stop" if event.get("status") in {"failed", "blocked"} else "event",
                when=event_epoch(event),
            )
        )
    return Dashboard(
        f"{title} · run {run_id} · frames {frames} · graphics {graphics}",
        "Last probe: " + str(question),
        rows,
        rows[-1].key,
    )
