"""Operation events: what each `decomp` call tried and how it came out, written through the one event writer.

The wrappers emit these themselves, so every lane shows up on the stream without changing the lane. The event kinds
are the closed schema 2 set; an `op` in the payload says which operation, and the console reads it back:

  MATCH  payload.op = "attempt"  one `unbake score`: function, version, size_bytes, attempt (per function, counted in
                                 <feed>.attempts.json), words {matched, target, built}, exact, score, variants, unit,
                                 first_difference {built, target, index}, refusals [plain sentence], lane
  MATCH  payload.op = "submit"   one `unbake submit` row: function, exact, refused, score, landed_files, refusals, lane
  PROBE  payload.op = "behave"   one `decomp behave` verdict (BEHAVE emits it with `behave_event`): function, verdict
                                 same|differs|error, checks, differences, inputs, expected, actual, evidence, lane
  PROBE  (Cake Taste's own)      checks, differences, function, result, run_id; written by `taste repro probe` itself
  LAND   payload.op = "landed"   `decomp land`: lanes, functions [names], recipe (proposals, see proposals.py)

`lane` is DECOMP_LANE when set, else the name of the directory that holds the project tree. Emitting never fails
or changes a command: a missing events section or an unwritable feed only prints a note on stderr.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, events, tools
from abu_decomp.config import Profile

VERBS = ("score", "submit", "land")


def lane_of(project: Path | None) -> str:
    if os.environ.get("DECOMP_LANE"):
        return os.environ["DECOMP_LANE"]
    if project:
        return project.resolve().parent.name
    here = Path.cwd().resolve()  # no project (decomp behave): the lane directory the command runs in, if any
    return next((p.name for p in (here, *here.parents) if p.parent.name == "lanes"), "unknown")


def display_name(function: str, version: str | None) -> str:
    """The function without its version suffix: func_802BD430_de -> func_802BD430."""
    suffix = "_" + str(version or "").replace("-", "_")
    return function[: -len(suffix)] if version and function.endswith(suffix) else function


def bump_attempt(feed: Path, function: str) -> int:
    """The per-function attempt counter, kept beside the feed (atomic replace)."""
    state = feed.with_name(feed.name + ".attempts.json")
    try:
        counts = json.loads(state.read_text())
    except (OSError, ValueError):
        counts = {}
    counts[function] = counts.get(function, 0) + 1
    temporary = state.with_suffix(".new")
    temporary.write_text(json.dumps(counts))
    os.replace(temporary, state)
    return counts[function]


def refusal(finding: dict[str, Any]) -> str:
    where = f"{finding['path']}:{finding.get('line', 0)} " if finding.get("path") else ""
    return " ".join(f"{where}{finding.get('key', '')}: {finding.get('message', '')}".split())[:240]


def best_variant(variants: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [v for v in variants if isinstance(v.get("result"), dict) and "score" in v["result"]]
    if not scored:
        return variants[0] if variants else {}
    return max(scored, key=lambda v: (bool(v["result"].get("exact")), v["result"]["score"]))


def attempt_payload(body: dict[str, Any], feed: Path, lane: str) -> dict[str, Any] | None:
    variants = [v for v in body.get("variants") or [] if isinstance(v, dict)]
    if not variants:
        return None
    top = best_variant(variants)
    result = top.get("result") or {}
    members = result.get("members") or []
    function = str(members[0]) if members else Path(str(top.get("file", ""))).stem
    if not function:
        return None
    words = result.get("words") or {}
    first = (result.get("shift") or {}).get("first_difference")
    findings = [f for v in variants for f in v.get("findings") or ()]
    return {
        "op": "attempt",
        "function": function,
        "version": result.get("version"),
        "unit": result.get("unit"),
        "size_bytes": int(words["target"]) * 4 if "target" in words else None,
        "attempt": bump_attempt(feed, function),
        "words": {k: words[k] for k in ("matched", "target", "built") if k in words},
        "exact": bool(result.get("exact")),
        "score": result.get("score"),
        "variants": len(variants),
        "first_difference": first if isinstance(first, dict) else None,
        "refusals": [refusal(f) for f in findings][:5],
        "lane": lane,
    }


def attempt_message(payload: dict[str, Any]) -> str:
    name = display_name(payload["function"], payload.get("version"))
    size = f" ({payload['size_bytes']:,} B)" if payload.get("size_bytes") else ""
    head = f"Tried {name}{size}, attempt {payload['attempt']}: "
    words = payload["words"]
    if payload["exact"]:
        return head + "exact match"
    if "matched" in words:
        return head + f"{words['matched']} of {words.get('target', '?')} words match"
    return head + "no score (the candidate was refused)"


def submit_payloads(body: dict[str, Any], lane: str) -> list[dict[str, Any]]:
    landed = {str(row.get("member")): row.get("files") or [] for row in (body.get("drain") or {}).get("landed") or ()}
    rows = []
    for row in body.get("submissions") or ():
        member = str(row.get("member") or Path(str(row.get("file", ""))).stem)
        rows.append(
            {
                "op": "submit",
                "function": member,
                "exact": bool(row.get("exact")),
                "refused": bool(row.get("refused")),
                "score": row.get("score"),
                "landed_files": landed.get(member, []),
                "refusals": [refusal(f) for f in row.get("findings") or ()][:5],
                "lane": lane,
            }
        )
    return rows


def submit_message(payload: dict[str, Any]) -> str:
    name = payload["function"]
    if payload["refused"]:
        return f"Submitted {name}: refused" + (f" ({payload['refusals'][0]})" if payload["refusals"] else "")
    if payload["landed_files"]:
        return f"Submitted {name}: exact, stored in {', '.join(payload['landed_files'])}"
    return f"Submitted {name}: " + ("exact" if payload["exact"] else "not exact")


def append(profile: Profile, actor: str, status: str, message: str, kind: str, payload: dict[str, Any]) -> None:
    events.append(
        events.events_path(profile),
        {"actor": actor, "status": status, "message": message, "kind": kind, "payload": payload},
    )


def behave_event(profile: Profile, **fields: Any) -> None:
    """The stable schema `decomp behave` emits into: function, verdict (same|differs|error), checks, differences,
    inputs, expected, actual, evidence (path), lane. Anything else is kept as given."""
    verdict = fields.get("verdict")
    if verdict not in ("same", "differs", "error") or not fields.get("function"):
        raise DecompError("narrate.behave", "a behave event needs function and verdict same|differs|error")
    lane = str(fields.setdefault("lane", lane_of(None)))
    name = fields["function"]
    message = {
        "same": f"Checked {name} against the original: same behaviour",
        "differs": f"Checked {name} against the original: behaviour differs",
        "error": f"Checking {name} against the original failed",
    }[verdict]
    append(profile, lane, "passed" if verdict == "same" else "failed", message, "PROBE", {"op": "behave", **fields})


def emit(profile: Profile, tool: str, args: list[str], document: dict[str, Any], project: Path | None) -> None:
    """After `decomp unbake score|submit|land`: one event per attempt or submission. Never raises."""
    verb = next((a for a in args if not a.startswith("-")), None)
    if tool != "unbake" or verb not in VERBS or not isinstance(document, dict):
        return
    try:
        feed = events.events_path(profile)
        lane, body = lane_of(project or tools.settings(profile).project), document.get("result") or {}
        if verb == "score":
            payload = attempt_payload(body, feed, lane)
            if payload:
                status = "passed" if payload["exact"] else "info"
                append(profile, lane, status, attempt_message(payload), "MATCH", payload)
        elif verb == "submit":
            for payload in submit_payloads(body, lane):
                status = "failed" if payload["refused"] else "passed" if payload["exact"] else "info"
                append(profile, lane, status, submit_message(payload), "MATCH", payload)
        else:
            landed = (body.get("landed") or []) if isinstance(body, dict) else []
            payload = {"op": "inbox-land", "count": len(landed), "lane": lane}
            append(profile, lane, "passed", f"Stored {len(landed)} submitted functions in the tree", "MATCH", payload)
    except (DecompError, OSError, KeyError, TypeError, ValueError) as error:
        print(f"decomp: no operation event written: {error}", file=sys.stderr)
