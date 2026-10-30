"""Reviewed state differences in the port facts directory's lockstep-ignore.tsv."""

from __future__ import annotations

import re
from pathlib import Path


def read(path: Path) -> tuple[dict[str, str], list[tuple[int, int, str]]]:
    """Read exact symbols or half-open guest address ranges, each with a measured cause."""
    symbols: dict[str, str] = {}
    ranges: list[tuple[int, int, str]] = []
    if not path.exists():
        return symbols, ranges
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, separator, cause = line.partition("\t")
        cause = cause.strip()
        error = f"{path}:{number}: invalid reviewed difference (expected symbol or range, TAB, measured cause)"
        if not separator or not cause:
            raise ValueError(error)
        if re.fullmatch(r"0x[0-9a-fA-F]+-0x[0-9a-fA-F]+", name):
            start, end = (int(value, 16) for value in name.split("-"))
            if not 0 <= start < end <= 0x100000000 or any(start < hi and lo < end for lo, hi, _ in ranges):
                raise ValueError(error)
            ranges.append((start, end, cause))
        elif re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name) and name not in symbols:
            symbols[name] = cause
        else:
            raise ValueError(error)
    return symbols, ranges


def cause(ledger: tuple[dict[str, str], list[tuple[int, int, str]]], name: str, address: int, size: int) -> str | None:
    """A range reviews a symbol only when it contains its entire compared extent."""
    symbols, ranges = ledger
    return symbols.get(name) or next((why for lo, hi, why in ranges if lo <= address and address + size <= hi), None)
