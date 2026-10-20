"""Plain-language reading of events: one sentence per operation, and a unit-test style block to expand.

The tiles never show raw event kinds. Operation events carry `payload.op` (see abu_decomp.narrate); the other
events (Cake Taste probes, git landings, builds, failures) are read from the fields they already carry.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from abu_decomp.narrate import display_name

from .feed import canonical_kind

FIX = re.compile(r"^Fix (\d+) \S+ stand-ins?\b")
TESTS = re.compile(r"(\d+)% tests passed, (\d+) tests failed out of (\d+)")


def payload_of(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("payload")
    return value if isinstance(value, dict) else {}


def passed(event: dict[str, Any]) -> bool:
    return event.get("status") == "passed"


@lru_cache(maxsize=64)
def _receipt(path: str, mtime: int) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def receipt_of(event: dict[str, Any]) -> dict[str, Any]:
    evidence = event.get("evidence")
    path = evidence.get("path") if isinstance(evidence, dict) else evidence
    if not path or not str(path).endswith(".json"):
        return {}
    try:
        return _receipt(str(path), Path(path).stat().st_mtime_ns)
    except OSError:
        return {}


def test_counts(receipt: dict[str, Any]) -> tuple[int, int] | None:
    """(passed, total) from the receipt's ctest log."""
    for step in receipt.get("steps", []):
        if step.get("name") == "ctest" and step.get("log"):
            try:
                found = TESTS.search(Path(step["log"]).read_text(errors="replace"))
            except OSError:
                return None
            if found:
                return int(found[3]) - int(found[2]), int(found[3])
    return None


def split_land(message: str) -> tuple[str, str, str]:
    repo, _, rest = message.partition(" ")
    sha, _, subject = rest.partition(" ")
    return repo, sha, subject


def words(payload: dict[str, Any]) -> str:
    w = payload.get("words") or {}
    return f"{w['matched']} of {w.get('target', '?')} words" if "matched" in w else "no score"


