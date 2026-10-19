"""Lane pane: one AI lane's live effort as terse rows, from the events the lane itself appends to the feed.

    "panes": {"lane": {"actor": "<the lane's actor name>", "limit": 60}}

The lane reports with the public `decomp event append --actor NAME --kind LANE --status S --message M --payload P`,
the same writer and feed every other row uses. `payload.op` says what a row is:

  step    {"op": "step"}                                     the lane's current step (newest one is pinned)
  status  {"op": "status"} or no op                           one STATUS line
  build   {"op": "build", "name": "x", "ok": true, "passed": 154, "total": 166}   build or test result
  test    {"op": "test", ...}                                 same fields as build
  stop    {"op": "stop", "name": "0x80291BE8", "reason": "..."}   the current stop (newest one is pinned)
  commit  {"op": "commit", "sha": "abc1234", "subject": "..."}

Only events whose actor is the configured one are shown. Pinned lines: step, stop, newest build and test.
"""

from __future__ import annotations

from typing import Any

from .dashboard_model import Row, event_epoch
from .human import payload_of

CLIP = 110
PIN_STEP = 48  # the pinned step stays short so a one-line hud still shows the stop after it


def clip(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= CLIP else text[: CLIP - 1] + "…"


def counts(payload: dict[str, Any]) -> str:
    return f" {payload['passed']}/{payload['total']}" if "passed" in payload and "total" in payload else ""


def result_text(op: str, payload: dict[str, Any], message: str) -> str:
    mark = "✓" if payload.get("ok") else "✗"
    name = f" {payload['name']}" if payload.get("name") else ""
    reason = (
        ""
        if payload.get("ok") or not (payload.get("reason") or message)
        else f": {clip(payload.get('reason') or message)}"
    )
    return f"{op}{name} {mark}{counts(payload)}{reason}"


def title_of(event: dict[str, Any]) -> str:
    payload, message = payload_of(event), str(event.get("message", ""))
    op = payload.get("op")
    if op == "step":
        return f"step: {clip(message)}"
    if op in ("build", "test"):
        return result_text(op, payload, message)
    if op == "stop":
        return f"stop {payload.get('name', '')}: {clip(payload.get('reason') or message)}".replace("stop : ", "stop ")
    if op == "commit":
        return f"commit {str(payload.get('sha', ''))[:7]} {clip(payload.get('subject') or message)}"
    return clip(message)


def state_of(event: dict[str, Any]) -> str:
    payload = payload_of(event)
    op = payload.get("op")
    if op in ("build", "test"):
        return "passed" if payload.get("ok") else "stop"
    if op == "stop" or event.get("status") in ("failed", "blocked"):
        return "stop"
    return "passed" if op == "commit" else "event"


class LanePane:
    name = "lane"
    uses_events = True

    def __init__(self, config: dict[str, Any]):
        self.actor = str(config["actor"])
        self.limit = int(config.get("limit", 60))

    def mine(self, sources: Any) -> list[dict[str, Any]]:
        held = [e for records in sources.records.values() for e in records if e.get("actor") == self.actor]
        return sorted((e for e in held if e.get("status") != "started"), key=lambda e: str(e.get("time", "")))

    def rows(self, sources: Any, events: list[dict[str, Any]]) -> list[Row]:
        del events
        rows = []
        for event in self.mine(sources)[-self.limit :]:
            key = f"lane-{event.get('time')}-{event.get('message')}"
            rows.append(
                Row(key, title_of(event), str(event.get("message", "")), state_of(event), when=event_epoch(event))
            )
        return rows or [Row("lane-none", f"{self.actor}: no events yet", "", "planned")]

    def pins(self, sources: Any) -> list[str]:
        newest: dict[str, dict[str, Any]] = {}
        for event in self.mine(sources):
            newest[str(payload_of(event).get("op") or "status")] = event
        lines = []
        if "step" in newest:
            lines.append(title_of(newest["step"])[:PIN_STEP])
        if "stop" in newest:
            lines.append(title_of(newest["stop"]))
        results = [title_of(newest[op]) for op in ("build", "test") if op in newest]
        if results:
            lines.append(" · ".join(results))
        return lines
