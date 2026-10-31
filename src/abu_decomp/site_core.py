"""The Runner site's record contract and live feed (lanes/RUNNER-REDO/PLAN.md); `tas serve` mounts it.

Every feed line is one typed event rendered by the template of its kind; nothing here writes prose. Events are derived
from records that already exist (the run records `tas serve` keeps in live.json, later the feedback records and the lane
journal), so the feed holds no store of its own. An event's identity is stable per subject (a running run keeps one
event that moves its poll), and the live push channel (`/api/feed/stream`, Server-Sent Events) wakes on the Live
watch's signal, never on a clock. The regions (map, function, stream, feedback) answer from their own modules, named
in REGIONS; a region not built yet answers 501 with the card that builds it.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import threading
from http import HTTPStatus
from typing import Any

RUN_KINDS = ("unit", "set", "tas")  # single-function diff, function-set integration, TAS play
FEEDBACK_CATEGORIES = ("crashed", "ui", "glitch", "audio", "input", "other")
FEED_KEPT = 200  # newest events the feed holds and /api/feed answers

# One template per event kind (a kind:variant key when a run kind or a target picks the wording).
TEMPLATES = {
    "run.progress:tas": "{lane} is playing {movie} (poll {poll} of {polls})",
    "run.progress:tas-target": "{lane} is playing toward {target} (poll {poll} of {polls})",
    "run.progress:unit": "{lane} is matching function {target}: {equal}/{cases} cases equal",
    "run.progress:set": "{lane} is running set {target}: {equal}/{cases} cases equal",
    "run.queued": "{lane} queued {movie}",
    "run.ended": "{lane} finished {movie}: {outcome} at poll {poll} of {polls}",
    "run.refused": "{lane} was refused {movie}: {reason}",
    "case.diffed": "{lane} diffed {target}: {equal}/{cases} cases equal, first divergence in {field}",
    "function.matched": "{lane} matched function {target}",
    "feedback.filed": "{lane} flagged {movie} at poll {poll}: {category}",
}
FIELD = re.compile(r"\{(\w+)\}")

# Map filters: a box is lit when any checked filter selects it (bit set in its flags); all unchecked = all dark.
FILTERS = (
    ("named", "C, clean name"),
    ("exact", "C matches"),
    ("asm", "still assembly"),
    ("exercised", "port ran it"),
    ("original-only", "only the original ran it"),
    ("behaves", "calls equal"),
    ("differs", "a call differs"),
    ("feedback", "human flag"),
    ("streamed", "recorded stream hits it"),
)
FILTER_BITS = {key: 1 << index for index, (key, _) in enumerate(FILTERS)}

# Region -> (module that answers it, card that builds it). The module exports `answer(server, method, rest, query,
# body) -> (status, payload)`; site_core only dispatches.
REGIONS = {
    "map": ("site_map", "RUNNER-MAP"),
    "fn": ("site_fn", "RUNNER-FN"),
    "stream": ("site_stream", "RUNNER-STREAM"),
    "feedback": ("site_feedback", "RUNNER-PLAY"),
}


def contract() -> dict[str, Any]:
    """What the page and agents read once: templates, filters with their bits, run kinds, feedback categories."""
    return {
        "templates": TEMPLATES,
        "filters": [{"key": key, "bit": FILTER_BITS[key], "label": label} for key, label in FILTERS],
        "run_kinds": list(RUN_KINDS),
        "feedback_categories": list(FEEDBACK_CATEGORIES),
        "regions": {name: card for name, (_, card) in REGIONS.items()},
    }


# --- records ---


def lane_of(row: dict[str, Any]) -> str:
    """Who drives the run: its lane label, the owner for a human run, else the runner itself."""
    if row.get("label"):
        return str(row["label"])
    return "owner" if row.get("input") == "human" else "runner"


def run_record(row: dict[str, Any]) -> dict[str, Any]:
    """A run row (run.json / live.json) as the site's Run record. Runs without a kind are TAS plays."""
    kind = row.get("kind") or "tas"
    if kind not in RUN_KINDS:
        raise ValueError(f"run {row.get('id')}: kind {kind!r} is not one of {', '.join(RUN_KINDS)}")
    return {
        "id": str(row["id"]),
        "kind": kind,
        "lane": lane_of(row),
        "target": row.get("target"),
        "movie": row.get("movie") or "live play",
        "status": row.get("status") or "queued",
        "poll": int(row.get("poll") or 0),
        "polls": int(row.get("polls") or 0),
        "outcome": row.get("outcome"),
        "started": float(row.get("started") or 0.0),
        "ended": float(row["ended"]) if row.get("ended") else None,
        "sides": ["port", "original"] if row.get("oracle") else ["port"],
        "from": row.get("from"),
        "at": row.get("at"),
        "equal": row.get("equal"),
        "cases": row.get("cases"),
        "error": row.get("error"),
    }


