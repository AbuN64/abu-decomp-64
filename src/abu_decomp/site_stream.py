"""Site region `stream`: the lockstep stream of one recorded run (module tas_stream packs it).

GET /api/stream/RUN answers {run, polls, inputs, every}; GET /stream/RUN/SIDE/POLL.jpg answers one frame. A request
packs the shots the run has dumped since the last pack first (the rig prunes them), so a running run's index grows as
its polls land. A refusal is a typed record {kind: "error", code, run, side, poll} with a 404 status, never prose.
"""

from __future__ import annotations

import contextlib
import json
import re
from http import HTTPStatus
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, tas, tas_stream

FRAME = re.compile(r"(\d+)\.jpg")


def refusal(code: str, run: str, side: str | None = None, poll: str | None = None) -> tuple[int, dict[str, Any]]:
    return HTTPStatus.NOT_FOUND, {"kind": "error", "code": code, "run": run, "side": side, "poll": poll}


def folder(server: Any, run_id: str) -> Path | None:
    return tas.run_folder(server.cfg, run_id)


def refresh(run: Path) -> None:
    """Pack what the run has pictured since the last pack; a run with no pictures yet keeps what it has."""
    with contextlib.suppress(DecompError):
        tas_stream.pack(run)


def index(server: Any, run_id: str) -> tuple[int, dict[str, Any]]:
    run = folder(server, run_id)
    if run is None:
        return refusal("stream.run", run_id)
    refresh(run)
    try:
        return HTTPStatus.OK, json.loads((tas_stream.folder(run) / tas_stream.INDEX).read_text())
    except (OSError, ValueError):
        return refusal("stream.empty", run_id)


def answer(server: Any, method: str, rest: str, query: str, body: bytes) -> tuple[int, Any]:
    if method != "GET":
        return HTTPStatus.METHOD_NOT_ALLOWED, {"kind": "error", "code": "stream.method", "run": rest}
    return index(server, rest)


def frame(server: Any, rest: str) -> tuple[int, Any, str]:
    """`RUN/SIDE/POLL.jpg` -> (status, body, content type): the JPEG, else the typed refusal as JSON."""
    run_id, _, tail = rest.partition("/")
    side, _, name = tail.partition("/")

    def fail(code: str) -> tuple[int, Any, str]:
        return (*refusal(code, run_id, side, name), "application/json")

    run = folder(server, run_id)
    if run is None:
        return fail("stream.run")
    if side not in tas_stream.SIDES:
        return fail("stream.side")
    match = FRAME.fullmatch(name)
    path = tas_stream.frame_path(run, side, int(match[1])) if match else None
    if path is None or not path.is_file():
        return fail("stream.poll")
    return HTTPStatus.OK, path.read_bytes(), "image/jpeg"
