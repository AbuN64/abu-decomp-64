"""The TAS page's per-function view and coverage maze (GET /api/fn/<name>, /api/maze, /api/calls/<name>).

Every source is a file something else already writes; nothing here writes or runs a capture:
- behave runs: the invocation records `decomp` leaves per run (module claims; `tas.serve.invocations` names the
  folders or globs of them): time, lane (its working folder, `--project` or candidate path), mode (describe or
  `--candidate`), verdict, case counts, the differing cases (original = recomp vs candidate = decomp values) and the
  candidate source, diffed against the function's current C in the project;
- the function's score and bytes from the project's `docs/map-<version>.json`, its lines of `tas.serve.claims`;
- the maze: the coverage table (module tas_coverage, per-run `covered` indexes) joined with the map's decomp state;
- the call graph: the jal targets of the coverage ELF's code, so a node's callers and callees are static.
"""

from __future__ import annotations

import bisect
import difflib
import glob
import json
import re
import struct
from pathlib import Path
from typing import Any

from abu_decomp import evidence

DIFF_LINES = 300
RUNS_SHOWN = 60
NAME = re.compile(r"^[A-Za-z_][\w.$]*$")
STATES = ("exact", "fuzzy", "none")


# --- behave runs ---


def invocation_files(patterns: list[str]) -> list[Path]:
    found: list[Path] = []
    for pattern in patterns:
        for folder in sorted(glob.glob(pattern)):
            found += sorted(Path(folder).glob("*.json")) if Path(folder).is_dir() else []
    return found


def lane_of(record: dict[str, Any], path: Path, worktrees: set[str]) -> str:
    """The lane a run belongs to: the lane folder its record sits in, else the work tree or lane folder its command
    line, candidate or working folder names, else the folder's own name (`seat` for the shared one)."""
    argv = [str(a) for a in record.get("argv") or []]
    texts = [str(record.get("cwd") or ""), str(record.get("candidate_file") or ""), *argv]
    for text in texts:
        parts = Path(text).parts
        for marker in ("wt", "lanes"):
            if marker in parts and parts.index(marker) + 1 < len(parts):
                return parts[parts.index(marker) + 1]
    parent = path.parent
    return parent.parent.name if parent.name == "invocations" else parent.name


def behave_index(patterns: list[str]) -> dict[str, list[dict[str, Any]]]:
    """function -> its behave invocation records, newest first."""
    out: dict[str, list[dict[str, Any]]] = {}
    for path in invocation_files(patterns):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if record.get("command") != "behave" or not record.get("function"):
            continue
        record["_lane"] = lane_of(record, path, set())
        out.setdefault(record["function"], []).append(record)
    for rows in out.values():
        rows.sort(key=lambda r: -float(r.get("time") or 0))
    return out


def function_text(source: str, name: str) -> str | None:
    """The definition of `name` in a C source (header lines included), None when the file has no such function."""
    fn = evidence.parse(source).get(name)
    if fn is None or fn.kind == evidence.ASM:
        return None
    lines = source.splitlines()
    first = fn.start
    while first > 0 and lines[first - 1].strip() and not re.search(r"[;}]\s*$|^\s*#", lines[first - 1]):
        first -= 1
    return "\n".join(lines[first : fn.end + 1])


def main_source(project: Path | None, name: str, site: str | None) -> dict[str, Any]:
    """The current C of `name` in the project: {file, text, kind} or {} when it has none."""
    if project is None or not site:
        return {}
    rel = site.rpartition(":")[0]
    try:
        source = (project / rel).read_text(errors="replace")
    except OSError:
        return {}
    fn = evidence.parse(source).get(name)
    return {"file": rel, "text": function_text(source, name), "kind": fn.kind if fn else None}


def candidate_text(record: dict[str, Any]) -> str | None:
    if record.get("candidate_source"):
        return str(record["candidate_source"])
    file = record.get("candidate_file")
    if not file:
        for index, word in enumerate(record.get("argv") or []):
            if word == "--candidate" and index + 1 < len(record["argv"]):
                file = record["argv"][index + 1]
    try:
        return Path(file).read_text(errors="replace") if file else None
    except OSError:
        return None


def candidate_diff(record: dict[str, Any], name: str, main: str | None) -> dict[str, Any]:
    text = candidate_text(record)
    if text is None:
        return {"state": "gone"}
    mine = function_text(text, name) or text
    if main is None:
        return {"state": "no-main", "lines": mine.count("\n") + 1}
    diff = list(difflib.unified_diff(main.splitlines(), mine.splitlines(), "main", "candidate", lineterm="", n=2))
    return {"state": "same" if not diff else "diff", "diff": diff[:DIFF_LINES], "cut": max(0, len(diff) - DIFF_LINES)}