def event_id(kind: str, subject: str) -> str:
    return hashlib.sha1(f"{kind}\0{subject}".encode()).hexdigest()[:16]


def event(kind: str, at: float, lane: str, subject: str, **fields: Any) -> dict[str, Any]:
    """One feed event; its template must exist and every field the template names must be given."""
    if kind not in TEMPLATES:
        raise ValueError(f"no template for event kind {kind!r}")
    missing = [name for name in FIELD.findall(TEMPLATES[kind]) if name != "lane" and fields.get(name) is None]
    if missing:
        raise ValueError(f"event {kind} of {subject}: missing {', '.join(missing)}")
    family = kind.split(":", 1)[0]
    return {"id": event_id(family, subject), "kind": kind, "at": at, "lane": lane, "subject": subject, "fields": fields}


def run_events(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The one event a Run record shows in the feed (its id stays the same while it runs, then the run ends)."""
    common = {"lane": run["lane"], "subject": run["id"]}
    fields = {"run": run["id"], "movie": run["movie"], "poll": run["poll"], "polls": run["polls"]}
    if run["error"]:
        return [event("run.refused", run["ended"] or run["started"], reason=run["error"], **common, **fields)]
    if run["status"] == "queued":
        return [event("run.queued", run["started"], **common, **fields)]
    if run["status"] == "running":
        variant = run["kind"] if run["kind"] != "tas" else ("tas-target" if run["target"] else "tas")
        extra = {"target": run["target"], "equal": run["equal"], "cases": run["cases"]}
        return [event(f"run.progress:{variant}", run["started"], **common, **fields, **extra)]
    return [event("run.ended", run["ended"] or run["started"], outcome=run["outcome"] or "unknown", **common, **fields)]


def render(item: dict[str, Any]) -> str:
    """The plain text of an event (the CLI and tests; the page renders the same template with links)."""

    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        got = item["lane"] if name == "lane" else item["fields"][name]
        return f"{got:,}" if isinstance(got, int) and not isinstance(got, bool) else str(got)

    return FIELD.sub(value, TEMPLATES[item["kind"]])


# --- the feed ---


class Feed:
    """The newest events by identity, a version that moves on every change, and a condition the stream waits on."""

    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.changed: dict[str, int] = {}  # event id -> version it last changed at
        self.version = 0
        self.cond = threading.Condition()

    def publish(self, items: list[dict[str, Any]]) -> int:
        """Merge events; wake every stream when one is new or changed. Returns the number that changed."""
        with self.cond:
            moved = [item for item in items if self.events.get(item["id"]) != item]
            if not moved:
                return 0
            self.version += 1
            for item in moved:
                self.events[item["id"]] = item
                self.changed[item["id"]] = self.version
            for old in self.newest()[FEED_KEPT:]:
                del self.events[old["id"]], self.changed[old["id"]]
            self.cond.notify_all()
            return len(moved)

    def publish_runs(self, rows: list[dict[str, Any]]) -> int:
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                items += run_events(run_record(row))
            except (ValueError, KeyError, TypeError) as error:  # a malformed record is visible, never fatal
                print(f"site feed: skipped run {row.get('id')}: {error}", flush=True)
        return self.publish(items)

    def newest(self) -> list[dict[str, Any]]:
        return sorted(self.events.values(), key=lambda item: (-item["at"], item["id"]))

    def snapshot(self) -> tuple[int, list[dict[str, Any]]]:
        with self.cond:
            return self.version, self.newest()

    def since(self, version: int, wait: float) -> tuple[int, list[dict[str, Any]]]:
        """Events changed after `version`, blocking until one changes or `wait` (the stream's keepalive) passes."""
        with self.cond:
            self.cond.wait_for(lambda: self.version > version, timeout=wait)
            return self.version, [item for item in self.newest() if self.changed[item["id"]] > version]


def sse(name: str, data: Any) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


# --- dispatch ---


def answer(server: Any, method: str, path: str, query: str, body: bytes) -> tuple[int, Any] | None:
    """The site's JSON endpoints under /api; None when `path` is not one of them (tas serve answers it)."""
    if path == "/api/contract" and method == "GET":
        return HTTPStatus.OK, contract()
    if path == "/api/feed" and method == "GET":
        version, items = server.feed.snapshot()
        return HTTPStatus.OK, {"version": version, "events": items}
    head, _, rest = path.removeprefix("/api/").partition("/")
    if not path.startswith("/api/") or head not in REGIONS:
        return None
    module = REGIONS[head][0]
    region = importlib.import_module(f"abu_decomp.{module}")
    return region.answer(server, method, rest, query, body)


def stub(card: str, region: str) -> tuple[int, Any]:
    """A region not built yet: 501 naming the card that builds it."""
    return HTTPStatus.NOT_IMPLEMENTED, {"error": f"the {region} region is not built yet", "card": card}
