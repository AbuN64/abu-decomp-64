"""The one row framework every console pane uses: row label, title line, value cells, folding and detail blocks.

Every pane builds `Row`s and hands them over; this module decides how they read, so the port, Cake Taste and decomp
consoles share one format:

    title line   TITLE · fact · fact · 12s            (the age of the newest row, live)
    row          21:42:50 +3s ✓ name [1.9 KB] op  value x4  tag
    expanded     label      value                      (aligned `label value` lines, values only)

`+3s` is the time since the previous row. A row that folds N identical results shows `xN` and keeps the newest
time; its detail lists when each one happened. Rows without a time (stack frames) leave the time columns blank.
Nothing here knows a lane, a project or a path.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import replace

from rich.markup import escape

from .dashboard_model import Row, ago

MARKERS = {"passed": "✓", "cracked": "★", "stop": "!", "planned": "○", "raw": "≡"}
SEPARATOR = " · "
CLOCK_WIDTH = 8  # HH:MM:SS
DELTA_WIDTH = 4  # +59s, +12m, +3h
LABEL_WIDTH = 11  # detail labels, the same column as human.lines


def clock(epoch: float | None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(epoch)) if epoch else ""


def delta(seconds: float | None) -> str:
    """The short time since the previous row: +0s, +12s, +4m, +2h, +3d."""
    return "" if seconds is None else "+" + ago(seconds)


def size(byte_count: int | None) -> str:
    """`[812 B]` below 1 KB, else `[1.9 KB]`; empty when the size is unknown."""
    if not byte_count:
        return ""
    return f"[{byte_count} B]" if byte_count < 1024 else f"[{byte_count / 1024:.1f} KB]"


def cells(*parts: str | None) -> str:
    """One row title from its cells, empty cells dropped: `name [1.9 KB] op  value`."""
    head = " ".join(part for part in parts[:-1] if part)
    tail = parts[-1] if parts else ""
    return f"{head}  {tail}" if head and tail else head or tail or ""


def heading(title: str, facts: Iterable[str]) -> str:
    return SEPARATOR.join([title, *(fact for fact in facts if fact)])


def block(*pairs: tuple[str, object]) -> str:
    """The expanded detail: aligned `label value` lines, empty values left out."""
    shown = [(label, str(value)) for label, value in pairs if value not in (None, "", [], {})]
    width = max([LABEL_WIDTH - 2, *(len(label) for label, _ in shown)]) + 2  # blocks of one row line up
    lines = []
    for label, value in shown:
        first, *rest = value.splitlines() or [""]
        lines.append(f"{label:<{width}}{first}")
        lines.extend(" " * width + line for line in rest)
    return "\n".join(lines)


def label(row: Row, previous: float | None) -> str:
    """The collapsed row as Collapsible markup: dim clock and delta, marker, title, fold count, dim tag."""
    stamp = clock(row.when)
    since = delta(row.when - previous) if row.when and previous else ""
    marker = MARKERS.get(row.state, "•")
    count = f" x{row.count}" if row.count > 1 else ""
    tag = f"  [dim]{escape(row.tag)}[/dim]" if row.tag else ""
    return f"[dim]{stamp:>{CLOCK_WIDTH}} {since:>{DELTA_WIDTH}}[/dim] " + escape(f"{marker} {row.title}{count}") + tag


def previous_times(rows: list[Row]) -> list[float | None]:
    """For each row, the time of the nearest earlier row that has one (what its delta counts from)."""
    out, last = [], None
    for row in rows:
        out.append(last)
        if row.when:
            last = row.when
    return out


def newest(rows: Iterable[Row]) -> float | None:
    return max((row.when for row in rows if row.when), default=None)


def fold(rows: list[Row], signature: Callable[[Row], object] | None = None) -> list[Row]:
    """Identical results become one row: the newest keeps its place and gains `count` and the list of times.

    `signature` says what makes two rows the same (default: state and title). Tags of the folded rows are merged.
    """
    sign = signature or (lambda row: (row.state, row.title))
    groups: dict[object, list[Row]] = {}
    for row in rows:
        groups.setdefault(sign(row), []).append(row)
    out = []
    for row in rows:
        group = groups[sign(row)]
        if row is not group[-1]:
            continue
        if len(group) == 1:
            out.append(row)
            continue
        tags = list(dict.fromkeys(tag for item in group for tag in item.tag.split(", ") if tag))
        times = [clock(item.when) for item in group if item.when]
        seen = block(
            ("seen", f"{len(group)} times"), ("first", times[0] if times else ""), ("last", times[-1] if times else "")
        )
        details = "\n".join(part for part in (row.details, seen) if part)
        out.append(replace(row, count=sum(item.count for item in group), tag=", ".join(tags[:3]), details=details))
    return out
