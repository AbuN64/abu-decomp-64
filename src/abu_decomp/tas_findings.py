"""Findings: everything a batch run, a sibling check or a sweep found, one JSON line in `<tas.runs>/findings.jsonl`.
A cycle stops at the first crash; a batch yields many findings, and module tas_buckets sorts them by root cause so one
lane takes a whole bucket. Writers: `tas play` (a crash, stop, hang or exit of a movie), state-diff, function replay,
the sanitizer build and `decomp sweep`; the reader is the bucket view (`tas triage`, the page's Work tab).

A line: {"id", "time", "kind", "movie", "poll", "signature", "evidence"} plus whatever root the writer knows (all
optional): "run", "batch", "outcome", "subject", "fn", "callers" [fn...], "datum" (the global touched), "field"
(Type.member), "offset" (into the datum or off a null base), "base" ("null"), "region", "fp" (a sweep hit's stable
fingerprint). `signature` is the writer's own one-line root key, used when nothing structured is known.
A clean replay of a movie appends {"kind": "clear", "movie", "clears": [kinds]} so a fixed movie leaves its buckets.
A sanitizer-build run's crash is kind "crash-sanitized" (its layout differs from the page port's, so it neither replaces
nor is cleared by the movie's plain runs); a clean sanitizer run clears only that kind.

The root of a finding (`root`) is the strongest fact it carries, in this order: the struct field touched, the datum
touched, the sweep rule, a null base with its offset in the faulting function, the faulting function with its caller,
the writer's signature, the outcome. The datum comes from the fault address: a game address (the guest RAM range) is
named against the project's symbol file (`names_from` of the project config), a host address against the data symbols
of the executable the run used, and a small address is a field offset off a null pointer.
"""

from __future__ import annotations

import bisect
import fcntl
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

FILE = "findings.jsonl"
BAD = ("crash", "stop", "hang", "exit")  # the outcomes of a movie run that are findings
SANITIZED = "crash-sanitized"  # a crash of a sanitizer-build run (its stops, hangs and exits are the slow build's)
GUEST_BASE, GUEST_END = 0x80000000, 0x80800000  # the guest RAM window of the port (KSEG0 addresses, 8 MB with the pak)
NULL_LIMIT = 0x10000
NEAR = 0x2000  # a guest address further than this past its symbol names no datum
_SYMBOL = re.compile(r"^(\w+)\s*=\s*0x([0-9A-Fa-f]+)\s*;")
_NAME_FN = re.compile(r"^(.+?)(?:\+0x[0-9a-fA-F]+)?$")
_CACHE: dict[Any, Any] = {}


def path(runs: Path) -> Path:
    return runs / FILE


def make_id(row: dict[str, Any]) -> str:
    text = "|".join(str(row.get(k, "")) for k in ("kind", "movie", "poll", "run", "signature", "fp", "evidence"))
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    sig = row.get("signature")
    if isinstance(sig, dict):  # a sibling writer may give its signature as a record: keep it readable
        row = {**row, "signature": json.dumps(sig, sort_keys=True), "signature_detail": sig}
    return row