def run_view(record: dict[str, Any], name: str, main: str | None) -> dict[str, Any]:
    mode = record.get("mode") or (
        "candidate" if record.get("candidate_file") or "--candidate" in (record.get("argv") or []) else "describe"
    )
    counts = record.get("counts") or {}
    out = {
        "id": record.get("invocation"),
        "time": record.get("time"),
        "lane": record.get("_lane"),
        "mode": "describe" if mode in ("describe", "described") else "candidate",
        "verdict": record.get("verdict"),
        "ok": record.get("ok"),
        "same": counts.get("same"),
        "differs": counts.get("differs"),
        "untestable": counts.get("untestable"),
        "compared": record.get("compared"),
        "total": record.get("total"),
        "reason": record.get("reason"),
        "first": record.get("first_difference"),
        "cases": record.get("cases_detail") or [],
        "candidate": record.get("candidate_file")
        or next(
            (
                record["argv"][i + 1]
                for i, w in enumerate(record.get("argv") or [])
                if w == "--candidate" and i + 1 < len(record["argv"])
            ),
            None,
        ),
        "detailed": "counts" in record,
    }
    out["source"] = candidate_diff(record, name, main) if out["mode"] == "candidate" else None
    return out


def claim_lines(path: Path | None, name: str) -> list[str]:
    try:
        text = path.read_text(errors="replace") if path else ""
    except OSError:
        return []
    return [line.strip() for line in text.splitlines() if name in line.split()][:40]


def map_row(project: Path | None, version: str, name: str) -> dict[str, Any] | None:
    rows = map_rows(project, version)
    return rows.get(name)


