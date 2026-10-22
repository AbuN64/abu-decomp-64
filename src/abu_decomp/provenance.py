"""Two checks on the function bodies a change adds or changes, both read from the code, never self-reported.

provenance  a body must equal (normalised text) one that `recipe try` recorded: the tries stream of the project's
            Recipe state holds each candidate file it measured, so a hand-written body has no record.
names       a body may not add machine-generated leftovers: the rules (name, regex, hint) come from the seat profile
            and run on the body's code lines (comments and strings blanked); a leftover the parent body already had
            does not block (no step down, as the evidence ladder).

A body counts as changed unless the parent held the same braces text (a declaration or name edit outside it); both
plain bodies (the project's check proves their bytes) need no try but still face the names ratchet.
Both checks only refuse; the evidence gate runs them.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from abu_decomp import evidence
from abu_decomp.evidence import ASM, PLAIN, Fn


def inner(fn: Fn) -> str:
    """The braces text of a function: what the header declares never makes a body new."""
    return fn.text[fn.text.find("{") :] if "{" in fn.text else fn.text


def edited(before: Fn | None, after: Fn) -> bool:
    """Whether a function body was added or its braces text edited (a header edit alone is not)."""
    if after.kind == ASM or before == after:
        return False
    return before is None or before.kind == ASM or inner(before) != inner(after)


def changed(before: Fn | None, after: Fn) -> bool:
    """Whether the body also needs a try: both plain bodies are proven by the project's check, so they need none."""
    return edited(before, after) and not (before is not None and before.kind == PLAIN and after.kind == PLAIN)


def recorded(paths: list[Path]) -> dict[str, dict[str, Any]]:
    """Body text -> the try that produced it (item, session, time), over every candidate a try compiled and scored
    (a failed try proved nothing)."""
    found: dict[str, dict[str, Any]] = {}
    for path in paths:
        if not path.is_file():
            continue
        for raw in path.read_text(errors="replace").splitlines():
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(row, dict) or not row.get("text") or row.get("result") == "failed":
                continue
            who = {k: row.get(k) for k in ("item", "session", "time")}
            for fn in evidence.parse(str(row["text"])).values():
                if fn.kind != ASM:
                    found.setdefault(inner(fn), who)
    return found


def unproven(base: dict[str, Fn], new: dict[str, Fn], tries: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "function": name,
            "why": f"{name} body was not produced/proven by recipe try (no recorded candidate has this body)",
            "next": f"recipe try {name}",
        }
        for name, fn in sorted(new.items())
        if changed(base.get(name), fn) and inner(fn) not in tries
    ]


def leftovers(fn: Fn, text: str, rules: list[dict[str, str]]) -> list[tuple[str, int, str]]:
    """(rule, 1-based line, matched text) over the code lines of the function in its source `text`."""
    lines = evidence.blank(text).splitlines()[fn.start : fn.end + 1]
    return [
        (rule["id"], fn.start + i + 1, hit.group(0))
        for i, line in enumerate(lines)
        for rule in rules
        for hit in re.finditer(rule["pattern"], line)
    ]


def leftover_names(
    base: dict[str, Fn], new: dict[str, Fn], old_text: str, new_text: str, rules: list[dict[str, str]]
) -> list[dict[str, str]]:
    """One refusal per function whose changed body holds more of a leftover than the parent body held."""
    hint = {rule["id"]: rule.get("hint", "") for rule in rules}
    out = []
    for name, fn in sorted(new.items()):
        if not rules or not edited(base.get(name), fn):
            continue
        before = base.get(name)
        had = (
            Counter((r, m) for r, _, m in leftovers(before, old_text, rules))
            if before and before.kind != ASM
            else Counter()
        )
        added = []
        for rule, line, match in leftovers(fn, new_text, rules):
            if had[(rule, match)] > 0:
                had[(rule, match)] -= 1
            else:
                added.append(f"line {line} {rule} `{match}`")
        if added:
            rules_hit = sorted({a.split()[2] for a in added})
            hints = "; ".join(f"{r}: {hint[r]}" for r in rules_hit if hint.get(r))
            out.append(
                {
                    "function": name,
                    "why": f"{name} adds machine-generated leftovers: "
                    + ", ".join(added[:12])
                    + (f"; {hints}" if hints else ""),
                    "next": f"recipe name {name}",
                }
            )
    return out
