"""Cake Taste pane: what the recompiled original executed, one `function(params): output` row per run.

    "panes": {"ops": {"runs": "/path/runs root", "runs_shown": 20, "trace": "/path/N64R_TRACE file", "limit": 200}}
                                                  host_log comes from the tile's console

`runs` is Cake Taste's repro runs root: each of the newest runs' `events.txt` (what `taste repro probe` and `decomp
behave` executed: `case c0`, the root `call func args`, nested calls, `st`/`rd` memory traffic, `ret values`,
`end ok`) becomes ONE row, `func(a0, a1): ret v0, v1 · 64/64 ok`, from its first case; expanding it gives the input
breakdown (argument registers), the outputs, the nested calls with counts, the memory traffic and every case's
inputs and result. Nested calls never become rows. The run's `probe.json` names the function under test.
From the tile's `host_log` only faults and warnings are rows (frame counters go to the title line); the optional
`trace` file (N64R_TRACE: `vi thread function`) folds repeats into `x N`.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import kit
from .dashboard_model import Row

TAIL = 98304
RUN_TEXT = 8 << 20  # an events.txt is read whole up to this size
HOST = [
    (
        re.compile(r"TLB fault va=([0-9a-fA-F]+) thread=([0-9a-fA-F]+)"),
        lambda m: f"TLB fault va 0x{m[1].upper()} thread 0x{m[2].upper()}",
    ),
    (re.compile(r"\b(?:error|fatal|abort(?:ed)?|unimplemented)\b.*", re.I), lambda m: m[0][:100]),
]
TRACE = re.compile(r"^(\d+)\s+(\S+)\s+(\S+)")
CLASSES = [
    (re.compile(r"^_*os(Pi|EPi)|Dma"), "PI DMA"),
    (re.compile(r"^_*osVi"), "VI"),
    (re.compile(r"^_*os(Sp|Dp)|^_*(Sp|Dp)[A-Z]|Rsp|Rdp"), "RSP/RDP"),
    (re.compile(r"^_*os[A-Z]"), "OS"),
]
FALLBACK = "enter"
ARG_NAMES = ("a0", "a1", "a2", "a3")
RET_NAMES = ("v0", "v1", "f0", "f2")
CASES_SHOWN = 12
TITLE_ARGS = 2  # the title shows the first arguments; the expanded row has every register


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


def trimmed(words: list[str]) -> list[str]:
    """Trailing zero words dropped (unused argument and return registers); one word kept."""
    out = list(words)
    while len(out) > 1 and out[-1] in ("0", "00000000"):
        out.pop()
    return out


def named(words: list[str], names: tuple[str, ...], spill: str) -> str:
    labels = [names[i] if i < len(names) else f"{spill}{i - len(names)}" for i in range(len(words))]
    return "  ".join(f"{label} {word}" for label, word in zip(labels, trimmed(words), strict=False))


def parse_cases(text: str) -> list[dict[str, Any]]:
    """Each `case` of a repro events.txt: root call and args, ret words, end status, nested calls, memory traffic."""
    cases: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        word, _, rest = line.strip().partition(" ")
        if word == "case":
            current = {
                "case": rest,
                "root": None,
                "args": [],
                "ret": None,
                "end": "open",
                "calls": {},
                "st": 0,
                "rd": 0,
            }
            cases.append(current)
        elif current is None:
            continue
        elif word == "call":
            name, *args = rest.split()
            if current["root"] is None:
                current["root"], current["args"] = name, args
            else:
                current["calls"][name] = current["calls"].get(name, 0) + 1
        elif word == "ret" and current["ret"] is None:
            current["ret"] = rest.split()
        elif word == "end":
            current["end"] = rest or "?"
        elif word in ("st", "rd"):
            current[word] += 1
    return cases


def case_line(case: dict[str, Any]) -> str:
    args = ", ".join(trimmed(case["args"]))
    ret = ", ".join(trimmed(case["ret"] or [])) or "no return"
    calls = sum(case["calls"].values())
    return f"{case['case']:<4}({args}) -> {ret}  {case['end']}" + (f"  · {calls} calls" if calls else "")


def run_row(run: str, cases: list[dict[str, Any]], probe: dict[str, Any]) -> tuple[str, str, str] | None:
    """(title, details, state) of one repro run: `func(params): output · n/m ok`."""
    if not cases or not cases[0]["root"]:
        return None
    first = cases[0]
    function = str(probe.get("function") or first["root"])
    ends: dict[str, int] = {}
    for case in cases:
        ends[case["end"]] = ends.get(case["end"], 0) + 1
    ok = ends.get("ok", 0)
    same_ret = all(case["ret"] == first["ret"] for case in cases)
    args = trimmed(first["args"])
    params = ", ".join(args[:TITLE_ARGS]) + (", …" if len(args) > TITLE_ARGS else "")
    ret = f"ret {first['ret'][0]}" + ("" if same_ret else " …") if first["ret"] else "no return"
    title = f"{function}({params}): {ret} · {ok}/{len(cases)} ok"
    calls: dict[str, int] = {}
    for case in cases:
        for name, count in case["calls"].items():
            calls[name] = calls.get(name, 0) + count
    shown = [case_line(case) for case in cases[:CASES_SHOWN]]
    if len(cases) > CASES_SHOWN:
        shown.append(f"… {len(cases) - CASES_SHOWN} more cases")
    details = kit.block(
        ("function", f"{function}  (root {first['root']})" if first["root"] != function else function),
        ("input", named(first["args"], ARG_NAMES, "stack") + "  (c0)"),
        ("output", named(first["ret"] or [], RET_NAMES, "r") + ("" if same_ret else "  (c0; other cases differ)")),
        ("cases", ", ".join(f"{count} {end}" for end, count in ends.items())),
        ("calls", ", ".join(f"{name} x{count}" for name, count in sorted(calls.items(), key=lambda i: -i[1]))),
        ("memory", f"{sum(c['st'] for c in cases)} stores, {sum(c['rd'] for c in cases)} reads"),
        ("each case", "\n".join(shown)),
        ("run", f"{run}  {probe.get('wall_seconds', '?')} s" if probe else run),
    )
    state = "passed" if ok == len(cases) else "stop"
    return title, details, state


def run_epoch(name: str) -> float | None:
    try:
        return datetime.strptime(name[:15], "%Y%m%dT%H%M%S").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None


class OpsPane:
    name = "ops"
    uses_events = False

    def __init__(self, config: dict[str, Any]):
        self.trace = Path(config["trace"]) if config.get("trace") else None
        self.runs = Path(config["runs"]) if config.get("runs") else None
        self.runs_shown = int(config.get("runs_shown", 20))
        self.limit = int(config.get("limit", 200))
        self.parsed: dict[str, tuple[str, Row | None]] = {}  # run -> (the text it was parsed from, its row)

    def run_rows(self, sources: Any) -> list[Row]:
        if not self.runs:
            return []
        sources.watched.add(self.runs / ".console-watch")
        try:
            names = sorted(p.name for p in self.runs.iterdir() if p.is_dir())[-self.runs_shown :]
        except OSError:
            return []
        rows: list[Row] = []
        for name in names:
            text = sources.text(self.runs / name / "events.txt", limit=RUN_TEXT)
            cached = self.parsed.get(name)
            if cached is None or cached[0] is not text:
                found = run_row(name, parse_cases(text), sources.json(self.runs / name / "probe.json"))
                row = Row(f"ops-run-{name}", *found, when=run_epoch(name)) if found else None
                cached = self.parsed[name] = (text, row)
            if cached[1]:
                rows.append(cached[1])
        for stale in self.parsed.keys() - set(names):
            del self.parsed[stale]
        return rows

    def rows(self, sources: Any) -> list[Row]:
        host = [
            Row(f"ops-host-{key}", text, kit.block(("source", str(sources.host_path))), "stop")
            for text, key in host_rows(sources.text(sources.host_path, limit=TAIL))
        ]
        trace = []
        if self.trace:
            trace = [
                Row(f"ops-trace-{key}", text, kit.block(("source", str(self.trace))), "event")
                for text, key in trace_rows(sources.text(self.trace, limit=TAIL))
            ]
        runs = self.run_rows(sources)
        rows = kit.fold(host) + trace + kit.fold(runs)
        rows = rows[-self.limit :]
        return rows or [Row("ops-none", "no Cake Taste runs yet", str(self.runs or sources.host_path or ""), "planned")]

    def pins(self, sources: Any) -> list[str]:
        host = sources.host
        if host is None or host.frames == "-":
            return []
        return [f"vi {host.vi} · frames {host.frames} · gfx tasks {host.graphics_tasks}"]

    def facts(self, sources: Any, rows: list[Row]) -> list[str]:
        runs = sum(1 for row in rows if row.key.startswith("ops-run-"))
        return [f"{runs} run" + ("" if runs == 1 else "s"), *self.pins(sources)]
