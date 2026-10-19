"""Matching pane: one block per active lane of what the matching work is doing right now.

    "panes": {"matching": {
        "lanes": "/home/abu/lanes/RW-SOL-*",   glob of lane work dirs; a lane is its dir name
        "outcomes": "done.jsonl",              per lane: JSON lines {"name", "outcome", "bytes"}, newest last
        "scores": "logs/*.log",                per lane: logs holding an unbake JSON envelope, newest first
        "matched": ["exact", "already-exact"], outcomes that count as matched
        "active_seconds": 1800,                a lane is active when one of its files changed this recently
        "progress": {"path": ".../progress.json", "version": "de", "label": "RW"}   footer figures
    }}

Each block names the function being tried (the newest score's first candidate), its latest score in words
(matched out of target words of the best variant), the latest refusal or compile error (path:line key: message)
and the lane's last recorded outcome, marked `landed` once a LAND event names the function. The footer counts
the functions landed today (LAND events whose message follows `decomp land`'s "Fix N ... stand-ins" convention)
and shows the progress.json functions and code percentage. Everything is a file's or an event's own value.
"""

from __future__ import annotations

import glob
import json
import re
import time
from pathlib import Path
from typing import Any

from .dashboard_model import Row
from .feed import canonical_kind

ENVELOPE = '{"findings"'
SCAN_FILES = 8
SCAN_BYTES = 4 * 1024 * 1024
LANDED = re.compile(r"\bFix (\d+) \S+ stand-ins?\b")
DEFAULT_MATCHED = ("exact", "already-exact")


