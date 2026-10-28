"""Exact header content correspondence, independent of Git's similarity score."""

from __future__ import annotations

import hashlib
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


def _shell_change(block: str) -> tuple[str, str, str] | None:
    """A complete guarded body exchanged with an empty wrapper at an existing path.

    Only contiguous evidence from line one is usable. Both sides must include their
    closing guard and match Git's blob identities. Context ending at an endif
    alone cannot establish EOF: an unchanged tail may sit outside the hunk.
    """
    old = re.search(r"^--- a/([^\n]+)\n", block, re.M)
    new = re.search(r"^\+\+\+ b/([^\n]+)\n", block, re.M)
    if not old or not new or old[1] != new[1] or not old[1].lower().endswith(_HEADERS):
        return None
    hunks = list(_HUNK.finditer(block))
    if len(hunks) != 1 or hunks[0][1] != "1" or hunks[0][3] != "1":
        return None
    hunk = hunks[0]
    sides: dict[str, list[str]] = {"-": [], "+": []}
    previous = ""
    for line in block[hunk.end() :].splitlines(keepends=True):
        if line == "\\ No newline at end of file\n" and previous:
            for side in previous:
                sides[side][-1] = sides[side][-1].removesuffix("\n")
            previous = ""
            continue
        if not line or line[0] not in " +-":
            return None
        previous = "-+" if line[0] == " " else line[0]
        for side in previous:
            sides[side].append(line[1:])
    if len(sides["-"]) != int(hunk[2] or 1) or len(sides["+"]) != int(hunk[4] or 1):
        return None
    texts = {side: "".join(lines) for side, lines in sides.items()}
    blobs = re.search(r"^index ([0-9a-f]{7,64})\.\.([0-9a-f]{7,64})(?: |\n)", block, re.M)
    if not blobs:
        return None
    for side, expected in zip(("-", "+"), blobs.groups(), strict=True):
        data = texts[side].encode()
        blob = f"blob {len(data)}\0".encode() + data
        if not any(hashlib.new(algorithm, blob).hexdigest().startswith(expected) for algorithm in ("sha1", "sha256")):
            return None
    identities = {side: guard_content(text) for side, text in texts.items()}
    if any(identity is None for identity in identities.values()):
        return None
    empty = {
        side
        for side, text in texts.items()
        if re.fullmatch(r"\s*#\s*ifndef\s+(\w+)\s*\n\s*#\s*define\s+\1\s*\n\s*#\s*endif\s*", text)
    }
    if len(empty) != 1:
        return None
    side = "+" if "-" in empty else "-"
    return side, old[1], texts[side]


def correspondence(diff: str) -> tuple[dict[str, str], str]:
    """Unique guard-only moves and the diff without those complete file pairs.

    Exact bodies must occur once on each side, including an existing empty wrapper
    filled or emptied by the move. Copies, merges, partial hunks and changed
    declarations/types/activation retain their ordinary gate/refusal path.
    """
    blocks = _BLOCK.split(diff)
    sides: dict[str, dict[str, list[tuple[int, str]]]] = {"-": defaultdict(list), "+": defaultdict(list)}
    for index, block in enumerate(blocks):
        row = _whole(block) or _shell_change(block)
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