def results_only(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop every 'started' event: a console shows results, never that something began."""
    return [event for event in events if event.get("status") != "started"]


def percent_matched(payload: dict[str, Any]) -> str | None:
    """Words matched of the target as a percentage truncated to one decimal (never rounded up to 100)."""
    if payload.get("exact"):
        return "100.0"
    words = payload.get("words") or {}
    if "matched" not in words or not words.get("target"):
        return None
    return f"{int(1000 * words['matched'] / words['target']) / 10:.1f}"


def sentence(event: dict[str, Any]) -> str:
    """The collapsed row: `subject: value`, no prose."""
    payload, kind, actor = payload_of(event), canonical_kind(event), str(event.get("actor", ""))
    message, op = str(event.get("message", "")), payload.get("op")
    name = display_name(str(payload.get("function") or ""), payload.get("version"))
    if op == "attempt":
        if payload.get("exact"):
            return f"{name}: CRACKED!"
        found = percent_matched(payload)
        return f"{name}: {found}% match" if found else f"{name}: refused"
    if op == "submit":
        state = "refused" if payload.get("refused") else "stored" if payload.get("landed_files") else "exact"
        return f"{name}: {state if payload.get('refused') or payload.get('exact') else 'not exact'}"
    if op == "behave":
        verdict = {"same": "same as original", "differs": "differs from original"}.get(
            payload.get("verdict"), "check could not run"
        )
        return f"{name}: {verdict}"
    if op == "landed":
        count = len(payload.get("functions") or [])
        if not passed(event):
            return "landing failed: " + message.partition(": ")[2]
        names = suggested_names(payload.get("recipe") or {})
        where = names[0][1] or names[0][0] if names else "no name proposed"
        return f"landed {count} fn \u2192 {where}" + (f" (+{payload['bytes']} B)" if payload.get("bytes") else "")
    if kind == "PROBE" and payload.get("checks") is not None:
        checks, differences = payload["checks"], payload.get("differences") or 0
        head = f"{name or message}: {'same' if not differences else 'differs'} ({checks - differences}/{checks})"
        return head if event.get("status") != "failed" else head + " failed"
    if kind == "LAND" and actor == "git":
        repo, sha, subject = split_land(message)
        found = FIX.match(subject)
        return f"landed {found[1]} fn \u2192 {repo} {sha}" if found else f"{repo} {sha}: {subject}"
    if actor == "build":
        counts = test_counts(receipt_of(event))
        state = str(event.get("status"))
        if state == "failed":
            return f"build \u2717{failed_step(receipt_of(event)).replace(' at the ', ' ').replace(' step', '')}"
        mark = "\u2713" if state == "passed" else state
        return f"build {mark}" + (f" {counts[0]}/{counts[1]}" if counts else "")
    if kind == "RUN-FAIL":
        return f"{actor}: {message}"
    if kind == "MATCH":
        return message.replace("progress ", "progress ", 1)
    if kind == "HANDOFF":
        return f"{actor} handoff: {message}"
    if kind == "DECISION":
        return f"{actor} decision: {message}"
    return message or actor


def failed_step(receipt: dict[str, Any]) -> str:
    for step in receipt.get("steps", []):
        code = step.get("returncode")
        if code is not None and code not in step.get("expected_exit_codes", [0]):
            return f" at the {step['name']} step"
    return ""


def lines(*rows: tuple[str, str]) -> str:
    return "\n".join(f"{label:<11}{text}" for label, text in rows if text)


def value(item: Any) -> str:
    return json.dumps(item, default=str)[:200]


def block(event: dict[str, Any]) -> str:
    """The expanded row: expected vs actual and the first difference, values only, then the evidence path."""
    evidence = event.get("evidence")
    path = evidence.get("path") if isinstance(evidence, dict) else evidence
    source = payload_of(event).get("decomp_path")
    tail = lines(("source", str(source) if source and source != "unknown" else ""), ("evidence", str(path or "")))
    return "\n".join(part for part in (_block(event), tail) if part)


def _block(event: dict[str, Any]) -> str:
    payload, kind, actor = payload_of(event), canonical_kind(event), str(event.get("actor", ""))
    op = payload.get("op")
    if op == "attempt":
        w, diff = payload.get("words") or {}, payload.get("first_difference") or {}
        return lines(
            ("expected", f"{w.get('target', '?')} words"),
            ("actual", f"{w.get('matched', '?')} match of {w.get('built', '?')} built"),
            ("first diff", f"built {diff.get('built', '?')} \u2260 original {diff.get('target', '?')}" if diff else ""),
            ("variants", str(payload.get("variants", ""))),
            ("refused", "; ".join(payload.get("refusals") or [])),
        )
    if op == "submit":
        return lines(
            ("score", str(payload.get("score"))),
            ("stored", ", ".join(payload.get("landed_files") or [])),
            ("refused", "; ".join(payload.get("refusals") or [])),
        )
    if op == "behave":
        return lines(
            ("input", value(payload["inputs"]) if payload.get("inputs") is not None else ""),
            ("expected", value(payload["expected"]) if payload.get("expected") is not None else ""),
            ("actual", value(payload["actual"]) if payload.get("actual") is not None else ""),
            ("checks", f"{payload.get('checks', '?')}, differences {payload.get('differences', '?')}"),
            ("reason", str(payload.get("reason") or "")),
        )
    if kind == "PROBE" and payload.get("checks") is not None:
        return lines(
            ("question", str(payload.get("question") or "")),
            ("checks", f"{payload['checks']}, differences {payload.get('differences')}"),
            ("result", str(payload.get("result") or "")),
        )
    if op == "landed":
        gates = payload.get("gates") or {}
        return lines(
            ("functions", ", ".join(payload.get("functions") or [])),
            ("gates", " ".join(f"{g}={'ok' if v else 'FAILED'}" for g, v in gates.items())),
            ("recipe", recipe_text(payload.get("recipe") or {})),
        )
    if actor == "build":
        receipt = receipt_of(event)
        steps = " ".join(
            f"{s['name']}={'ok' if s.get('returncode') in s.get('expected_exit_codes', [0]) else 'FAILED'}"
            for s in receipt.get("steps", [])
        )
        return lines(("steps", steps), ("message", str(event.get("message", "")) if not steps else ""))
    return lines(("message", str(event.get("message", ""))), ("status", str(event.get("status", ""))))


def recipe_text(recipe: dict[str, Any]) -> str:
    out = []
    for step, summary in recipe.items():
        if not summary.get("ok"):
            out.append(f"{step}: no proposal ({summary.get('error', '?')})")
            continue
        out.append(f"{step}: {summary.get('moves', 0)} moves proposed")
        out.extend("  " + suggestion_text(item) for item in summary.get("head") or [])
    return "\n".join(out)


def suggestion_text(item: str) -> str:
    try:
        op = json.loads(item)
    except ValueError:
        return item
    return f"{op.get('op', '?')}: {op.get('subject', '?')} -> {op.get('target', '?')}"


def suggested_names(recipe: dict[str, Any]) -> list[tuple[str, str]]:
    """(module name, source file) Recipe proposes, from its structure suggestions."""
    ops = []
    for summary in recipe.values():
        for item in summary.get("head") or []:
            try:
                ops.append(json.loads(item))
            except ValueError:
                continue
    sources = {o.get("subject"): o.get("target") for o in ops if o.get("op") == "source"}
    return [(o["target"], sources.get(o["target"], "")) for o in ops if o.get("op") == "module" and o.get("target")]
