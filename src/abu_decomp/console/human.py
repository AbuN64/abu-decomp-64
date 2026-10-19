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

from .feed import canonical_kind, format_event

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


def sentence(event: dict[str, Any]) -> str:
    payload, kind, actor = payload_of(event), canonical_kind(event), str(event.get("actor", ""))
    message, op = str(event.get("message", "")), payload.get("op")
    if op == "attempt":
        from abu_decomp.narrate import attempt_message

        return attempt_message(payload)
    if op == "submit":
        from abu_decomp.narrate import submit_message

        return submit_message(payload)
    if op == "behave":
        verdict = {"same": "same behaviour", "differs": "behaviour differs", "error": "check failed"}
        return f"Checked {payload.get('function')} against the original: {verdict.get(payload.get('verdict'), '?')}"
    if op == "landed":
        count = len(payload.get("functions") or [])
        if not passed(event):
            return "Landing failed: " + message.partition(": ")[2]
        return f"Landed {count} matched functions (checked on a proof tree)"
    if kind == "PROBE" and event.get("status") == "started":
        name = payload.get("function") or message.rsplit(" ", 1)[-1]
        return f"Probing {name} on the original game"
    if kind == "PROBE" and "checks" in payload:
        name = payload.get("function") or message
        checks, differences = payload["checks"], payload.get("differences", "?")
        head = f"Probed {name} on the original game: {checks} checks, {differences} differences"
        return head if event.get("status") != "failed" else head + " (failed)"
    if kind == "LAND" and actor == "git":
        repo, sha, subject = split_land(message)
        found = FIX.match(subject)
        if found:
            return f"Landed {found[1]} matched functions into {repo} (commit {sha})"
        return f"Committed to {repo} ({sha}): {subject}"
    if actor == "build":
        counts = test_counts(receipt_of(event))
        tests = f": {counts[0]} of {counts[1]} tests" if counts else ""
        state = {"started": "started", "passed": "passed", "failed": "failed"}.get(str(event.get("status")), "ran")
        return f"Build {state}{tests}" if state != "failed" else f"Build failed{failed_step(receipt_of(event))}"
    if kind == "RUN-FAIL":
        return f"{actor} stopped: {message}"
    if kind == "MATCH":
        return message.replace("progress ", "Progress changed: ", 1)
    if kind == "HANDOFF":
        return f"{actor} handed off: {message}"
    if kind == "DECISION":
        return f"{actor} decided: {message}"
    return message or actor


def failed_step(receipt: dict[str, Any]) -> str:
    for step in receipt.get("steps", []):
        code = step.get("returncode")
        if code is not None and code not in step.get("expected_exit_codes", [0]):
            return f" at the {step['name']} step"
    return ""


def lines(*rows: tuple[str, str]) -> str:
    return "\n".join(f"{label + ':':<10}{text}" for label, text in rows if text)


def block(event: dict[str, Any]) -> str:
    """A short unit-test style description: what ran, input, expected (original) vs actual (decomp), result."""
    payload, kind, actor = payload_of(event), canonical_kind(event), str(event.get("actor", ""))
    op, ok = payload.get("op"), passed(event)
    result = "PASS" if ok else "FAIL" if event.get("status") in ("failed", "blocked") else "INFO"
    raw = "\n".join(format_event(event))
    if op == "attempt":
        w, diff = payload.get("words") or {}, payload.get("first_difference") or {}
        body = lines(
            ("Ran", f"compare the decomp candidate with the original assembly (attempt {payload.get('attempt')})"),
            (
                "Input",
                f"{display_name(payload['function'], payload.get('version'))} in {payload.get('unit') or '?'},"
                f" best of {payload.get('variants')} variants",
            ),
            ("Expected", f"original: {w.get('target', '?')} words"),
            ("Actual", f"decomp: {w.get('matched', '?')} words match, {w.get('built', '?')} built"),
            (
                "Differs",
                f"first at {diff.get('built', '?')} (decomp) vs {diff.get('target', '?')} (original)" if diff else "",
            ),
            ("Refused", "; ".join(payload.get("refusals") or [])),
            ("Result", "PASS, exact match" if payload.get("exact") else "FAIL, not exact yet"),
        )
    elif op == "submit":
        body = lines(
            ("Ran", "store the candidate in the tree if it matches the original exactly"),
            ("Input", payload.get("function", "")),
            ("Expected", "exact match with the original assembly"),
            ("Actual", ("exact" if payload.get("exact") else "not exact") + f", score {payload.get('score')}"),
            ("Stored", ", ".join(payload.get("landed_files") or [])),
            ("Refused", "; ".join(payload.get("refusals") or [])),
            ("Result", "FAIL" if payload.get("refused") else "PASS" if payload.get("exact") else "FAIL"),
        )
    elif op == "behave":
        body = lines(
            ("Ran", "compare the decomp function's behaviour with the original game"),
            (
                "Input",
                json.dumps(payload.get("inputs"), default=str)[:300]
                if payload.get("inputs") is not None
                else payload.get("function", ""),
            ),
            (
                "Expected",
                json.dumps(payload.get("expected"), default=str)[:300]
                if payload.get("expected") is not None
                else "the original's behaviour",
            ),
            (
                "Actual",
                json.dumps(payload.get("actual"), default=str)[:300]
                if payload.get("actual") is not None
                else f"{payload.get('checks', '?')} checks, {payload.get('differences', '?')} differences",
            ),
            ("Result", "PASS" if payload.get("verdict") == "same" else "FAIL"),
        )
    elif kind == "PROBE" and "checks" in payload:
        body = lines(
            ("Ran", "observe the original game running this function"),
            ("Input", str(payload.get("question") or payload.get("function") or "")),
            ("Expected", "the original's recorded behaviour"),
            (
                "Actual",
                str(payload.get("result") or f"{payload['checks']} checks, {payload.get('differences')} differences"),
            ),
            ("Result", "PASS, no differences" if ok and not payload.get("differences") else result),
        )
    elif op == "landed":
        gates = payload.get("gates") or {}
        body = lines(
            ("Ran", "the landing gates on a proof tree (apply, setup, sync, build, markers, README)"),
            ("Input", ", ".join(payload.get("functions") or []) or "no functions"),
            ("Expected", "every gate passes"),
            ("Actual", ", ".join(f"{g} {'ok' if v else 'FAILED'}" for g, v in gates.items())),
            ("Result", result),
            ("Recipe", recipe_text(payload.get("recipe") or {})),
        )
    elif actor == "build":
        receipt = receipt_of(event)
        steps = ", ".join(
            f"{s['name']} {'ok' if s.get('returncode') in s.get('expected_exit_codes', [0]) else 'FAILED'}"
            for s in receipt.get("steps", [])
        )
        body = lines(
            ("Ran", "build the committed sources, run the tests and the boot gate"),
            ("Expected", "every step passes"),
            ("Actual", steps or str(event.get("message", ""))),
            ("Result", result),
        )
    else:
        body = lines(("Ran", f"{actor}"), ("Actual", str(event.get("message", ""))), ("Result", result))
    return body + "\n\nRaw event:\n" + raw


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
