"""Site region `fn`: the function screen's record (`GET /api/fn/NAME`; lanes/RUNNER-REDO/PLAN.md).

The answer joins what the bake wrote for the function (`<site>/fn/NAME.json`: code slice, line counts, callers, the
per-call verdict, decoded behave cases) with the run records on disk: the newest run per movie that reached the function
(each with its first poll of the function and whether a recorded stream exists to link) and the unit / set runs that
target it. Cases are typed Case records (the contract in module site_core); nothing here writes prose.
"""

from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path
from typing import Any

from abu_decomp import site_core, tas, tas_coverage

STREAM_INDEX = Path("stream") / "index.json"  # RUN/: present once the run's frames are packed for the stream view
SIDES = ("port", "original")  # the contract's side names; the bake's `oracle` is the `original` here
BAKE_SIDE = {"port": "port", "original": "oracle"}


def side_pair(pick: Any) -> dict[str, Any]:
    return {side: pick(BAKE_SIDE[side]) for side in SIDES}


def string_or_none(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def verdict_case(name: str, call: dict[str, Any]) -> dict[str, Any] | None:
    """The Case of the first diverging call the per-call verdict names (None when every compared call matched)."""
    first = call.get("first")
    if not first:
        return None
    return {
        "function": name,
        "ordinal": first.get("ordinal"),
        "caller": first.get("caller"),
        "frame": side_pair(lambda side: (first.get("frame") or {}).get(side)),
        "inputs": side_pair(lambda side: list((first.get(side) or {}).get("args") or [])),
        "outputs": side_pair(
            lambda side: {
                "status": None,
                "return": string_or_none((first.get(side) or {}).get("ret")),
                "writes": string_or_none((first.get(side) or {}).get("writes")),
            }
        ),
        "divergence": [
            {
                "field": d.get("field"),
                "kind": d.get("kind"),
                "port": string_or_none(d.get("port")),
                "original": string_or_none(d.get("oracle")),
            }
            for d in first.get("diffs") or []
        ],
        "verdict": call.get("verdict"),
        "compared": call.get("compared"),
        "unmatched": {"port": call.get("unmatched_port"), "original": call.get("unmatched_oracle")},
    }


def behave_case(name: str, ordinal: int, row: dict[str, Any]) -> dict[str, Any]:
    """The Case of one decoded behave row: both sides got the same inputs; the divergence is where outputs differ."""
    inputs = [f"{a['where']}={a['value']}" for a in row.get("arguments") or []]
    outputs = row.get("outputs") or {}
    sides = {side: outputs.get(BAKE_SIDE[side]) for side in SIDES}
    divergence = []
    if all(sides.values()):
        for field in ("status", "return"):
            port, original = sides["port"].get(field), sides["original"].get(field)
            if port != original:
                divergence.append({"field": field, "kind": field, "port": port, "original": original})
    return {
        "function": name,
        "ordinal": ordinal,
        "caller": None,
        "frame": {"port": None, "original": None},
        "inputs": {"port": inputs, "original": inputs},
        "outputs": {
            side: None if got is None else {"status": got.get("status"), "return": got.get("return"), "writes": None}
            for side, got in sides.items()
        },
        "divergence": divergence,
        "verdict": row.get("verdict"),
        "compared": row.get("count", 1),
        "unmatched": {"port": 0, "original": 0},
    }


def cases_of(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The function's Case records: the first diverging call of the per-call verdict, then the decoded behave rows."""
    name = record["name"]
    first = verdict_case(name, record.get("call") or {})
    rows = [behave_case(name, ordinal, row) for ordinal, row in enumerate(record.get("behave") or [])]
    return ([first] if first else []) + rows


def first_poll(run: Path, movie: str, record: dict[str, Any]) -> int | None:
    """The first poll the original ran the function in this run: from the run's own function trace, else from the
    bake's first sighting when it was in this run's movie (None when neither names a poll)."""
    got = tas_coverage.read_oracle(run)
    hit = got[0].get(record["name"]) if got else None
    if hit:
        return int(hit[0])
    seen = record.get("oracle") or {}
    return int(seen["poll"]) if seen.get("movie") == movie and seen.get("poll") is not None else None


def runs_of(server: Any, record: dict[str, Any]) -> list[dict[str, Any]]:
    """Run records for the function: the newest run per movie that reached it, and every unit / set run targeting it."""
    name, movies = record["name"], set(record.get("movies") or [])
    newest: dict[str, tuple[Path, dict[str, Any]]] = {}
    out = []
    for path, row in tas.run_rows(server.cfg):
        shown = site_core.run_record(server.row(path, row))
        if shown["kind"] == "tas":
            kept = newest.get(shown["movie"])
            if shown["movie"] in movies and (kept is None or shown["started"] >= kept[1]["started"]):
                newest[shown["movie"]] = (path, shown)
        elif shown["target"] == name:
            out.append({**shown, "stream": False, "first": None})
    for path, shown in newest.values():
        first = first_poll(path, shown["movie"], record)
        out.append({**shown, "stream": (path / STREAM_INDEX).is_file(), "first": first})
    return sorted(out, key=lambda r: (site_core.RUN_KINDS.index(r["kind"]), -r["started"], r["id"]))


def function_record(server: Any, name: str) -> dict[str, Any] | None:
    try:
        record = json.loads((server.site / "fn" / f"{name}.json").read_text())
    except FileNotFoundError:
        return None
    return {
        "name": record["name"],
        "file": record.get("file"),
        "first": record.get("first"),
        "size": record.get("size"),
        "state": record.get("state"),
        "reach": record.get("reach"),
        "behaviour": record.get("behaviour"),
        "html": record.get("html") or [],
        "lines": record.get("lines") or [],
        "callers": record.get("callers") or [],
        "callees": record.get("callees") or [],
        "movies": record.get("movies") or [],
        "cases": cases_of(record),
        "runs": runs_of(server, record),
    }


def answer(server: Any, method: str, rest: str, query: str, body: bytes) -> tuple[int, Any]:
    if method != "GET":
        return HTTPStatus.METHOD_NOT_ALLOWED, {"error": f"{method} is not served here"}
    if not rest or "/" in rest or rest.startswith("."):
        return HTTPStatus.BAD_REQUEST, {"error": "the path names one function: /api/fn/NAME"}
    got = function_record(server, rest)
    if got is None:
        return HTTPStatus.NOT_FOUND, {"error": f"no function {rest}"}
    return HTTPStatus.OK, got