def read(runs: Path) -> list[dict[str, Any]]:
    """Every parsable line of the feed in file order (a torn or foreign line is skipped, never an error)."""
    rows = []
    try:
        text = path(runs).read_text(errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("kind"):
            rows.append(_clean(row))
    return rows


def append(runs: Path, rows: list[dict[str, Any]]) -> int:
    """Add `rows` (ids filled in) that the feed does not already hold; returns how many were new. Locked: writers run
    in parallel (play --all jobs, a sweep, a sibling lane)."""
    if not rows:
        return 0
    runs.mkdir(parents=True, exist_ok=True)
    target = path(runs)
    with (runs / (FILE + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        have = {r.get("id") for r in read(runs)}
        fresh = []
        for row in rows:
            row = {"time": round(time.time(), 1), **row}
            row["id"] = row.get("id") or make_id(row)
            if row["id"] not in have:
                have.add(row["id"])
                fresh.append(row)
        if fresh:
            with target.open("a") as out:
                out.write("".join(json.dumps(r, sort_keys=True) + "\n" for r in fresh))
        return len(fresh)


# ---- naming the address a fault touched -----------------------------------------------------------------------------


def project_symbols(project: Path | None) -> tuple[list[int], list[str]]:
    """The project's guest symbols as sorted (addresses, names), from `versions/<names_from>/symbol_addrs.txt`."""
    if project is None:
        return [], []
    try:
        config = (project / "config.toml").read_text(errors="replace")
    except OSError:
        return [], []
    match = re.search(r'(?m)^names_from\s*=\s*"([^"]+)"', config)
    file = project / "versions" / (match[1] if match else "") / "symbol_addrs.txt"
    try:
        key = ("names", str(file), file.stat().st_mtime_ns)
    except OSError:
        return [], []
    if key not in _CACHE:
        rows: dict[int, str] = {}
        for line in file.read_text(errors="replace").splitlines():
            m = _SYMBOL.match(line)
            if m and int(m[2], 16) >= GUEST_BASE:
                rows.setdefault(int(m[2], 16), m[1])
        ordered = sorted(rows)
        _CACHE[key] = (ordered, [rows[a] for a in ordered])
    return _CACHE[key]


def host_symbols(port: Path | str) -> tuple[list[int], list[int], list[str]]:
    """The executable's data symbols as sorted (addresses, sizes, names): `nm -S` rows of type d, b, r (any case)."""
    file = Path(port)
    try:
        key = ("host", str(file), file.stat().st_mtime_ns)
    except OSError:
        return [], [], []
    if key not in _CACHE:
        try:
            out = subprocess.run(
                ["nm", "-n", "-S", "--defined-only", str(file)], capture_output=True, text=True, timeout=60, check=False
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        rows = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 4 and parts[2] in "dDbBrRvV":
                rows.append((int(parts[0], 16), int(parts[1], 16), parts[3]))
        _CACHE[key] = ([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows])
    return _CACHE[key]


def locate(address: int, *, project: Path | None = None, port: Path | str | None = None) -> dict[str, Any]:
    """What `address` (the accessed address of a host fault) is: {"base": "null", "offset"} for a small address (a
    field off a null pointer), {"datum", "offset"} for a guest or executable data address, {"region"} otherwise."""
    if address < NULL_LIMIT:
        return {"base": "null", "offset": address}
    guest = address & 0x9FFFFFFF if address >= 0xA0000000 else address  # KSEG1 (uncached) names the same bytes
    if GUEST_BASE <= guest < GUEST_END:
        addrs, names = project_symbols(project)
        at = bisect.bisect_right(addrs, guest) - 1
        if at >= 0 and guest - addrs[at] < NEAR:
            return {"datum": names[at], "offset": guest - addrs[at]}
        return {"region": "guest RAM"}
    if guest >= GUEST_END and guest < 0x90000000:
        return {"region": "past the guest RAM end"}
    if port:
        addrs2, sizes, names2 = host_symbols(port)
        at = bisect.bisect_right(addrs2, address) - 1
        if at >= 0 and address - addrs2[at] < max(sizes[at], 1):
            return {"datum": names2[at], "offset": address - addrs2[at]}
    return {"region": "host"}


def _name(frame: str) -> str:
    match = _NAME_FN.match(frame)
    return match[1] if match else frame


def from_run(
    record: dict[str, Any], run: Path | None = None, *, project: Path | None = None, batch: str | None = None
) -> list[dict[str, Any]]:
    """The findings of one finished movie run: one for a crash, stop, hang or exit, and a clear line for a clean
    replay. Empty for a human run, a stopped run or one without an outcome."""
    movie, outcome = record.get("movie"), record.get("outcome")
    if not movie or record.get("input") == "human" or record.get("stopped") or outcome is None:
        return []
    sanitized = bool(record.get("sanitizer"))
    if outcome == "ok":
        clears = [SANITIZED] if sanitized else list(BAD)
        return [{"kind": "clear", "movie": movie, "clears": clears, "run": record.get("id"), "poll": None}]
    if outcome not in BAD or (sanitized and outcome != "crash"):
        return []
    row: dict[str, Any] = {
        "kind": SANITIZED if sanitized else outcome,
        "movie": movie,
        "poll": record.get("polls_reached"),
        "run": record.get("id"),
        "outcome": outcome,
        "subject": record.get("subject"),
        "evidence": str(run) if run else None,
    }
    if batch:
        row["batch"] = batch
    fault = record.get("fault") if isinstance(record.get("fault"), dict) else None
    frame = str(record.get("frame") or "")
    chain = [_name(x) for x in ([fault.get("at", ""), *fault.get("callers", [])]) if x] if fault else []
    if frame:
        row["fn"] = _name(frame)
        after = chain[chain.index(row["fn"]) + 1 :] if row["fn"] in chain else chain[1:]
        row["callers"] = [c for c in after if not c.startswith("_fini")][:3]
    elif chain:
        row["fn"], row["callers"] = chain[0], chain[1:3]
    elif m := re.search(r"func_[0-9A-Fa-f]{8}\w*", str(record.get("subject") or "")):
        row["fn"] = m[0]
    if fault and fault.get("si_addr"):
        try:
            where = locate(int(str(fault["si_addr"]), 16), project=project, port=fault.get("port"))
        except ValueError:
            where = {}
        for key in ("datum", "offset", "base", "region"):
            if key in where:
                row[key] = where[key]
        row["access"] = fault.get("access")
        row["si_addr"] = fault.get("si_addr")
    row["signature"] = root(row)[1]
    return [row]


def backfill(runs: Path, project: Path | None = None) -> int:
    """Findings for the newest run of every movie that the feed has no line for (runs recorded before this feed)."""
    newest: dict[str, tuple[float, Path, dict[str, Any]]] = {}
    plays = runs / "plays"
    for folder in plays.iterdir() if plays.is_dir() else []:
        try:
            rec = json.loads((folder / "run.json").read_text())
        except (OSError, ValueError):
            continue
        movie = rec.get("movie")
        finished = movie and rec.get("status") == "done" and rec.get("started") and not rec.get("stopped")
        if finished and (movie not in newest or rec["started"] > newest[movie][0]):
            newest[movie] = (rec["started"], folder, rec)
    rows: list[dict[str, Any]] = []
    for _, folder, rec in sorted(newest.values(), key=lambda x: x[0]):
        rows += from_run(rec, folder, project=project)
    return append(runs, rows)


# ---- the root of a finding ------------------------------------------------------------------------------------------


def root(f: dict[str, Any]) -> tuple[str, str]:
    """(level, key): the strongest fact the finding carries, see the module text."""
    if f.get("field"):
        return "field", f"field {f['field']}"
    if f.get("datum"):
        return "datum", f"datum {f['datum']}"
    if f.get("kind") == "sweep" and f.get("signature") and not (f.get("fn") and f.get("callers")):
        return "sweep", str(f["signature"])
    fn = f.get("fn")
    callers = f.get("callers") or []
    if f.get("base") == "null" and fn:
        return "null base", f"null+{int(f.get('offset') or 0):#x} in {fn}"
    if fn:
        return "function", f"{fn} <- {callers[0]}" if callers else str(fn)
    outcome = ": ".join(str(x) for x in (f.get("kind"), f.get("subject") or f.get("outcome")) if x)
    if f.get("signature") and f["signature"] != outcome:
        return "signature", str(f["signature"])
    return "outcome", outcome


def _standing_key(row: dict[str, Any]) -> tuple[Any, ...]:
    if row["kind"] in BAD:  # a movie's newest run replaces its older run outcomes
        return ("run", row.get("movie"))
    if row["kind"] == SANITIZED:  # and its newest sanitizer run its older sanitizer crash
        return ("sanitized run", row.get("movie"))
    return (row["kind"], row.get("movie"), root(row)[1], row.get("fp", ""))


def active(rows: list[dict[str, Any]], batch: str | None = None) -> list[dict[str, Any]]:
    """The findings still standing: a clear line drops its movie's earlier findings of those kinds, a movie's newest
    run outcome replaces its older ones, any other finding stands once per (kind, movie, root, fingerprint);
    `batch` keeps one batch's findings only."""
    out: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        if row["kind"] == "clear":
            for key in [
                k for k, v in out.items() if v.get("movie") == row.get("movie") and v["kind"] in row.get("clears", [])
            ]:
                del out[key]
            continue
        out[_standing_key(row)] = row
    values = list(out.values())
    return [r for r in values if r.get("batch") == batch] if batch else values


def record_run(profile: Any, cfg: dict[str, Any], record: dict[str, Any], run: Path, batch: str | None = None) -> None:
    """The hook of `tas play`: add the run's finding to the feed. Never raises (a feed problem never fails a replay)."""
    try:
        from abu_decomp import tas_triage

        append(cfg["runs"], from_run(record, run, project=tas_triage._project(profile), batch=batch))
    except Exception:
        pass
