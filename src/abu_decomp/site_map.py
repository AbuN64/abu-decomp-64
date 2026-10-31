"""Site region `map`: the function rows `[name, size, state, flags]` the sidebar map draws and the filters light.

`flags` is the bitmask of site_core.FILTER_BITS, built from the baked site (fns.json: decomp state, reach, behaviour)
and the records beside it: a feedback record (`runs/feedback/ID/feedback.json`) sets `feedback` on every function its
run reached, and a run with a packed stream (`runs/plays/ID/stream/index.json`) sets `streamed` on its reach. The
rows are ROM-ordered and the version is a hash of them, so the same data gives the same answer. The page lights a box
when its flags share a bit with the checked filters (`lit`); all unchecked = all dark. `colour_key` is the one rule
for a box's colour (progress; behaves green, differs red) and `COLOURS` maps its key to the page's CSS variable.
"""

from __future__ import annotations

import hashlib
import json
from http import HTTPStatus
from pathlib import Path
from typing import Any

from abu_decomp import site_core

# Progress colour keys -> the shell's CSS variables. Progress runs asm < exact < named; a judged call overrides it.
COLOURS = {
    "asm": "--s-asm",
    "fuzzy": "--warn",
    "exact": "--s-exact",
    "named": "--s-named",
    "behaves": "--b-ok",
    "differs": "--b-diff",
}
DECOMP_FILTERS = ("named", "exact", "asm")  # filter keys that a decomp state sets (fuzzy C is still not a match)
REACH_FILTERS = {"port": ("exercised",), "both": ("exercised",), "oracle": ("original-only",)}
BEHAVIOUR_PREFIX = {"behaves": "behaves", "differs": "differs"}  # behaviour column value -> filter key (prefix match)
BIT = site_core.FILTER_BITS


def reach_rows(body: bytes, count: int) -> set[int]:
    """The fns.json rows a reach.bin bitset names (bit i of byte i>>3); a bitset of another size names none."""
    if len(body) != (count + 7) // 8:
        return set()
    return {i for i in range(count) if body[i >> 3] >> (i & 7) & 1}


def flags_of(state: str, reach: str, behaviour: str, *, feedback: bool = False, streamed: bool = False) -> int:
    """The filter bitmask of one function row."""
    flags = 0
    if state in DECOMP_FILTERS:
        flags |= BIT[state]
    for key in REACH_FILTERS.get(reach, ()):
        flags |= BIT[key]
    for prefix, key in BEHAVIOUR_PREFIX.items():
        if behaviour.startswith(prefix):
            flags |= BIT[key]
    if feedback:
        flags |= BIT["feedback"]
    if streamed:
        flags |= BIT["streamed"]
    return flags


def lit(flags: int, checked: set[str] | list[str]) -> bool:
    """A box is lit when any checked filter selects it; with nothing checked every box is dark."""
    mask = 0
    for key in checked:
        mask |= BIT[key]
    return bool(flags & mask)


def colour_key(state: str, flags: int) -> str:
    """The progress colour of a box: a judged call (differs wins) over the decomp state."""
    if flags & BIT["differs"]:
        return "differs"
    if flags & BIT["behaves"]:
        return "behaves"
    return state if state in COLOURS else "asm"


def runs_with(runs: Path, feedback: Path) -> tuple[list[str], list[str]]:
    """(run ids a feedback record names, run ids with a packed stream), sorted."""
    flagged: set[str] = set()
    for record in sorted(feedback.glob("*/feedback.json")) if feedback.is_dir() else []:
        flagged.add(str(json.loads(record.read_text())["run"]))
    streamed = [p.parent.parent.name for p in sorted(runs.glob("*/stream/index.json"))]
    return sorted(flagged), streamed


def build(site: Path, runs: Path, feedback: Path) -> dict[str, Any]:
    """The /api/map answer from the baked site and the run and feedback folders."""
    manifest = json.loads((site / "manifest.json").read_text())
    baked = json.loads((site / manifest["fns"]).read_text())
    cols = baked["cols"]
    at = {name: cols.index(name) for name in ("name", "size", "state", "reach", "behaviour")}
    rows = baked["rows"]
    flagged, streamed = runs_with(runs, feedback)

    def hits(ids: list[str]) -> set[int]:
        rows_hit: set[int] = set()
        for run in ids:
            try:
                rows_hit |= reach_rows((runs / run / "reach.bin").read_bytes(), len(rows))
            except OSError:
                continue
        return rows_hit

    feedback_rows, streamed_rows = hits(flagged), hits(streamed)
    out = [
        [
            row[at["name"]],
            row[at["size"]],
            row[at["state"]],
            flags_of(
                row[at["state"]],
                row[at["reach"]],
                row[at["behaviour"]] or "",
                feedback=i in feedback_rows,
                streamed=i in streamed_rows,
            ),
        ]
        for i, row in enumerate(rows)
    ]
    version = hashlib.sha1(json.dumps(out, separators=(",", ":")).encode()).hexdigest()[:16]
    return {
        "version": version,
        "ref": baked.get("ref"),
        "filters": [{"key": key, "bit": BIT[key]} for key in BIT],
        "colours": COLOURS,
        "rows": out,
    }


def answer(server: Any, method: str, rest: str, query: str, body: bytes) -> tuple[int, Any]:
    if rest or method != "GET":
        return HTTPStatus.NOT_FOUND, {"error": "not an endpoint", "path": f"/api/map/{rest}", "method": method}
    runs = server.cfg["runs"]
    try:
        return HTTPStatus.OK, build(server.site, runs / "plays", runs / "feedback")
    except (OSError, ValueError, KeyError) as error:
        cause = f"{type(error).__name__}: {error}"
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": "no baked site to map", "cause": cause}
