"""The TAS page's slow side (orch plans/TAS-PAGE-CONTRACT.md, GET /api/board and the lanes/inbox of /api/state):
decomp progress per ROM, what just landed, the ranked open work, the lanes and the integration train.
Every source is a file or folder something else already writes; nothing here writes.
- progress: `tas.serve.states` (progress-states.json, exact / fuzzy / none per version) and the `history.jsonl` beside
  it (one line per progress update: "pct": {version: [exact %, fuzzy %]}), drawn as one sparkline per version;
- landed: the newest commits of the decomp tree the tools work in (`tools.project`), progress-only commits left out;
- work: module tas_rank over the recorded standing (`runs/standing.json`) and the port build's health, the same list
  `tas play --all` ends with; computed off the request path because `owes` reads the whole tree;
- lanes: the folder `tas.serve.lanes`, lane dirs mirrored from the orchestrator (brief.md, STATUS.md, RESULT.md,
  cycle.json; orch/bin/tas-inbox-push.sh): title = the brief's `# ` line (cycle.json "title" wins), kind = its `KIND:`
  line, status = the last STATUS.md line, result = RESULT.md's first line;
- inbox: the train's inbox, each line with its mark (LANDED / DROPPED / DRYRUN-OK) or none while queued.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

MARK = re.compile(r"\|\s*(LANDED|DROPPED|DRYRUN-OK)\b")
KIND = re.compile(r"^KIND:\s*(\S+)")
SPARK_POINTS = 48  # newest progress updates drawn per version
LANDED_SHOWN = 20
PROGRESS_ONLY = re.compile(r"^Update progress to [0-9a-f]+$")
WORK_SECONDS = 120.0  # the ranked list is recomputed at most this often (sooner when the standing changes)


def first_line(path: Path) -> str | None:
    try:
        with path.open(errors="replace") as handle:
            for line in handle:
                if line.strip():
                    return line.strip()
    except OSError:
        return None
    return None


def lane_row(lane: Path) -> dict[str, Any] | None:
    """One mirrored lane dir as a lanes row, None when it has neither a brief nor a cycle.json."""
    try:
        cycle = json.loads((lane / "cycle.json").read_text())
    except (OSError, ValueError):
        cycle = None
    try:
        brief = (lane / "brief.md").read_text(errors="replace")
    except OSError:
        brief = ""
    if not isinstance(cycle, dict) and not brief.strip():
        return None
    cycle = cycle if isinstance(cycle, dict) else {}
    lines = brief.splitlines()
    title = cycle.get("title") or next((ln[2:].strip() for ln in lines if ln.startswith("# ")), lane.name)
    kind = next((m[1] for m in map(KIND.match, lines[:5]) if m), "cycle" if cycle else None)
    status, status_at = None, None
    try:
        with (lane / "STATUS.md").open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - 4096))
            tail = [ln.strip() for ln in handle.read().decode(errors="replace").splitlines() if ln.strip()]
        status = tail[-1] if tail else None
        status_at = (lane / "STATUS.md").stat().st_mtime
    except OSError:
        pass
    return {
        "name": lane.name,
        "title": str(title),
        "kind": kind,
        "status": status,
        "status_at": status_at,
        "result": first_line(lane / "RESULT.md"),
    }


def lanes(folder: Path | None, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every mirrored lane: active ones (no RESULT.md) first, newest status first; `runs` = its running replays."""
    out = []
    for lane in sorted(folder.iterdir()) if folder and folder.is_dir() else []:
        row = lane_row(lane) if lane.is_dir() else None
        if row:
            row["runs"] = sum(1 for r in runs if r["status"] == "running" and r.get("label") == lane.name)
            out.append(row)
    out.sort(key=lambda r: (r["result"] is not None, -(r["status_at"] or 0), r["name"]))
    return out


