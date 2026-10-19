"""Ops pane: raw operations of the recompiled original running the ROM, one terse row each.

    "panes": {"ops": {"trace": "/path/N64R_TRACE file", "runs": "/path/runs root", "limit": 200}}
                                                  host_log comes from the tile's console

Sources are the oracle host's own output: the tile's `host_log` (n64host lines: RSP graphics tasks with their display
list, VI swaps with the screen counter, TLB faults, audio driver) and, when set, the `trace` file Cake Taste writes with
N64R_TRACE (`vi thread function` per guest function entry; PI DMA, OS, VI and RSP/RDP calls are named by their
libultra function). `runs` is Cake Taste's repro runs root: the newest runs' `events.txt` (what `taste repro probe`
and `decomp behave` executed: `case c0`, `call func args`, `ret values`, `end ok`) become one row per case,
`<run function> c0: call func(args) -> ret values ok`. Only the tail of each file is read;
repeated trace entries fold into one row with a count.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .dashboard_model import Row

TAIL = 98304
HOST = [
    (
        re.compile(r"graphics tasks=(\d+) .*?dl=(0x[0-9A-Fa-f]+) bytes=(\d+)"),
        lambda m: f"RSP task #{m[1]} dl {m[2]} {m[3]} B",
    ),
    (re.compile(r"screen updates=(\d+) .*?graphics_tasks=(\d+)"), lambda m: f"VI swap #{m[1]} · {m[2]} gfx tasks"),
    (
        re.compile(r"TLB fault va=([0-9a-fA-F]+) thread=([0-9a-fA-F]+)"),
        lambda m: f"TLB fault va 0x{m[1].upper()} thread 0x{m[2].upper()}",
    ),
    (re.compile(r"audio driver=(\S+) rate=(\d+)"), lambda m: f"AI {m[1]} {m[2]} Hz"),
]
TRACE = re.compile(r"^(\d+)\s+(\S+)\s+(\S+)")
CLASSES = [
    (re.compile(r"^_*os(Pi|EPi)|Dma"), "PI DMA"),
    (re.compile(r"^_*osVi"), "VI"),
    (re.compile(r"^_*os(Sp|Dp)|^_*(Sp|Dp)[A-Z]|Rsp|Rdp"), "RSP/RDP"),
    (re.compile(r"^_*os[A-Z]"), "OS"),
]
FALLBACK = "enter"


def classify(name: str) -> str:
    return next((label for pattern, label in CLASSES if pattern.search(name)), FALLBACK)


def trace_rows(text: str) -> list[tuple[str, str]]:
    """(row text, key) for the trace lines, consecutive repeats of one function folded into `x N`."""
    rows: list[list[Any]] = []
    for line in text.splitlines()[1:]:  # the first line of a tail is usually cut
        found = TRACE.match(line)
        if not found:
            continue
        vi, _, name = found.groups()
        if rows and rows[-1][0] == name:
            rows[-1][2] += 1
            continue
        rows.append([name, vi, 1, len(rows)])
    return [(f"{classify(n)} {n}{f' x{c}' if c > 1 else ''} · vi {vi}", f"{n}-{vi}-{i}") for n, vi, c, i in rows]


def host_rows(text: str) -> list[tuple[str, str]]:
    rows = []
    for line in text.splitlines()[1:]:
        for pattern, render in HOST:
            if found := pattern.search(line):
                rows.append((render(found), hashlib.sha256(line.encode()).hexdigest()[:16]))
                break
    return rows


def case_rows(text: str, label: str) -> list[tuple[str, str]]:
    """One row per `case` block of a repro run's events.txt."""
    rows: list[tuple[str, str]] = []
    current: dict[str, Any] = {}

    def close() -> None:
        if current:
            call = current.get("call", "no call")
            result = f" -> ret {current['ret']}" if current.get("ret") else ""
            rows.append(
                (
                    f"{label} {current['case']}: {call}{result} {current.get('end', 'open')}",
                    f"{label}-{current['case']}",
                )
            )

    for line in text.splitlines():
        word, _, rest = line.strip().partition(" ")
        if word == "case":
            close()
            current.clear()
            current["case"] = rest
        elif word == "call" and current:
            name, _, args = rest.partition(" ")
            current["call"] = f"call {name}({args.strip().replace(' ', ', ')})"
            current["function"] = name
        elif word in ("ret", "end") and current:
            current[word] = rest
    close()
    return rows


class OpsPane:
    name = "ops"
    uses_events = False

    def __init__(self, config: dict[str, Any]):
        self.trace = Path(config["trace"]) if config.get("trace") else None
        self.runs = Path(config["runs"]) if config.get("runs") else None
        self.runs_shown = int(config.get("runs_shown", 3))
        self.limit = int(config.get("limit", 200))

    def run_rows(self, sources: Any) -> list[tuple[str, str]]:
        if not self.runs:
            return []
        sources.watched.add(self.runs / ".console-watch")
        try:
            names = sorted(p.name for p in self.runs.iterdir() if p.is_dir())[-self.runs_shown :]
        except OSError:
            return []
        rows: list[tuple[str, str]] = []
        for name in names:
            text = sources.text(self.runs / name / "events.txt")
            rows += case_rows(text, name[9:15] if len(name) > 15 else name)
        return rows

    def rows(self, sources: Any) -> list[Row]:
        found = [(text, key, "host") for text, key in host_rows(sources.text(sources.host_path, limit=TAIL))]
        if self.trace:
            found += [(text, key, "trace") for text, key in trace_rows(sources.text(self.trace, limit=TAIL))]
        found += [(text, key, "run") for text, key in self.run_rows(sources)]
        rows = [Row(f"ops-{source}-{key}", text, "", "event") for text, key, source in found[-self.limit :]]
        return rows or [Row("ops-none", "no oracle operations yet", str(sources.host_path or ""), "planned")]

    def pins(self, sources: Any) -> list[str]:
        host = sources.host
        if host is None or host.frames == "-":
            return []
        return [f"vi {host.vi} · frames {host.frames} · gfx tasks {host.graphics_tasks}"]
