"""Names a candidate spells in another version's vocabulary, found in this version's own symbol table.
Source written against one version's names (`D_801470C0_de`) refers to a symbol the version being tested names
otherwise (`D_8014D340_us_rev1`). The real build learns that from the original code: the same function's assembly
in two versions uses the two names at the same operand, as unbake's cross-version inference reads it. The
candidate link reads the same evidence, so a name no table of the version has resolves to the address of the
version's own name for the same thing.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection, Mapping
from typing import Any

_OPERAND = re.compile(r"%(?:hi|lo|gp_rel)\((\w+)\)|\bjal\s+(\w+)|\.word\s+(\w+)")


def operands(asm: str) -> list[str]:
    """The symbol operands of an assembly listing, in order: %hi/%lo/%gp_rel relocations, jal targets, .word values."""
    return [next(part for part in match if part) for match in _OPERAND.findall(asm)]


def counterparts(
    mine: str, others: Mapping[str, str], wanted: Collection[str], symbols: Mapping[str, list[Any]]
) -> dict[str, int]:
    """wanted name -> address in this version. `mine` is the function's assembly in the tested version, `others`
    its assembly in the versions whose names the candidate uses. A name counts when every version that spells it
    pairs it with one and the same name here, and that name has an address in `symbols`."""
    wanted = set(wanted)
    here = operands(mine)
    votes: dict[str, Counter[str]] = {}
    for text in others.values():
        there = operands(text)
        if len(there) != len(here):
            continue
        for theirs, ours in zip(there, here, strict=True):
            if theirs != ours and theirs in wanted:
                votes.setdefault(theirs, Counter())[ours] += 1
    found: dict[str, int] = {}
    for name, counter in votes.items():
        if len(counter) != 1:
            continue
        row = symbols.get(next(iter(counter)))
        if row and row[0] is not None:
            found[name] = row[0]
    return found