def map_rows(project: Path | None, version: str) -> dict[str, dict[str, Any]]:
    """name -> {score, size, file, state} from the project's docs/map-<version>.json."""
    if project is None:
        return {}
    try:
        data = json.loads((project / "docs" / f"map-{version}.json").read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for row in data.get("fns", []):
        name, _, size, score, file = row[0], row[1], row[2], row[3], row[4] or ""
        state = "none" if not file or file.endswith(".s") else "exact" if score >= 100 else "fuzzy"
        out[name] = {"score": score, "size": size, "file": file, "state": state}
    return out


# --- the call graph (static: jal targets of the ELF's code) ---


def elf_calls(elf: Path, funcs: dict[str, tuple[int, int]]) -> dict[str, list[str]]:
    """caller name -> callee names: every `jal` in the function's bytes whose target is the start of a function."""
    try:
        blob = elf.read_bytes()
        shoff, shentsize, shnum = struct.unpack(">I", blob[32:36])[0], *struct.unpack(">HH", blob[46:50])
    except (OSError, struct.error):
        return {}
    sections = []
    for index in range(shnum):
        base = shoff + index * shentsize
        _, kind, flags, addr, offset, size = struct.unpack(">IIIIII", blob[base : base + 24])
        if kind == 1 and flags & 4 and addr:
            sections.append((addr, offset, size))
    starts = sorted((addr, name) for name, (addr, _) in funcs.items())
    keys = [a for a, _ in starts]
    by_addr = dict(starts)

    def word(address: int) -> int | None:
        for addr, offset, size in sections:
            if addr <= address < addr + size:
                return struct.unpack(">I", blob[offset + address - addr : offset + address - addr + 4])[0]
        return None

    calls: dict[str, list[str]] = {}
    for name, (addr, size) in funcs.items():
        seen: dict[str, None] = {}
        for pc in range(addr, addr + size - 3, 4):
            w = word(pc)
            if w is None or w >> 26 != 3:
                continue
            target = (pc & 0xF0000000) | ((w & 0x03FFFFFF) << 2)
            callee = by_addr.get(target)
            if callee is None:
                at = bisect.bisect_right(keys, target) - 1
                callee = starts[at][1] if at >= 0 and target < starts[at][0] + funcs[starts[at][1]][1] else None
            if callee and callee != name:
                seen[callee] = None
        if seen:
            calls[name] = list(seen)
    return calls


def invert(calls: dict[str, list[str]]) -> dict[str, list[str]]:
    callers: dict[str, list[str]] = {}
    for caller, callees in calls.items():
        for callee in callees:
            callers.setdefault(callee, []).append(caller)
    return callers


def static_path(callers: dict[str, list[str]], name: str, live: set[str] | None, limit: int = 12) -> list[str]:
    """A chain of callers from some root down to `name`: breadth first up the callers, staying on functions in `live`
    (a replay's covered set) when it is given; the shortest chain to a function nothing in `live` calls."""
    parent: dict[str, str | None] = {name: None}
    queue = [name]
    best = name
    for current in queue:
        up = [c for c in callers.get(current, []) if c not in parent and (live is None or c in live)]
        if not up and current != name:
            best = current
            break
        for caller in up:
            parent[caller] = current
            queue.append(caller)
        if len(parent) > 20000:
            break
    chain = []
    node: str | None = best
    while node is not None and len(chain) < limit:
        chain.append(node)
        node = parent.get(node)
    return chain


# --- the maze ---


def module_of(file: str) -> str:
    parts = file.split("/")
    return "/".join(parts[1:-1]) if len(parts) > 2 and parts[0] == "src" else "/".join(parts[:-1]) or "(no source)"


def movie_covered(table: dict[str, Any]) -> tuple[list[list[int]], bool]:
    """Per movie, the table's function indexes it covered, and whether those are exact. A table written before
    per-run indexes were kept only knows each function's first movie: the sets then hold what each reached first."""
    runs = table.get("runs") or []
    if runs and all("covered" in r for r in runs):
        return [list(r["covered"]) for r in runs], True
    index = {r["movie"]: i for i, r in enumerate(runs)}
    sets: list[list[int]] = [[] for _ in runs]
    for i, row in enumerate(table.get("functions") or []):
        first = (row["port"] if row["port"]["hit"] else row["oracle"])["movie"]
        if first in index:
            sets[index[first]].append(i)
    return sets, False


def maze_view(table: dict[str, Any], mapped: dict[str, dict[str, Any]], tiles: list[dict[str, Any]]) -> dict[str, Any]:
    """The coverage maze payload: modules of cells [name, bytes, state, covered, first movie index or -1], the
    replays (tiles: name, start, status, polls) with the cells each covered, and the headline numbers."""
    functions = table.get("functions") or []
    sets, exact = movie_covered(table)
    rows = [(i, r) for i, r in enumerate(functions) if r.get("scope") == "game" and r.get("in_version")]
    cell_of = {i: n for n, (i, _) in enumerate(rows)}
    modules: dict[str, list[Any]] = {}
    first_of: dict[int, int] = {}
    for m, members in enumerate(sets):
        for i in members:
            first_of.setdefault(i, m)
    for i, r in rows:
        hit = r["port"]["hit"] or r["oracle"]["hit"]
        got = mapped.get(r["name"])
        state = got["state"] if got else ("none" if str(r.get("file", "")).endswith(".s") else "exact")
        modules.setdefault(module_of(r.get("file", "")), []).append(
            [r["name"], r["size"], state, 1 if hit else 0, first_of.get(i, -1)]
        )
    order = sorted(modules.items(), key=lambda kv: kv[1][0][0])
    total, covered = len(rows), sum(1 for _, r in rows if r["port"]["hit"] or r["oracle"]["hit"])
    real = sum(1 for _, r in rows if (mapped.get(r["name"]) or {}).get("state", "exact") != "none")
    flat = [c for _, cells in order for c in cells]
    flat_index = {c[0]: n for n, c in enumerate(flat)}
    runs = table.get("runs") or []
    replays = []
    for m, run in enumerate(runs):
        cells = sorted(
            cell_of_flat
            for i in sets[m]
            if i in cell_of and (cell_of_flat := flat_index.get(functions[i]["name"])) is not None
        )
        replays.append(
            {**(tiles[m] if m < len(tiles) else {"name": run["movie"]}), "run": run.get("run"), "cells": cells}
        )
    return {
        "generated": table.get("generated"),
        "version": (table.get("summary") or {}).get("version"),
        "exact": exact,
        "total": total,
        "covered": covered,
        "real_c": real,
        "covered_pct": round(100.0 * covered / total, 2) if total else 0.0,
        "real_pct": round(100.0 * real / total, 2) if total else 0.0,
        "bytes": sum(r["size"] for _, r in rows),
        "modules": [{"name": n, "cells": cells} for n, cells in order],
        "replays": replays,
        "movies": [r["movie"] for r in runs],
    }


def fn_index(table: dict[str, Any]) -> dict[str, int]:
    return {r["name"]: i for i, r in enumerate(table.get("functions") or [])}
