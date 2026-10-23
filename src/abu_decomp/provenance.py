"""The names check on the function bodies a change adds or changes, read from the code, never self-reported.

names       a body may not add machine-generated leftovers: the rules (name, regex, hint) come from the seat profile
            and run on the body's code lines (comments and strings blanked); a leftover the parent body already had
            does not block (no step down, as the evidence ladder).

A body counts as changed unless the parent held the same braces text (a declaration or name edit outside it).
The check only refuses; the evidence gate runs it.
"""

from __future__ import annotations

import re
from collections import Counter

from abu_decomp import evidence
from abu_decomp.evidence import ASM, Fn


def inner(fn: Fn) -> str:
    """The braces text of a function: what the header declares never makes a body new."""
    return fn.text[fn.text.find("{") :] if "{" in fn.text else fn.text


_FIXED = frozenset(
    re.findall(
        r"\w+",
        "auto break case char const continue default do double else enum extern float for goto if int long register "
        "return short signed sizeof static struct switch typedef union unsigned void volatile while "
        "s8 u8 s16 u16 s32 u32 s64 u64 f32 f64 NULL TRUE FALSE",
    )
)


_TYPE_WORDS = frozenset(
    [
        "s8",
        "u8",
        "s16",
        "u16",
        "s32",
        "u32",
        "s64",
        "u64",
        "f32",
        "f64",
        "int",
        "char",
        "short",
        "long",
        "float",
        "double",
        "unsigned",
        "signed",
        "void",
        "const",
    ]
)


def edited(before: Fn | None, after: Fn) -> bool:
    """Whether a function body was added or its braces text edited (a header edit alone is not)."""
    if after.kind == ASM or before == after:
        return False
    return before is None or before.kind == ASM or inner(before) != inner(after)


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
