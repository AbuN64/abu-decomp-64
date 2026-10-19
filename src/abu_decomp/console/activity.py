"""Activity pane: one chronological feed of what the matching work is doing, from the operation events.

    "panes": {"activity": {
        "limit": 200,                          newest rows kept (the tile tails the bottom)
        "fail_seconds": 1800,                  RUN-FAIL events and refused landings this recent are rows; a later
                                               successful landing also retires an earlier refused one
        "progress": {"path": ".../progress.json", "version": "us-rev1", "label": "RW"}   footer figures
    }}

Rows come from `payload.op` events (abu_decomp.narrate): a score attempt, a submission, a behaviour check, a landing
with Recipe's proposal, plus Cake Taste probes of a function. A row's lane is a tag at its end, not a pane. Expanding a
row shows the whole trial history of its function: every attempt (words matched, first differing instruction), every
probe or behaviour verdict with its inputs and outputs, and the refusals in plain words. A landing row says what
Recipe Maker suggests (module name and file) and shows the proposal. The footer counts the functions landed today
(`Fix N ... stand-ins` commit messages) and shows the progress.json function and code figures.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from abu_decomp.narrate import display_name

from .dashboard_model import Row, event_epoch
from .feed import canonical_kind
from .human import block, payload_of, sentence

LANDED = re.compile(r"\bFix (\d+) \S+ stand-ins?\b")
OPS = ("attempt", "submit", "behave")


def function_of(event: dict[str, Any]) -> str | None:
    payload = payload_of(event)
    if payload.get("op") in OPS:
        return str(payload.get("function"))
    if canonical_kind(event) == "PROBE" and payload.get("function") and payload.get("checks") is not None:
        return str(payload["function"])
    return None


def stamp(event: dict[str, Any]) -> str:
    when = event_epoch(event)
    return time.strftime("%H:%M:%S", time.localtime(when)) if when else "--:--:--"


def history_line(event: dict[str, Any]) -> str:
    """One compact line of an earlier trial of the same function."""
    payload = payload_of(event)
    op = payload.get("op")
    if op == "attempt":
        return f"attempt {payload.get('attempt')}: {sentence(event).partition(': ')[2]}"
    if op == "submit":
        return f"submit: {sentence(event).partition(': ')[2]}"
    return sentence(event).partition(": ")[2] or sentence(event)


class ActivityPane:
    name = "activity"
    uses_events = True

    def __init__(self, config: dict[str, Any]):
        self.limit = int(config.get("limit", 200))
        self.fail_seconds = float(config.get("fail_seconds", 1800))
        self.progress = config.get("progress")
        self.last_landed = 0.0

    def events(self, sources: Any) -> list[dict[str, Any]]:
        held = [event for records in sources.records.values() for event in records]
        return sorted(held, key=lambda event: str(event.get("time", "")))

    def row(self, event: dict[str, Any], history: dict[str, list[dict[str, Any]]], now: float) -> Row | None:
        payload, kind = payload_of(event), canonical_kind(event)
        if event.get("status") == "started":
            return None
        function = function_of(event)
        when, key = event_epoch(event), f"act-{event.get('time')}-{event.get('actor')}-{event.get('message')}"
        lane = str(payload.get("lane") or "")
        lane = "" if lane == "unknown" else lane
        if function:
            op = payload.get("op")
            version = next(
                (payload_of(e).get("version") for e in history[function] if payload_of(e).get("version")), None
            )
            title = sentence(event).replace(function, display_name(function, version), 1)
            earlier = [history_line(e) for e in history[function] if e is not event and payload_of(e).get("op") == op][
                -6:
            ]
            trail = ("\ntrail      " + " | ".join(earlier)) if earlier else ""
            details = block(event) + trail
            state = (
                "cracked"
                if op == "attempt" and payload.get("exact")
                else "passed"
                if payload.get("exact") or payload.get("verdict") == "same"
                else "stop"
                if payload.get("refused") or payload.get("verdict") in ("differs", "error")
                else "event"
            )
            return Row(key, title, details, state, when=when, tag=lane)
        if payload.get("op") == "landed":
            title = sentence(event)
            details = block(event)
            applied = ", ".join(n for n, row in (payload.get("lanes") or {}).items() if row.get("status") == "applied")
            state = "passed" if event.get("status") == "passed" else "stop"
            if state == "stop" and when and (now - when > self.fail_seconds or when < self.last_landed):
                return None  # an old refusal, or one a later landing already answered, is not news
            return Row(key, title, details, state, when=when, tag=applied)
        if kind == "RUN-FAIL" and when and now - when <= self.fail_seconds:
            return Row(key, sentence(event), block(event), "stop", when=when)
        return None

    def rows(self, sources: Any, events: list[dict[str, Any]]) -> list[Row]:
        del events
        now = time.time()
        held = self.events(sources)
        history: dict[str, list[dict[str, Any]]] = {}
        for event in held:
            if function := function_of(event):
                history.setdefault(function, []).append(event)
        self.last_landed = max(
            (
                event_epoch(e) or 0.0
                for e in held
                if payload_of(e).get("op") == "landed" and e.get("status") == "passed"
            ),
            default=0.0,
        )
        rows = [row for event in held if (row := self.row(event, history, now))]
        rows = rows[-self.limit :]
        return rows or [
            Row("act-none", "no matching activity yet", "attempts appear as lanes score functions", "planned")
        ]

    def landed_today(self, sources: Any) -> int:
        today, total = time.strftime("%Y-%m-%d"), 0
        for records in sources.records.values():
            for event in records:
                if canonical_kind(event) != "LAND":
                    continue
                try:
                    when = datetime.fromisoformat(str(event.get("time"))).astimezone()
                except ValueError:
                    continue
                found = LANDED.search(str(event.get("message", "")).partition(" ")[2].partition(" ")[2])
                if found and when.strftime("%Y-%m-%d") == today:
                    total += int(found[1])
        return total

    def footer(self, sources: Any) -> str:
        parts = [f"landed today {self.landed_today(sources)}"]
        if self.progress:
            from .panes import progress_versions

            path = Path(self.progress["path"])
            sources.watched.add(path)
            label = self.progress.get("label", "main")
            try:
                cart = progress_versions(sources.json(path))[self.progress["version"]]
                parts.append(
                    f"{label} {self.progress['version']} functions "
                    f"{cart['functions_matched']}/{cart['functions_total']}"
                    f" code {100 * cart['code_matched'] / cart['code_total']:.2f}%"
                    f" ({cart['code_matched']:,} of {cart['code_total']:,} bytes)"
                )
            except (KeyError, ValueError, TypeError):
                parts.append(f"{label} progress unavailable")
        return " · ".join(parts)
