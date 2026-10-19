"""Lifecycle panes: Progress, Landings and Lanes. Each reads published files only.

Progress   unbake progress.json (schema 2) per version, plus the head of `unbake report --next`.
Landings   LAND events from the merged feeds.
Lanes      the lane registry (lanes.json), each live lane's PULSE.json and its transcript age.

No pane estimates: every number is a file's own value and every age is a file's own mtime.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .dashboard_model import Dashboard, Row
from .feed import canonical_kind, format_event

PROGRESS_SCHEMA = 2
NEXT_TIMEOUT_SECONDS = 600.0


def age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def stamp(epoch: float) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(epoch)) + " UTC"


def percent(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "n/a"


def progress_versions(document: dict[str, Any]) -> dict[str, dict[str, int]]:
    """version -> the integers `unbake report` prints for it, read from a progress.json schema 2 document."""
    if document.get("schema") != PROGRESS_SCHEMA:
        raise ValueError(f"progress.json schema {document.get('schema')!r}, expected {PROGRESS_SCHEMA}")
    versions = {}
    for cart in document["carts"]:
        versions[cart["version"]] = {
            "code_matched": cart["code"]["matched"],
            "code_total": cart["code"]["total"],
            "code_fuzzy": cart["code"]["fuzzy"],
            "data_matched": cart["data"]["matched"],
            "data_total": cart["data"]["total"],
            "functions_matched": cart["functions"]["matched"],
            "functions_total": cart["functions"]["total"],
            "functions_fuzzy": cart["functions"]["fuzzy"],
            "blobs_decoded": cart["blobs"]["decoded"],
            "blobs_identified": cart["blobs"]["identified"],
            "blobs_total": cart["blobs"]["total"],
            "unknown": cart["unknown"],
        }
    return versions


class NextHead:
    """The head of `unbake report --next`, run off the UI path and cached as a small file.

    The command takes tens of seconds, so it runs in a thread whenever progress.json is newer than the cache.
    The cache file lives in a watched directory: its atomic replace wakes the console like any other source.
    """

    def __init__(self, argv: list[str], cache: Path, count: int, timeout: float = NEXT_TIMEOUT_SECONDS):
        self.argv, self.cache, self.count, self.timeout = argv, cache, count, timeout
        self.thread: threading.Thread | None = None
        self.error = ""

    def refresh_if_older_than(self, source: Path) -> None:
        if self.thread and self.thread.is_alive():
            return
        try:
            if self.cache.stat().st_mtime_ns >= source.stat().st_mtime_ns:
                return
        except FileNotFoundError:
            if not source.exists():
                return
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self) -> None:
        try:
            called = subprocess.run(self.argv, capture_output=True, text=True, timeout=self.timeout, check=False)
            document = json.loads(called.stdout)
            result = document["result"]
            head = {"digest": result.get("digest"), "total": len(result["next"]), "head": result["next"][: self.count]}
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache.with_name(self.cache.name + ".new")
            temporary.write_text(json.dumps(head))
            os.replace(temporary, self.cache)
            self.error = ""
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            self.error = f"{' '.join(self.argv[:3])}: {error}"


class ProgressPane:
    name = "progress"

    def __init__(self, config: dict[str, Any]):
        self.files = [(str(item["label"]), Path(item["path"])) for item in config["files"]]
        self.baseline: dict[str, dict[str, int]] = {}
        nxt = config.get("next")
        self.next = NextHead(list(nxt["argv"]), Path(nxt["cache"]), int(nxt.get("count", 8))) if nxt else None

    def rows(self, sources: Any) -> list[Row]:
        rows: list[Row] = []
        for label, path in self.files:
            document = sources.json(path)
            if not document:
                rows.append(Row(f"progress-{label}", f"{label}: progress.json unavailable", str(path), "stop"))
                continue
            try:
                versions = progress_versions(document)
            except (KeyError, ValueError, TypeError) as error:
                rows.append(Row(f"progress-{label}", f"{label}: {error}", str(path), "stop"))
                continue
            written = path.stat().st_mtime
            base = self.baseline.setdefault(label, {v: dict(c) for v, c in versions.items()})
            for version, cart in versions.items():
                rows.append(self.version_row(label, version, cart, base.get(version), written, path))
            if self.next:
                self.next.refresh_if_older_than(path)
                rows.extend(self.next_rows(label, sources))
        return rows

    @staticmethod
    def version_row(
        label: str, version: str, cart: dict[str, int], base: dict[str, int] | None, written: float, path: Path
    ):
        title = (
            f"{label} {version}  code {percent(cart['code_matched'], cart['code_total'])}"
            f"  data {percent(cart['data_matched'], cart['data_total'])}"
            f"  fn {cart['functions_matched']}/{cart['functions_total']}"
        )
        delta = {key: cart[key] - (base or cart)[key] for key in ("code_matched", "data_matched", "functions_matched")}
        lines = [
            f"code      {cart['code_matched']}/{cart['code_total']} bytes matched, {cart['code_fuzzy']} fuzzy",
            f"data      {cart['data_matched']}/{cart['data_total']} bytes matched",
            f"functions {cart['functions_matched']}/{cart['functions_total']} matched, {cart['functions_fuzzy']} fuzzy",
            f"blobs     {cart['blobs_decoded']} decoded, {cart['blobs_identified']} identified, "
            f"{cart['blobs_total']} total",
            f"unknown   {cart['unknown']} bytes",
            "since console start: " + ", ".join(f"{key.split('_')[0]} {value:+d}" for key, value in delta.items()),
            f"progress.json written {stamp(written)} ({age(time.time() - written)} ago): {path}",
        ]
        return Row(
            f"progress-{label}-{version}", title, "\n".join(lines), "passed" if not any(delta.values()) else "event"
        )

    def next_rows(self, label: str, sources: Any) -> list[Row]:
        assert self.next is not None
        head = sources.json(self.next.cache)
        if not head:
            note = self.next.error or "running unbake report --next; the list appears when it finishes"
            return [Row(f"next-{label}", f"{label} next: pending", note, "planned")]
        rows = [
            Row(
                f"next-{label}-head",
                f"{label} next: {head['total']} items (unbake report --next)",
                head.get("digest") or "",
                "raw",
            )
        ]
        for index, item in enumerate(head["head"]):
            title = (
                f"{label} next {index + 1}  {item['name']} ({item['version']}) {item['size']} B {item['percent']:.0f}%"
            )
            rows.append(
                Row(
                    f"next-{label}-{index}-{item['name']}",
                    title,
                    "\n".join(f"{k}: {v}" for k, v in item.items()),
                    "planned",
                )
            )
        return rows


class LandingsPane:
    name = "landings"

    def __init__(self, config: dict[str, Any]):
        self.limit = int(config.get("limit", 12))

    def rows(self, events: list[dict[str, Any]]) -> list[Row]:
        landed = [event for event in events if canonical_kind(event) == "LAND"]
        rows = []
        for event in reversed(landed[-self.limit :]):
            key = f"land-{event.get('time')}-{event.get('message')}"
            rows.append(
                Row(
                    key,
                    f"{str(event.get('time', ''))[11:19]} {event.get('message', '')}",
                    "\n".join(format_event(event)),
                    "passed",
                )
            )
        if not rows:
            rows.append(Row("land-none", "no LAND events in the feed", "", "planned"))
        return rows


class LanesPane:
    name = "lanes"

    def __init__(self, config: dict[str, Any]):
        self.registry = Path(config["registry"])
        self.pulse = str(config["pulse"])

    def rows(self, sources: Any) -> list[Row]:
        document = sources.json(self.registry)
        lanes = [row for row in document.get("lanes", []) if row.get("state") == "live"]
        rows = []
        for lane in sorted(lanes, key=lambda row: row["name"]):
            pulse = sources.json(Path(self.pulse.format(name=lane["name"])))
            transcript = Path(lane["transcript"]) if lane.get("transcript") else None
            if transcript:
                sources.watched.add(transcript)
            seen = transcript.stat().st_mtime if transcript and transcript.exists() else None
            phase = pulse.get("phase", "no PULSE.json")
            counted = f"{pulse.get('done', 0)}/{pulse.get('total', 0)} {pulse.get('unit', '')}".strip() if pulse else ""
            title = (
                f"{lane['name']}  {lane.get('kind', '?')}/{lane.get('model', '?')}/{lane.get('track', '?')}  {phase}"
            )
            lines = [
                f"progress: {counted or 'none reported'}  commits {pulse.get('commits', 0)}",
                f"blocked: {pulse.get('blocked') or 'no'}",
                "transcript: " + (f"last write {age(time.time() - seen)} ago" if seen else "not found"),
                "owns: " + ", ".join(lane.get("owns", [])),
                f"unit: {lane.get('unit', '?')}",
            ]
            rows.append(
                Row(f"lane-{lane['name']}", title, "\n".join(lines), "stop" if pulse.get("blocked") else "event")
            )
        if not rows:
            rows.append(Row("lane-none", "no live lanes in the registry", str(self.registry), "planned"))
        return rows


class ExperimentsPane:
    """Open and running probe requests with their age, the last PROBE per owner address, and the host counters."""

    name = "experiments"
    uses_events = True

    def __init__(self, config: dict[str, Any]):
        self.requests = Path(config["queue"]) / "requests"
        self.limit = int(config.get("limit", 8))

    def rows(self, sources: Any, events: list[dict[str, Any]]) -> list[Row]:
        from abu_decomp.probe import join, load

        sources.watched.add(self.requests / ".console-watch")
        # every PROBE the feeds still hold (the last 4000 per feed), not only the kind-filtered, shown window
        held = [event for records in sources.records.values() for event in records]
        probes = sorted((e for e in held if canonical_kind(e) == "PROBE"), key=lambda e: str(e.get("time", "")))
        del events
        joined = join(load(self.requests), probes)
        rows = []
        waiting = [row for row in joined if row["state"] in ("open", "running")]
        for row in waiting:
            request = row["request"]
            created = datetime.fromisoformat(request["created"]).timestamp()
            title = (
                f"{row['state']} {age(time.time() - created)}  {request['address']} {request['function']}"
                f"  · {request['question']}"
            )
            lines = [f"{key}: {request.get(key)}" for key in ("id", "requester", "decomp_path", "match_state")]
            lines += [f"evidence: {item['path']} {item['sha256'][:12]}" for item in request.get("evidence", [])]
            rows.append(
                Row(f"req-{row['id']}", title, "\n".join(lines), "planned" if row["state"] == "open" else "event")
            )
        if not waiting:
            rows.append(
                Row("req-none", f"no open probe requests ({len(joined)} in the queue)", str(self.requests), "planned")
            )
        latest: dict[str, dict[str, Any]] = {}
        for event in probes:
            address = str((event.get("payload") or {}).get("owner_address") or "unknown")
            if address != "unknown":
                latest[address] = event
        for address, event in sorted(latest.items(), key=lambda item: str(item[1].get("time")), reverse=True)[
            : self.limit
        ]:
            payload = event.get("payload") or {}
            title = (
                f"{str(event.get('time', ''))[11:19]} {address} [{payload.get('match_state', 'unknown')}]"
                f" {event.get('status')} · {payload.get('question') or event.get('message', '')}"
            )
            state = "stop" if event.get("status") in ("failed", "blocked") else "passed"
            rows.append(Row(f"probe-{address}", title, "\n".join(format_event(event)), state))
        if sources.host is not None:
            host = sources.host
            rows.append(
                Row("host", f"host frames {host.frames} · graphics {host.graphics_tasks} · VI {host.vi}", "", "raw")
            )
        return rows


def _matching(config: dict[str, Any]) -> Any:
    from .matching import MatchingPane

    return MatchingPane(config)


PANES: dict[str, Callable[[dict[str, Any]], Any]] = {
    "matching": _matching,
    "experiments": ExperimentsPane,
    "progress": ProgressPane,
    "landings": LandingsPane,
    "lanes": LanesPane,
}


def lifecycle_model(panes: dict[str, Any], sources: Any, events: list[dict[str, Any]], title: str) -> Dashboard:
    rows: list[Row] = []
    for name, pane in panes.items():
        if getattr(pane, "uses_events", False):
            rows.extend(pane.rows(sources, events))
        else:
            rows.extend(pane.rows(events) if name == "landings" else pane.rows(sources))
    live = sum(1 for row in rows if row.key.startswith("lane-") and row.key != "lane-none")
    landed = sum(1 for row in rows if row.key.startswith("land-") and row.key != "land-none")
    first = next((row.title for row in rows if row.key.startswith("progress-")), "no progress.json")
    heading = f"{title} · {live} live lanes · {landed} landings"
    if "experiments" in panes:
        waiting = sum(1 for row in rows if row.key.startswith("req-") and row.key != "req-none")
        first = next((row.title for row in rows if row.key.startswith("probe-")), "no PROBE yet")
        heading = f"{title} · {waiting} open probe requests"
    footer = ""
    if "matching" in panes:
        active = sum(
            1
            for row in rows
            if row.key.startswith("match-") and not row.key.startswith(("match-idle", "match-none", "match-fail"))
        )
        heading = f"{title} · {active} active lanes"
        first = ""
        footer = panes["matching"].footer(sources)
    return Dashboard(heading, first, rows, rows[0].key if rows else None, footer)
