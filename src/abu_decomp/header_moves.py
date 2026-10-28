"""Exact deleted/added header correspondence, independent of Git's similarity score."""

from __future__ import annotations

import re
from collections import defaultdict

from unbake.api import guard_content

_BLOCK = re.compile(r"(?=^diff --git )", re.M)
_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n")
_HEADERS = (".h", ".hh", ".hpp", ".hxx")


def _whole(block: str) -> tuple[str, str, str] | None:
    """Only a complete, single-hunk added/deleted header; partial or malformed input refuses."""
    old = re.search(r"^--- (?:a/)?([^\n]+)\n", block, re.M)
    new = re.search(r"^\+\+\+ (?:b/)?([^\n]+)\n", block, re.M)
    if not old or not new or (old[1] == "/dev/null") == (new[1] == "/dev/null"):
        return None
    added = old[1] == "/dev/null"
    path = new[1] if added else old[1]
    if not path.lower().endswith(_HEADERS):
        return None
    hunks = list(_HUNK.finditer(block))
    if len(hunks) != 1:
        return None
    hunk = hunks[0]
    start, count, other_start, other_count = (3, 4, 1, 2) if added else (1, 2, 3, 4)
    if hunk[start] != "1" or hunk[other_start] != "0" or hunk[other_count] != "0":
        return None
    prefix = "+" if added else "-"
    lines = block[hunk.end() :].splitlines(keepends=True)
    text: list[str] = []
    for index, line in enumerate(lines):
        if line == "\\ No newline at end of file\n" and text and index == len(lines) - 1:
            text[-1] = text[-1].removesuffix("\n")
        elif line.startswith(prefix):
            text.append(line[1:])
        else:
            return None
    if len(text) != int(hunk[count] or 1):
        return None
    return prefix, path, "".join(text)


def correspondence(diff: str) -> tuple[dict[str, str], str]:
    """Unique guard-only moves and the diff without those complete file pairs.

    Exact bodies must occur once on each side. Copies, merges, partial hunks and
    declaration/type/activation changes retain their ordinary gate/refusal path.
    """
    blocks = _BLOCK.split(diff)
    sides: dict[str, dict[str, list[tuple[int, str]]]] = {"-": defaultdict(list), "+": defaultdict(list)}
    for index, block in enumerate(blocks):
        row = _whole(block)
        if row and (identity := guard_content(row[2])) is not None:
            sides[row[0]][identity].append((index, row[1]))
    pairs: dict[str, str] = {}
    matched: set[int] = set()
    for identity in sides["-"].keys() & sides["+"].keys():
        before, after = sides["-"][identity], sides["+"][identity]
        if len(before) == len(after) == 1 and before[0][1] != after[0][1]:
            pairs[before[0][1]] = after[0][1]
            matched.update((before[0][0], after[0][0]))
    return dict(sorted(pairs.items())), "".join(block for index, block in enumerate(blocks) if index not in matched)