def short(text: Any, limit: int = 78) -> str:
    one = " ".join(str(text).split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def document_of(envelope: dict[str, Any]) -> dict[str, Any]:
    """The unbake result document inside a raw envelope or inside `decomp unbake`'s wrapper of it."""
    result = envelope.get("result")
    while isinstance(result, dict) and isinstance(result.get("document"), dict):
        envelope, result = result["document"], result["document"].get("result")
    return {"findings": envelope.get("findings") or [], "result": result if isinstance(result, dict) else {}}


def envelope_of(text: str) -> dict[str, Any] | None:
    """The last JSON envelope line of a log that holds scored variants."""
    for line in reversed(text.splitlines()):
        if line.startswith(ENVELOPE):
            try:
                found = document_of(json.loads(line))
            except ValueError:
                continue
            if isinstance(found["result"].get("variants"), list):
                return found
    return None


def finding_text(finding: dict[str, Any]) -> str:
    where = f"{finding['path']}:{finding.get('line', 0)} " if finding.get("path") else ""
    return short(f"{where}{finding.get('key', '')}: {finding.get('message', '')}".rstrip(": "))


def best(variants: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [v for v in variants if isinstance(v.get("result"), dict) and "score" in v["result"]]
    return max(scored, key=lambda v: (bool(v["result"].get("exact")), v["result"]["score"])) if scored else {}


def score_words(variants: list[dict[str, Any]]) -> str:
    top = best(variants).get("result") or {}
    words = top.get("words") or {}
    if top.get("exact"):
        return f"exact, all {words.get('target', '?')} words matched"
    if "matched" in words:
        return f"{words['matched']} of {words.get('target', '?')} words matched, best of {len(variants)}"
    if top:
        return f"{100 * top['score']:.0f}% matched, best of {len(variants)}"
    return f"{len(variants)} variants, none scored"


def last_refusal(found: dict[str, Any]) -> str:
    rows = [f for v in found["result"]["variants"] for f in v.get("findings") or ()]
    rows += found["findings"]
    blocking = [f for f in rows if f.get("blocking", True)]
    return finding_text((blocking or rows)[-1]) if rows else ""


def last_row(path: Path, text: str) -> dict[str, Any]:
    for line in reversed(text.splitlines()):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            return row
    return {}


class MatchingPane:
    name = "matching"
    uses_events = True

    def __init__(self, config: dict[str, Any]):
        self.lanes = str(config["lanes"])
        self.outcomes = str(config.get("outcomes", "done.jsonl"))
        self.scores = str(config.get("scores", "logs/*.log"))
        self.matched = tuple(config.get("matched", DEFAULT_MATCHED))
        self.active = float(config.get("active_seconds", 1800))
        self.progress = config.get("progress")

    def lane_dirs(self) -> list[Path]:
        return sorted(Path(p) for p in glob.glob(self.lanes) if Path(p).is_dir())

    def newest_scores(self, lane: Path) -> list[Path]:
        files = [Path(p) for p in glob.glob(str(lane / self.scores))]
        return sorted(files, key=lambda p: p.stat().st_mtime_ns, reverse=True)[:SCAN_FILES]

    def block(self, sources: Any, lane: Path, now: float, landed_text: str) -> Row | None:
        done = lane / self.outcomes
        sources.watched.update({done, lane / ".console-watch"})
        scores = self.newest_scores(lane)
        if scores:
            sources.watched.add(scores[0].parent / ".console-watch")
        touched = [p.stat().st_mtime for p in [done, *scores[:1]] if p.exists()]
        if not touched or now - max(touched) > self.active:
            return None
        found, source = None, None
        for path in scores:
            found = envelope_of(sources.text(path, limit=SCAN_BYTES))
            if found:
                source = path
                break
        variants = found["result"]["variants"] if found else []
        first = (variants[0].get("file") if variants else None) or ""
        function = Path(first).stem if first else "no score yet"
        unit = (best(variants).get("result") or {}).get("unit")
        last = last_row(done, sources.text(done, limit=65536))
        outcome = "no outcome recorded"
        if last:
            name = str(last.get("name", "?"))
            tag = " landed" if name in landed_text else ""
            outcome = short(
                f"{name} {last.get('outcome', '?')}{tag}" + (f" {last['bytes']} B" if "bytes" in last else "")
            )
        refusal = last_refusal(found) if found else ""
        title = f"{lane.name}  {function}  {score_words(variants) if found else 'no score yet'}"
        details = [
            f"trying   {function}" + (f" in {unit}" if unit else ""),
            f"score    {score_words(variants) if found else 'none yet'}",
            f"refusal  {refusal or 'none in the latest score'}",
            f"outcome  {outcome}",
            f"source   {source}" if source else "source   no score log",
        ]
        exact = bool(best(variants).get("result", {}).get("exact"))
        state = "passed" if exact or (last and last.get("outcome") in self.matched and "landed" in outcome) else "event"
        if refusal and not exact:
            state = "stop"
        return Row(f"match-{lane.name}", short(title, 120), "\n".join(details), state, open=True)

    def rows(self, sources: Any, events: list[dict[str, Any]]) -> list[Row]:
        del events
        now = time.time()
        landed_text = "\n".join(
            str(e.get("message", ""))
            for records in sources.records.values()
            for e in records
            if canonical_kind(e) == "LAND"
        )
        rows, idle = [], []
        for lane in self.lane_dirs():
            row = self.block(sources, lane, now, landed_text)
            (rows if row else idle).append(row or lane.name)
        if idle:
            rows.append(Row("match-idle", "idle: " + ", ".join(idle), "no file changed recently", "planned"))
        if not rows:
            rows.append(Row("match-none", "no lane directories", self.lanes, "planned"))
        return rows

    def landed_today(self, sources: Any) -> int:
        today = time.strftime("%Y-%m-%d")
        total = 0
        for records in sources.records.values():
            for event in records:
                if canonical_kind(event) != "LAND":
                    continue
                try:
                    from datetime import datetime

                    when = datetime.fromisoformat(str(event.get("time"))).astimezone()
                except ValueError:
                    continue
                found = LANDED.search(str(event.get("message", "")))
                if found and when.strftime("%Y-%m-%d") == today:
                    total += int(found[1])
        return total

    def footer(self, sources: Any) -> str:
        parts = [f"landed today {self.landed_today(sources)}"]
        if self.progress:
            from .panes import percent, progress_versions

            path = Path(self.progress["path"])
            sources.watched.add(path)
            try:
                cart = progress_versions(sources.json(path))[self.progress["version"]]
                parts.append(
                    f"{self.progress.get('label', 'main')} {self.progress['version']} "
                    f"functions {cart['functions_matched']}/{cart['functions_total']} "
                    f"code {percent(cart['code_matched'], cart['code_total'])}"
                )
            except (KeyError, ValueError, TypeError):
                parts.append(f"{self.progress.get('label', 'main')} progress unavailable")
        return " · ".join(parts)
