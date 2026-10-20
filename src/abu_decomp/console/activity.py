"""Activity pane: one chronological feed of what the matching work is doing, from the operation events.

    "panes": {"activity": {
        "limit": 200,                          newest rows kept (the tile tails the bottom)
        "fail_seconds": 1800,                  RUN-FAIL events and refused landings this recent are rows; a later
                                               successful landing also retires an earlier refused one
        "progress": {"path": ".../progress.json", "version": "us-rev1", "label": "RW"}   footer figures
    }}

Rows come from `payload.op` events (abu_decomp.narrate), one per operation in the kit format
`function [size] op  value`: a score attempt (`88.1% match`, `CRACKED!`), a submission, a behaviour check (`same as
original 9/9`: the candidate and the original ran on the same captured inputs and agreed), a Recipe step, a landing
with Recipe's proposal, plus Cake Taste probes. Tool failures read `tool verb  failed: reason-key`; identical rows fold
into the newest with a count (kit.fold). A row's lane is a tag at its end, not a pane. Expanding a row shows the
operation's values and the function's earlier trials. The footer counts the functions landed today
(`Fix N ... stand-ins` commit messages) and shows the progress.json function and code figures.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import kit
from .dashboard_model import Row, event_epoch
from .feed import canonical_kind
from .human import block, payload_of, percent_matched, sentence

LANDED = re.compile(r"\bFix (\d+) \S+ stand-ins?\b")
OPS = ("attempt", "submit", "behave")
ITEM_VERBS = ("try", "work", "skip")  # recipe verbs that name one function
REASON = re.compile(r"(?:^|\s)([a-z][\w-]*(?:\.[\w-]+)+):")  # a finding key: score.inactive, land.request
BEHAVE = "runs this C and the original on the same captured inputs and compares return values and memory writes"


def function_of(event: dict[str, Any]) -> str | None:
    payload = payload_of(event)
    if payload.get("op") in OPS:
        return str(payload.get("function"))
    if payload.get("op") == "recipe" and payload.get("verb") in ITEM_VERBS and payload.get("item"):
        return str(payload["item"])
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


def reason_of(message: str) -> str:
    """The plain reason of a tool failure: its finding key (`score.inactive`), else the text after `failed`."""
    found = REASON.findall(message)
    if found:
        return found[-1]
    tail = message.partition("failed")[2].strip(" ,:")
    return tail[:60]


def checks_text(payload: dict[str, Any]) -> str:
    checks, differences = payload.get("checks"), payload.get("differences") or 0
    return f" {checks - differences}/{checks}" if isinstance(checks, int) and checks else ""


def verdict_cells(event: dict[str, Any]) -> tuple[str, str, str]:
    """(op, value, state) of a behaviour check or a Cake Taste probe: a plain badge, never `logic same`."""
    payload = payload_of(event)
    op = "behave" if payload.get("op") == "behave" else "probe"
    verdict = payload.get("verdict") or ("same" if not payload.get("differences") else "differs")
    if event.get("status") == "failed" and op == "probe":
        verdict = "error"
    counts = checks_text(payload)
    if verdict == "same":
        return op, f"same as original{counts}", "passed"
    if verdict == "differs":
        return op, f"differs from original{counts}", "stop"
    return op, "check could not run", "stop"


def function_row(event: dict[str, Any], function: str, history: list[dict[str, Any]], sizes: dict[str, int]):
    """`name [size] op  value` for one function operation, and its expanded detail."""
    payload = payload_of(event)
    op = payload.get("op")
    name = function  # the full symbol: func_8027CCA0_eu and func_8027CCA0_de are different functions
    earlier = [history_line(e) for e in history if e is not event and payload_of(e).get("op") == op][-6:]
    trail = ("trail", " | ".join(earlier))
    if op == "attempt":
        found = percent_matched(payload)
        exact = bool(payload.get("exact"))
        value = "CRACKED!" if exact else f"{found}% match" if found else "refused"
        state = "cracked" if exact else "stop" if not found else "event"
        details = "\n".join(part for part in (block(event), kit.block(trail)) if part)
        return cells(name, sizes.get(function), "score", value), details, state
    if op == "submit":
        value = sentence(event).partition(": ")[2]
        state = "stop" if payload.get("refused") else "passed" if payload.get("exact") else "event"
        return cells(name, sizes.get(function), "submit", value), block(event), state
    if op == "recipe":
        verb = str(payload.get("verb"))
        ok = bool(payload.get("ok"))
        details = kit.block(("next", payload.get("next")), ("result", "ok" if ok else "refused"))
        return (
            cells(name, sizes.get(function), f"recipe {verb}", "ok" if ok else "refused"),
            details,
            ("event" if ok else "stop"),
        )
    verb, value, state = verdict_cells(event)
    details = kit.block(("check", f"{verb}: {BEHAVE}" if verb == "behave" else "Cake Taste probe"), trail)
    return cells(name, sizes.get(function), verb, value), "\n".join(p for p in (details, block(event)) if p), state


def cells(name: str, size_bytes: int | None, op: str, value: str) -> str:
    return kit.cells(name, kit.size(size_bytes), op, value)


def tool_row(event: dict[str, Any]) -> tuple[str, str, str]:
    """A tool call that did not touch one function: `tool verb  value` (a recipe step, a failure, a landing)."""
    payload, message = payload_of(event), str(event.get("message", ""))
    op = payload.get("op")
    if op == "landed":
        return sentence(event), block(event), "passed" if event.get("status") == "passed" else "stop"
    if op == "recipe":
        verb = " ".join(str(payload[k]) for k in ("verb", "sub") if payload.get(k))
        facts = [f"{payload[k]} {k}" for k in ("accepted", "refused", "moves", "files") if k in payload]
        ok = bool(payload.get("ok"))
        value = ", ".join(facts) if ok and facts else "ok" if ok else "refused"
        state = "passed" if ok and event.get("status") == "passed" else "event" if ok else "stop"
        return kit.cells("recipe", verb, value), kit.block(("next", payload.get("next")), ("message", message)), state
    if op == "build":
        tail = payload.get("tail") or []
        value = f"failed exit {payload.get('exit')}" + (f": {tail[-2] if len(tail) > 1 else tail[-1]}" if tail else "")
        details = kit.block(("args", " ".join(map(str, payload.get("args") or []))), ("output", "\n".join(tail)))
        return kit.cells("build", value[:90]), details, "stop"
    if payload.get("verb"):
        value = "failed: " + (reason_of(message) or "no reason given")
        details = kit.block(
            ("message", message), ("seconds", payload.get("seconds")), ("findings", payload.get("findings"))
        )
        return kit.cells(f"{op} {payload['verb']}", value), details, "stop"
    actor = str(event.get("actor", ""))
    return kit.cells(actor, message), kit.block(("message", message), ("status", event.get("status"))), "stop"


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

    def row(
        self,
        event: dict[str, Any],
        history: dict[str, list[dict[str, Any]]],
        now: float,
        sizes: dict[str, int] | None = None,
    ) -> Row | None:
        payload, kind = payload_of(event), canonical_kind(event)
        if event.get("status") == "started":
            return None
        function = function_of(event)
        when, key = event_epoch(event), f"act-{event.get('time')}-{event.get('actor')}-{event.get('message')}"
        lane = str(payload.get("lane") or "")
        lane = "" if lane == "unknown" else lane
        if function:
            title, details, state = function_row(event, function, history.get(function, []), sizes or {})
            return Row(key, title, details, state, when=when, tag=lane)
        if payload.get("op") == "landed":
            title, details, state = tool_row(event)
            applied = ", ".join(n for n, row in (payload.get("lanes") or {}).items() if row.get("status") == "applied")
            if state == "stop" and when and (now - when > self.fail_seconds or when < self.last_landed):
                return None  # an old refusal, or one a later landing already answered, is not news
            return Row(key, title, details, state, when=when, tag=applied)
        if payload.get("op") == "recipe" and kind in ("MATCH", "LAND", "RUN-FAIL"):
            title, details, state = tool_row(event)
            return Row(key, title, details, state, when=when, tag=lane)
        if kind == "RUN-FAIL" and when and now - when <= self.fail_seconds:
            title, details, state = tool_row(event)
            return Row(key, title, details, state, when=when, tag=lane or str(event.get("actor", "")))
        return None

    def rows(self, sources: Any, events: list[dict[str, Any]]) -> list[Row]:
        del events
        now = time.time()
        held = self.events(sources)
        history: dict[str, list[dict[str, Any]]] = {}
        sizes: dict[str, int] = {}
        for event in held:
            if function := function_of(event):
                history.setdefault(function, []).append(event)
                if payload_of(event).get("size_bytes"):
                    sizes[function] = int(payload_of(event)["size_bytes"])
        self.last_landed = max(
            (
                event_epoch(e) or 0.0
                for e in held
                if payload_of(e).get("op") == "landed" and e.get("status") == "passed"
            ),
            default=0.0,
        )
        rows = [row for event in held if (row := self.row(event, history, now, sizes))]
        rows = kit.fold(rows)[-self.limit :]
        return rows or [
            Row("act-none", "no matching activity yet", "attempts appear as lanes score functions", "planned")
        ]

    def facts(self, sources: Any, rows: list[Row]) -> list[str]:
        cracked = sum(1 for row in rows if row.state == "cracked")
        failing = sum(row.count for row in rows if row.state == "stop")
        return [f"{len(rows)} rows", f"{cracked} cracked" if cracked else "", f"{failing} failures" if failing else ""]

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
