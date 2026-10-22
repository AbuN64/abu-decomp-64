"""A behave difference classified by the tool, never by the reader: `real` or `benign`, from facts the run has.

Benign is only a reorder or a count change of calls whose callees wrote no shared memory in any case (the original's
trace shows no store after a call to them before the next call, outside the stack frame and the candidate's own
image). A callee that was cut off (a stub), never traced, or wrote anything is real. Every other difference kind is
real, and so is anything unknown. The result carries the first differing case with both sides, the candidate source
line when it is known, and one `next` command.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from abu_decomp.behave import compare

CALLS = ("call", "stub")
KINDS = {
    "call": "call order",
    "call-args": "call arguments",
    "store": "store",
    "return": "return value",
    "status": "crash",
    "clobber": "callee-saved register",
}


def _calls(case: dict[str, Any], opaque: frozenset[str]) -> list[dict[str, Any]]:
    return compare._calls(case, opaque)


def writes(cases: list[dict[str, Any]], name: str, stack: tuple[int, int], own: tuple[int, int] | None) -> str | None:
    """None when callee `name` is traced and pure, else why it is not (a store address, 'stub', 'never traced')."""
    seen = False
    for case in cases:
        events = case["events"]
        for index, event in enumerate(events):
            if event["k"] not in CALLS or compare._name(event["name"]) != name or index == 0:
                continue
            if event["k"] == "stub":
                return "it is cut off at the boundary (a stub), its effects are unknown"
            seen = True
            for after in events[index + 1 :]:
                if after["k"] in CALLS:
                    break
                address = after.get("addr", 0)
                if (
                    after["k"] == "st"
                    and not stack[0] <= address < stack[1]
                    and not (own and own[0] <= address < own[1])
                ):
                    return f"it writes 0x{address:08X} in the original trace"
    return None if seen else "it never ran in the traced cases"


def source_line(candidate: Path | None, function: str, callee: str) -> tuple[int, str] | None:
    """(line, text) of the first call to `callee` in the body of `function` in the candidate file, when found."""
    if candidate is None or not candidate.is_file():
        return None
    lines = candidate.read_text(errors="replace").splitlines()
    start = next(
        (
            i
            for i, text in enumerate(lines)
            if re.search(rf"\b{re.escape(function)}\s*\(", text) and "{" in "".join(lines[i : i + 3])
        ),
        None,
    )
    if start is None:
        return None
    for i in range(start, len(lines)):
        if re.search(rf"\b{re.escape(callee)}\s*\(", lines[i]):
            return i + 1, lines[i].strip()
    return None


def classify(
    summary: dict[str, Any],
    originals: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    stack: tuple[int, int],
    own: tuple[int, int] | None,
    opaque: frozenset[str],
    function: str,
    candidate: Path | None,
    profile_file: Path,
) -> dict[str, Any] | None:
    first = summary.get("first_difference")
    if not first:
        return None
    kind = first.get("kind", "unknown")
    case = next((i for i, c in enumerate(originals) if c["name"] == first["case"]), None)
    out: dict[str, Any] = {
        "kind": KINDS.get(kind, kind),
        "case": first["case"],
        "inputs": first.get("inputs"),
        "original": first.get("expected"),
        "candidate": first.get("got"),
        "real": True,
    }
    why = f"a {out['kind']} difference changes what the function does"
    if kind == "call" and case is not None:
        a, b = _calls(originals[case], opaque), _calls(candidates[case], opaque)
        names_a, names_b = [compare._name(e["name"]) for e in a], [compare._name(e["name"]) for e in b]
        index = first["index"] - 1
        out["call_index"] = first["index"]
        out["kind"] = "call order" if sorted(names_a) == sorted(names_b) else "call count"
        involved = (
            set(names_a[index:]) ^ set(names_b[index:])
            if out["kind"] == "call count"
            else set(names_a[index : index + 1] + names_b[index : index + 1])
        )
        if out["kind"] == "call order" and first["got"] in names_a[index:]:
            involved |= set(names_a[index : names_a.index(first["got"], index) + 1])
        involved.discard("return")
        reasons = {name: writes(originals, name, stack, own) for name in involved}
        dirty = {n: r for n, r in reasons.items() if r}
        if involved and not dirty:
            out["real"] = False
            why = (
                f"{', '.join(sorted(involved))} wrote no shared memory in the traced case: "
                "only the order of side-effect-free calls differs"
            )
        else:
            name, reason = next(iter(sorted(dirty.items())), (first.get("expected"), "unknown"))
            why = f"{name}: {reason}; the {out['kind']} changes shared state, so the difference is real"
        found = source_line(candidate, function, str(first.get("got")))
        if found:
            out["candidate_line"] = {"file": str(candidate), "line": found[0], "text": found[1]}
        fix = (
            f"call {first['expected']} where the original does (call {first['index']}), before {first['got']}"
            if out["kind"] == "call order"
            else f"make the same calls as the original: it has {first['got']}, the original {first['expected']}"
        )
    elif kind == "store":
        fix = f"make the candidate leave 0x{first['address']:08X} as the original does"
    elif kind == "return":
        fix = "make the candidate return what the original returns"
    else:
        fix = "make the candidate behave as the original in this case"
    out["why"] = why
    where = f" at {out['candidate_line']['file']}:{out['candidate_line']['line']}" if "candidate_line" in out else ""
    out["fix"] = f"{fix}{where}"
    out["next"] = f"decomp --profile {profile_file} behave --function {function}" + (
        f" --candidate {candidate}" if candidate else ""
    )
    out["rule"] = "a real difference is not a tool artifact; do not dismiss it, fix the candidate and rerun `next`"
    return out
