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
  MATCH  payload.op = "recipe"   one `decomp recipe` verb (next work try skip structure name apply flags style): verb,
                                 sub, ok, counts (moves accepted refused targets files), item, next, lane; apply is LAND
  MATCH  payload.op = <tool>     every other tool call that returns a JSON document (`decomp unbake verify`, `decomp
                                 recipe status`, `decomp taste ...`): verb, ok, seconds, findings, lane (RUN-FAIL
                                 when it fails); `decomp make` writes op "build" the same way (proof.py). Text
                                 calls (--help, --version) write nothing.
  LAND   payload.op = "landed"   `decomp land`: lanes, functions [names], recipe (proposals, see proposals.py)

`lane` is DECOMP_LANE when set, else the name of the directory that holds the project tree, else the lane
directory (a `lanes/NAME` ancestor) of the cwd, the environment's paths or a parent process's cwd. Emitting never fails
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

VERBS = ("score", "submit", "land")  # unbake verbs with a richer payload than the default event


def lane_of(project: Path | None) -> str:
    if os.environ.get("DECOMP_LANE"):
        return os.environ["DECOMP_LANE"]
    if project:
        return project.resolve().parent.name
    for place in lane_places():
        lane = next((p.name for p in (place, *place.parents) if p.parent.name == "lanes"), None)
        if lane:
            return lane
    return "unknown"


def lane_places() -> list[Path]:
    """Where a lane can be found when no project names it: the command's cwd, then the directories its environment
    names (TMPDIR, PWD, OLDPWD, a lane's scratch), then the cwd of each parent process (the shell the lane cd'd in)."""
    places = [Path.cwd()]
    places += [Path(os.environ[name]) for name in ("PWD", "OLDPWD", "TMPDIR") if os.environ.get(name)]
    pid = os.getppid()
    for _ in range(8):
        if pid <= 1:
            break
        try:
            places.append(Path(os.readlink(f"/proc/{pid}/cwd")))
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            pid = int(fields[1])
        except (OSError, ValueError, IndexError):
            break
    resolved = []
    for place in places:
        try:
            resolved.append(place.resolve())
        except OSError:
            continue
    return resolved


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


RECIPE_VERBS = ("next", "work", "try", "skip", "structure", "name", "apply", "flags", "style")


def recipe_payload(args: list[str], body: dict[str, Any], ok: bool, lane: str) -> dict[str, Any]:
    """One `decomp recipe VERB` as an event payload. Recipe's documents are flat: counts (moves, accepted, refused,
    targets), `files`, and `next`; anything absent is left out."""
    words = [a for a in args if not a.startswith("-")]
    verb, rest = words[0], words[1:]
    payload: dict[str, Any] = {
        "op": "recipe",
        "verb": verb,
        "sub": rest[0] if verb == "name" and rest else None,
        "ok": ok,
        "lane": lane,
    }
    for key in ("moves", "accepted", "refused", "targets", "rows"):
        if isinstance(body.get(key), int):
            payload[key] = body[key]
    if isinstance(body.get("files"), list):
        payload["files"] = len(body["files"])
    item = next((w for w in rest if verb in ("work", "try", "skip")), None)
    if item:
        payload["item"] = item
    if body.get("next"):
        payload["next"] = str(body["next"])[:120]
    return payload


def recipe_message(payload: dict[str, Any]) -> str:
    verb = payload["verb"] + (f" {payload['sub']}" if payload.get("sub") else "")
    facts = [f"{payload[k]} {k}" for k in ("moves", "accepted", "refused", "targets", "files") if k in payload]
    item = f" {payload['item']}" if payload.get("item") else ""
    return f"Recipe {verb}{item}: " + (", ".join(facts) if facts else "done" if payload["ok"] else "refused")


def emit_recipe(profile: Profile, args: list[str], document: dict[str, Any], lane: str) -> None:
    verb = next(a for a in args if not a.startswith("-"))
    ok = bool(document.get("ok"))
    payload = recipe_payload(args, document, ok, lane)
    kind = "LAND" if verb == "apply" and ok else "MATCH" if ok else "RUN-FAIL"
    append(
        profile,
        lane,
        "passed" if ok and verb == "apply" else "info" if ok else "failed",
        recipe_message(payload),
        kind,
        payload,
    )


def emit_default(profile: Profile, tool: str, verb: str, document: dict[str, Any], lane: str, seconds: float) -> None:
    """The event every tool call without a richer payload writes: MATCH when ok, else RUN-FAIL."""
    ok = bool(document.get("ok"))
    found = [f for f in document.get("findings") or () if isinstance(f, dict)]
    payload = {
        "op": tool,
        "verb": verb,
        "ok": ok,
        "seconds": round(seconds, 3),
        "findings": len(found),
        "lane": lane,
    }
    line = f"{tool} {verb}: " + ("ok" if ok else f"failed, {refusal(found[0])}" if found else "failed")
    append(profile, lane, "passed" if ok else "failed", line[:240], "MATCH" if ok else "RUN-FAIL", payload)


def emit_unbake(profile: Profile, verb: str, document: dict[str, Any], lane: str) -> bool:
    """The richer events of `unbake score|submit|land`; False when the call has none to write."""
    feed = events.events_path(profile)
    body = document.get("result") or {}
    if verb == "score":
        payload = attempt_payload(body, feed, lane)
        if not payload:
            return False
        append(profile, lane, "passed" if payload["exact"] else "info", attempt_message(payload), "MATCH", payload)
    elif verb == "submit":
        payloads = submit_payloads(body, lane)
        for payload in payloads:
            status = "failed" if payload["refused"] else "passed" if payload["exact"] else "info"
            append(profile, lane, status, submit_message(payload), "MATCH", payload)
        return bool(payloads)
    else:
        landed = (body.get("landed") or []) if isinstance(body, dict) else []
        payload = {"op": "inbox-land", "count": len(landed), "lane": lane}
        append(profile, lane, "passed", f"Stored {len(landed)} submitted functions in the tree", "MATCH", payload)
    return True


def emit(
    profile: Profile, tool: str, args: list[str], document: dict[str, Any], project: Path | None, seconds: float = 0.0
) -> None:
    """After every `decomp <tool>` call that returned a JSON document: one event, rich for the verbs in VERBS and
    RECIPE_VERBS, the default (op = tool, verb, ok, seconds, findings, lane) for the rest. Never raises."""
    verb = next((a for a in args if not a.startswith("-")), None)
    if not verb or not isinstance(document, dict) or type(document.get("ok")) is not bool:
        return  # a text call (--help, --version) has no JSON document
    try:
        try:
            chosen = project or tools.project_dir(profile)
        except DecompError:
            chosen = None  # taste has no project; the lane comes from where the call runs
        lane = lane_of(chosen)
        if tool == "recipe" and verb in RECIPE_VERBS:
            emit_recipe(profile, args, document, lane)
            return
        if not (tool == "unbake" and verb in VERBS and emit_unbake(profile, verb, document, lane)):
            emit_default(profile, tool, verb, document, lane, seconds)
    except (DecompError, OSError, KeyError, TypeError, ValueError) as error:
        print(f"decomp: no operation event written: {error}", file=sys.stderr)
