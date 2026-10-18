"""`decomp probe request|serve|status`: oracle probe requests as one JSON file each, joined to PROBE events.

Section `probe`: {"queue": path}. A request is `<queue>/requests/<id>.json`, written once by `decomp probe request`
and announced as a HANDOFF event with `subject.address`. The oracle lane lists the open requests oldest first with
`decomp probe serve`; each row carries the `story` object to put in its Taste repro request, whose PROBE events then
carry `payload.request` = id (Cake Taste copies the story into the payload). `decomp probe status` joins requests to
those events:

    open        no PROBE names the request
    running     only a `started` PROBE names it
    answered    a terminal PROBE (info, passed, failed) names it; the newest one is the answer
    superseded  a later request names it in `supersedes`

Nothing here runs a probe and nothing edits another lane's files: requests are only added, never rewritten.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, events
from abu_decomp.config import Profile, check_keys
from abu_decomp.files import atomic_json, sha256_file

SCHEMA = 1
_ADDRESS = re.compile(r"0x[0-9A-Fa-f]{1,16}")
_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}")


def queue(profile: Profile) -> Path:
    table = profile.section("probe")
    keys = frozenset({"queue"})
    check_keys(profile.source_file, "probe", table, keys, keys)
    return profile.path("probe.queue", table["queue"]) / "requests"


def load(directory: Path) -> list[dict[str, Any]]:
    """Every request, oldest first (ids sort by creation time)."""
    rows = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get("schema") == SCHEMA and value.get("id") == path.stem:
            rows.append(value)
    return rows


def story(request: dict[str, Any]) -> dict[str, Any]:
    """The Taste repro `story` for this request; its PROBE events then carry payload.request."""
    return {
        "request": request["id"],
        "question": request["question"],
        "owner_address": request["address"],
        "decomp_path": request.get("decomp_path") or "unknown",
        "match_state": request.get("match_state") or "unknown",
    }


def make_request(
    profile: Profile,
    *,
    requester: str,
    address: str,
    function: str,
    question: str,
    decomp_path: str | None = None,
    match_state: str | None = None,
    evidence: list[Path] = (),
    supersedes: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not _ADDRESS.fullmatch(address):
        raise DecompError("probe.request", f"--address must be 0x followed by hex digits, not {address!r}")
    for name, value in (("requester", requester), ("function", function), ("question", question)):
        if not value.strip() or "\n" in value:
            raise DecompError("probe.request", f"--{name} must be one nonempty line")
    directory = queue(profile)
    if supersedes is not None and not (directory / f"{supersedes}.json").is_file():
        raise DecompError("probe.request", f"--supersedes names no request: {supersedes}")
    files = []
    for path in evidence:
        if not path.is_file():
            raise DecompError("probe.request", f"evidence is not a file: {path}")
        files.append({"path": str(path.resolve()), "sha256": sha256_file(path)})
    body = {
        "schema": SCHEMA,
        "requester": requester.strip(),
        "address": address.lower(),
        "function": function.strip(),
        "question": question.strip(),
        "decomp_path": decomp_path,
        "match_state": match_state,
        "evidence": files,
        "supersedes": supersedes,
    }
    when = (now or datetime.now(UTC)).astimezone(UTC)
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:8]
    request = {"id": f"{when:%Y%m%dT%H%M%SZ}-{digest}", "created": when.isoformat(), **body}
    path = directory / f"{request['id']}.json"
    if path.exists():
        raise DecompError("probe.request", f"request {request['id']} already exists")
    atomic_json(path, request)
    announced = events.append(
        events.events_path(profile),
        {
            "actor": request["requester"],
            "status": "handoff",
            "kind": "HANDOFF",
            "message": f"probe request {request['id']}: {request['question']}",
            "subject": {"address": request["address"], "function": request["function"]},
            "payload": {"request": request["id"], "path": str(path)},
        },
    )
    return {"request": request, "path": str(path), "event": announced.get("event")}


def join(requests: list[dict[str, Any]], probes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each request with its state, its PROBE events and its answer (the newest terminal PROBE)."""
    superseded = {row["supersedes"]: row["id"] for row in requests if row.get("supersedes")}
    rows = []
    for request in requests:
        named = [event for event in probes if (event.get("payload") or {}).get("request") == request["id"]]
        terminal = [event for event in named if event.get("status") != "started"]
        if request["id"] in superseded:
            state = "superseded"
        elif terminal:
            state = "answered"
        elif named:
            state = "running"
        else:
            state = "open"
        answer = terminal[-1] if terminal else None
        rows.append(
            {
                "id": request["id"],
                "state": state,
                "superseded_by": superseded.get(request["id"]),
                "request": request,
                "probes": len(named),
                "answer": None
                if answer is None
                else {
                    "time": answer.get("time"),
                    "status": answer.get("status"),
                    "message": answer.get("message"),
                    "evidence": answer.get("evidence"),
                    "match_state": (answer.get("payload") or {}).get("match_state"),
                },
            }
        )
    return rows


def probe_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return events.tail(path, 1_000_000, {"PROBE"})


def status(profile: Profile, request_id: str | None = None) -> dict[str, Any]:
    if request_id is not None and not _ID.fullmatch(request_id):
        raise DecompError("probe.request", f"not a request id: {request_id}")
    rows = join(load(queue(profile)), probe_events(events.events_path(profile)))
    if request_id is not None:
        rows = [row for row in rows if row["id"] == request_id]
        if not rows:
            raise DecompError("probe.unknown", f"no request {request_id}")
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    return {"counts": counts, "requests": rows}


def serve(profile: Profile) -> dict[str, Any]:
    """The open requests, oldest first, each with the story the oracle lane runs it with."""
    rows = [row for row in status(profile)["requests"] if row["state"] == "open"]
    return {"open": [{"id": row["id"], "request": row["request"], "story": story(row["request"])} for row in rows]}