def inbox_lines(path: Path | None) -> list[dict[str, Any]]:
    """Each inbox line without its mark, and the mark (LANDED / DROPPED / DRYRUN-OK) or None while unlanded."""
    try:
        text = path.read_text(errors="replace") if path else ""
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = MARK.search(line)
        rows.append({"line": line[: match.start()].strip() if match else line, "mark": match[1] if match else None})
    return rows


def none_names(version: dict[str, Any]) -> list[str]:
    """The version's `none_fns` as names (rows are `[name, size, file]` or bare names)."""
    return [str(f[0] if isinstance(f, list) else f) for f in version.get("none_fns") or []]


def progress(states: Path | None) -> dict[str, Any] | None:
    """Per-ROM progress for the page: exact / fuzzy / none of code bytes, function counts, the bridge (NM link and
    boot) and the exact/fuzzy history; None without a states file."""
    try:
        data = json.loads(states.read_text()) if states else None
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        return None
    spark: dict[str, list[list[float]]] = {}
    try:
        lines = (states.parent / "history.jsonl").read_text(errors="replace").splitlines()  # type: ignore[union-attr]
    except OSError:
        lines = []
    for line in lines[-SPARK_POINTS:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        for name, pct in (row.get("pct") or {}).items():
            spark.setdefault(name, []).append([row.get("generated") or 0, *pct[:2]])
    bridge = (data.get("bridge") or {}).get("versions") or {}
    versions = []
    for name, v in (data.get("versions") or {}).items():
        versions.append(
            {
                "name": name,
                "exact_pct": v.get("exact_percent"),
                "fuzzy_pct": v.get("fuzzy_percent"),
                "bytes": v.get("bytes_total"),
                "code": v.get("code") or {},
                "functions": v.get("functions") or {},
                "none_fns": none_names(v),
                "bridge": bridge.get(name),
                "spark": spark.get(name, []),
            }
        )
    return {
        "generated": data.get("generated"),
        "head": data.get("head"),
        "head_subject": data.get("head_subject"),
        "head_time": data.get("head_time"),
        "versions": versions,
    }


def landed(project: Path | None) -> list[dict[str, Any]]:
    """The newest commits of the decomp tree (functions and rows just landed), progress-only commits left out."""
    if not project:
        return []
    out = subprocess.run(
        ["git", "-C", str(project), "log", f"-n{LANDED_SHOWN * 3}", "--format=%H%x1f%ct%x1f%s"],
        capture_output=True,
        text=True,
        check=False,
    )
    rows = []
    for line in out.stdout.splitlines():
        sha, _, rest = line.partition("\x1f")
        when, _, subject = rest.partition("\x1f")
        if subject and not PROGRESS_ONLY.match(subject) and when.isdigit():
            rows.append({"sha": sha, "time": int(when), "subject": subject})
    return rows[:LANDED_SHOWN]


class Work:
    """The ranked open-work list, computed in a background thread: `rows` is the last list (None before the first),
    refreshed when the standing file changes or WORK_SECONDS passed."""

    def __init__(self, make: Any, standing: Path) -> None:
        self.make, self.standing = make, standing
        self.rows: list[dict[str, Any]] | None = None
        self.generated: float | None = None
        self.error: str | None = None
        self.key: Any = None
        self.busy = False
        self.lock = threading.Lock()

    def view(self) -> dict[str, Any]:
        try:
            stamp = self.standing.stat().st_mtime_ns
        except OSError:
            stamp = None
        with self.lock:
            due = self.key != stamp or self.generated is None or time.time() - self.generated > WORK_SECONDS
            if due and not self.busy:
                self.busy = True
                threading.Thread(target=self._refresh, args=(stamp,), daemon=True).start()
            return {"generated": self.generated, "rows": self.rows, "error": self.error}

    def _refresh(self, stamp: Any) -> None:
        try:
            rows, error = self.make(), None
        except Exception as exc:  # the page shows why; the last list stays
            rows, error = self.rows, f"{type(exc).__name__}: {exc}"
        with self.lock:
            self.rows, self.error, self.key, self.generated, self.busy = rows, error, stamp, time.time(), False
