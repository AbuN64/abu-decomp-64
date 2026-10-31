"""Site region `feedback`: a human flags a run at a poll; the record lands in `<tas.runs>/feedback/<id>/`.

A Feedback record is `{id, at, run, poll, category, text, lane, movie, inputs[{poll,buttons,x,y}],
frames{port,original}}` (lanes/RUNNER-REDO/PLAN.md) in `feedback.json` beside `port.jpg` / `original.jpg`. Both frames
are the newest picture at or before the flagged poll (the packed stream, else the oracle's shots, else the port's live
view for the port side), and the inputs are the last INPUT_WINDOW polls of pad 0 from the run's `polls.raw`, so a
record stays readable after the run's pictures are pruned. Filing publishes one `feedback.filed` event to the feed,
which wakes the push stream.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from http import HTTPStatus
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, site_core, tas, tas_oracle

TEXT_MAX = 2000  # characters of the human's note
INPUT_WINDOW = 120  # polls of pad 0 kept before and at the flagged poll (2 s at 60 polls/s)
RECORD = "feedback.json"
SIDES = {"port": "port", "original": tas_oracle.SIDES[1]}  # feedback side -> the oracle's side folder
STREAM = "stream"  # packed pictures: stream/<side>/<poll>.jpg
ID = re.compile(r"[0-9a-f]{12}")


def folder(runs: Path) -> Path:
    return runs / "feedback"


def check(body: Any) -> tuple[str, int, str, str]:
    """(run, poll, category, text) of a filing request; anything else is refused with the field named."""
    if not isinstance(body, dict):
        raise ValueError("body: expected a JSON object {run, poll, category, text}")
    run, poll, category, text = body.get("run"), body.get("poll"), body.get("category"), body.get("text", "")
    if not isinstance(run, str) or not run:
        raise ValueError("run: expected a run id")
    if isinstance(poll, bool) or not isinstance(poll, int) or poll < 0:
        raise ValueError("poll: expected a poll index, an integer of 0 or more")
    if category not in site_core.FEEDBACK_CATEGORIES:
        raise ValueError(f"category: expected one of {', '.join(site_core.FEEDBACK_CATEGORIES)}")
    if not isinstance(text, str) or len(text) > TEXT_MAX:
        raise ValueError(f"text: expected a string of at most {TEXT_MAX} characters")
    return run, poll, category, text.strip()


def record_id(run: str, poll: int, category: str, text: str, at: float) -> str:
    return hashlib.sha1(f"{run}\0{poll}\0{category}\0{text}\0{at!r}".encode()).hexdigest()[:12]


def inputs_at(run: Path, poll: int) -> list[dict[str, int]]:
    """Pad 0 of the last INPUT_WINDOW polls ending at `poll` (an empty list when the run recorded none)."""
    source = run / "polls.raw"
    if not source.is_file():
        return []
    rows = tas.read_raw(source)[max(0, poll - INPUT_WINDOW + 1) : poll + 1]
    first = max(0, poll - INPUT_WINDOW + 1)
    return [{"poll": first + i, "buttons": row[0], "x": row[1], "y": row[2]} for i, row in enumerate(rows)]


def newest_at(folder_: Path, poll: int, suffix: str, stem: str = "") -> Path | None:
    """The file `<stem><poll><suffix>` in `folder_` with the highest poll that is at or before `poll`."""
    if not folder_.is_dir():
        return None
    best: tuple[int, Path] | None = None
    pattern = re.compile(rf"{re.escape(stem)}(\d+){re.escape(suffix)}")
    for path in folder_.iterdir():
        match = pattern.fullmatch(path.name)
        if match and int(match.group(1)) <= poll and (best is None or int(match.group(1)) > best[0]):
            best = (int(match.group(1)), path)
    return best[1] if best else None


def frame_path(run: Path, side: str, poll: int) -> Path | None:
    """Where the picture of `side` at `poll` lives now: the packed stream, the oracle's shots, the port's live view."""
    return (
        newest_at(run / STREAM / side, poll, ".jpg")
        or newest_at(run / tas_oracle.SHOTS / SIDES[side], poll, ".ppm", "poll-")
        or ((run / tas.VIEW) if side == "port" and (run / tas.VIEW).is_file() else None)
    )


def build(
    run_id: str, row: dict[str, Any], poll: int, category: str, text: str, inputs: list[dict[str, int]], at: float
) -> dict[str, Any]:
    """The Feedback record before its frames are written: `frames` names the files each side will hold."""
    run = site_core.run_record({**row, "id": run_id})
    return {
        "id": record_id(run_id, poll, category, text, at),
        "at": at,
        "run": run_id,
        "poll": poll,
        "category": category,
        "text": text,
        "lane": run["lane"],
        "movie": run["movie"],
        "inputs": inputs,
        "frames": {"port": None, "original": None},
    }


def write(server: Any, record: dict[str, Any], sources: dict[str, Path | None]) -> None:
    """Frames first (JPEG files), then feedback.json by rename, so a listed record is always complete."""
    where = folder(server.cfg["runs"]) / record["id"]
    where.mkdir(parents=True, exist_ok=True)
    for side, source in sources.items():
        if source is not None:
            (where / f"{side}.jpg").write_bytes(server.capture(source, "JPEG")[1])
            record["frames"][side] = f"{side}.jpg"
    scratch = where / f"{RECORD}.part"
    scratch.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    os.replace(scratch, where / RECORD)


def event_of(record: dict[str, Any]) -> dict[str, Any]:
    """The `feedback.filed` event of a record; its id is stable per record."""
    return site_core.event(
        "feedback.filed",
        record["at"],
        record["lane"],
        record["id"],
        run=record["run"],
        movie=record["movie"],
        poll=record["poll"],
        category=record["category"],
        feedback=record["id"],
    )


def read(runs: Path, feedback_id: str) -> dict[str, Any] | None:
    if not ID.fullmatch(feedback_id):
        return None
    try:
        return json.loads((folder(runs) / feedback_id / RECORD).read_text())  # type: ignore[no-any-return]
    except (OSError, ValueError):
        return None


def records(runs: Path) -> list[dict[str, Any]]:
    """Every filed record, newest first."""
    root = folder(runs)
    found = [read(runs, path.name) for path in root.iterdir()] if root.is_dir() else []
    return sorted((r for r in found if r), key=lambda r: (-r["at"], r["id"]))


def file(server: Any, body: Any, at: float | None = None) -> dict[str, Any]:
    """File one record for a run at a poll and publish its event; ValueError names the refused field."""
    run_id, poll, category, text = check(body)
    run = server.run_dir(run_id)
    row = tas.read_record(run) if run else None
    if run is None or not row:
        raise ValueError(f"run: no run {run_id}")
    record = build(run_id, row, poll, category, text, inputs_at(run, poll), time.time() if at is None else at)
    write(server, record, {side: frame_path(run, side, poll) for side in SIDES})
    server.feed.publish([event_of(record)])
    return record


def answer(server: Any, method: str, rest: str, query: str, body: bytes) -> tuple[int, Any]:
    """GET /api/feedback (list), GET /api/feedback/ID (one), POST /api/feedback (file)."""
    if method == "POST" and not rest:
        try:
            return HTTPStatus.CREATED, file(server, json.loads(body or b"{}"))
        except ValueError as error:
            return HTTPStatus.BAD_REQUEST, {"error": str(error)}
    if method == "GET" and not rest:
        found = records(server.cfg["runs"])
        server.feed.publish([event_of(r) for r in found])  # a restarted server shows held flags again
        return HTTPStatus.OK, {"feedback": found}
    if method == "GET":
        found_one = read(server.cfg["runs"], rest)
        if found_one is None:
            return HTTPStatus.NOT_FOUND, {"error": f"no feedback {rest}"}
        return HTTPStatus.OK, found_one
    return HTTPStatus.METHOD_NOT_ALLOWED, {"error": f"{method} {rest or '/'} is not served"}


def command(runs: Path, verb: str, feedback_id: str | None) -> dict[str, Any]:
    """`decomp tas feedback list|show`: the records as typed fields (the list omits inputs; show has everything)."""
    if verb == "list":
        found = records(runs)
        keys = ("id", "at", "run", "poll", "category", "lane", "movie")
        return {"total": len(found), "feedback": [{k: r[k] for k in keys} for r in found]}
    if not feedback_id:
        raise DecompError("tas.feedback", "show needs the feedback id")
    record = read(runs, feedback_id)
    if record is None:
        raise DecompError("tas.feedback", f"no feedback {feedback_id}")
    return {**record, "folder": str(folder(runs) / feedback_id)}
