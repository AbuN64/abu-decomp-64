"""The evidence ladder of a decompiled function, read from the code at a commit, never self-reported.

  asm       the function is an `INCLUDE_ASM` stand-in only
  unproven  a C body inside the `NON_MATCHING` guard that `decomp behave` cannot show agrees with the original
  behaves   the guarded body agrees on k of n generated cases (`decomp behave --function SYM --candidate FILE`)
  matches   a plain body: the project's check proves its ROM bytes

`judge` compares the functions a change touches at the parent commit and at the candidate: no step down (except
dropping a body with no evidence, which loses none), a guarded body must agree on at least one case, and a guarded
body that was already measured must raise k, unless its linked bytes are identical (cleanup). The measure is injected:
`decomp land` supplies the real one (behave, cached), tests supply a table. Markers come from the seat profile.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

ASM, NM, PLAIN = "asm", "nm", "plain"
_COND = re.compile(r"^\s*#\s*(ifdef|ifndef|if|elif|else|endif)\b(.*)$")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"//[^\n]*")
_STRING = re.compile(r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'")
_KEYWORDS = frozenset({"if", "while", "for", "switch", "else", "do", "return", "sizeof"})


@dataclass(frozen=True)
class Fn:
    kind: str  # ASM, NM or PLAIN
    text: str  # whitespace-normalized, comments removed: equal text is an untouched function
    start: int = field(default=0, compare=False)  # 0-based line of the opening brace in the source
    end: int = field(default=0, compare=False)  # 0-based line of the closing brace


@dataclass(frozen=True)
class Score:
    k: int  # cases on which the candidate agrees with the original
    n: int  # cases generated
    sha: str | None  # digest of the linked bytes of the function (None when it did not build)
    note: str = ""  # why nothing compared, when k is 0


def blank(text: str) -> str:
    """Comments and string literals replaced by spaces (newlines kept), so braces in them count for nothing."""

    def keep_lines(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    for pattern in (_BLOCK_COMMENT, _LINE_COMMENT, _STRING):
        text = pattern.sub(keep_lines, text)
    return text


def parse(text: str, nm: str = "NON_MATCHING", asm: str = "INCLUDE_ASM") -> dict[str, Fn]:
    """Every function of a C source file with its rung: a body under the `nm` guard (the active branch of
    `#ifdef nm`, or the `#else` of `#ifndef nm`), a body outside it, or an `asm` stand-in naming it."""
    stripped = blank(text)
    stand_in = re.compile(rf"\b{re.escape(asm)}\s*\(\s*[^,()]+,\s*(\w+)")
    found: dict[str, Fn] = {}
    stack: list[list[bool]] = []  # per open #if: [is the guard, the guard is active on this branch]
    depth, header, body, name, guarded, opened = 0, "", "", "", False, 0
    for number, line in enumerate(stripped.splitlines()):
        cond = _COND.match(line)
        if cond:
            word, rest = cond.group(1), cond.group(2)
            has = re.search(rf"\b{re.escape(nm)}\b", rest) is not None
            if word in ("ifdef", "if"):
                stack.append([has and "!" not in rest, has and "!" not in rest])
            elif word == "ifndef":
                stack.append([has, False])
            elif word == "else" and stack:
                stack[-1][1] = stack[-1][0] and not stack[-1][1]
            elif word == "elif" and stack:
                stack[-1][1] = False
            elif word == "endif" and stack:
                stack.pop()
            if depth == 0:
                header = ""
            continue
        if depth == 0:
            for hit in stand_in.finditer(line):
                found.setdefault(hit.group(1), Fn(ASM, normalize(hit.group(0)), number, number))
        for char in line + "\n":
            if depth == 0 and char in ";}":
                header = ""
            elif depth == 0 and char == "{":
                head, header = header.strip(), ""  # a later function never inherits this header
                names = [m for m in re.findall(r"\b([A-Za-z_]\w*)\s*\(", head) if m not in _KEYWORDS]
                if head.endswith(")") and names:
                    name, guarded, body = names[0], any(flag[1] for flag in stack), head + " {"
                    depth, opened = 1, number
                else:
                    depth, name = 1, ""
                continue
            elif depth > 0:
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                if name:
                    body += char
                if depth == 0 and name:
                    found[name] = Fn(NM if guarded else PLAIN, normalize(body), opened, number)
                    name, body = "", ""
                continue
            else:
                header += char
    return found


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def level_name(fn: Fn | None, score: Score | None = None) -> str:
    if fn is None:
        return "absent"
    if fn.kind == ASM:
        return "asm"
    if fn.kind == PLAIN:
        return "matches"
    if score is None or score.k <= 0:
        return "C unproven"
    return f"behaves {score.k}/{score.n}"


Measure = Callable[[str, str], Score]  # (side "base"/"new", function) -> its score


def judge(
    base: dict[str, Fn], new: dict[str, Fn], measure: Measure
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """(refusals, checked) for the functions the change touches. A refusal names the function, both levels and why."""
    refusals: list[dict[str, str]] = []
    checked: list[dict[str, str]] = []
    for name in sorted(new):
        before, after = base.get(name), new[name]
        if before is not None and before == after:
            continue
        score_before = measure("base", name) if before and before.kind == NM and (after.kind in (NM, ASM)) else None
        score_after = measure("new", name) if after.kind == NM else None
        row = {"function": name, "base": level_name(before, score_before), "new": level_name(after, score_after)}
        why = _refusal(before, after, score_before, score_after)
        (refusals if why else checked).append({**row, **({"why": why} if why else {})})
    return refusals, checked


def _refusal(before: Fn | None, after: Fn, was: Score | None, now: Score | None) -> str:
    if after.kind == PLAIN:
        return ""  # the project's check proves its bytes
    if after.kind == ASM:
        if before is None or before.kind == ASM:
            return ""
        if before.kind == PLAIN:
            return "step down: a matching body became assembly"
        if was is not None and was.k <= 0:
            return ""  # dropping a body no evidence supported loses none
        return "step down: a body that behaved became assembly"
    assert now is not None
    if before is not None and before.kind == PLAIN:
        return "step down: a matching body became non-matching"
    if before is not None and before.kind == NM and was is not None and was.sha and was.sha == now.sha:
        return ""  # byte-identical to the parent's body: cleanup
    if now.k <= 0:
        return f"no behave evidence: 0 of {now.n} cases agree" + (f" ({now.note})" if now.note else "")
    if before is None or before.kind == ASM or was is None:
        return ""
    if now.k < was.k:
        return f"step down: behave agreement fell from {was.k}/{was.n} to {now.k}/{now.n}"
    if now.k == was.k and now.k != now.n:
        return f"agreement did not rise: still {now.k}/{now.n} and the bytes changed"
    return ""
