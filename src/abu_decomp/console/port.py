"""Port view: the boot step and the stop on the title line, and the game's own C stack as the rows.

Read from the supervised run (the tile's `supervisor_status` JSON): its `log` (the boot harness prints
`boot-step: N/M func`, `boot-step: census N/M hit, ... last F`, `boot-missing: F`, `debug-reason: F`) and its stack
artifacts beside `stack` (`*-threads.txt`: every thread's backtrace, else `*-stack-full.txt`, else the stack file).

Frames of the harness (the debugger's stop function, `main`, libc and thread plumbing) are not the game: rows show
only the frames left after the `stack_hide` prefixes (tile `sources.stack_hide`, default HARNESS). When no game
frame is in the stack (a stop raised outside a game thread), the rows are the boot trail instead: the function the
boot stopped at and the last game function it reached, said as such, never dressed up as a stack.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import kit
from .dashboard_model import Row, event_epoch

HARNESS = (
    "n64c_",
    "main",
    "__",
    "_start",
    "start_thread",
    "clone",
    "thread_main",
    "pthread_",
    "futex",
    "syscall",
    "sem_",
    "nanosleep",
    "clock_nanosleep",
    "poll",
    "epoll",
    "SDL_",
    "??",
    "std::",
    "BS::",
    "thread_main",
    "wake",
)
STEP = re.compile(r"boot-step: (\d+)/(\d+) (\S+)")
CENSUS = re.compile(r"boot-step: census (\d+)/(\d+) hit.*?last (\S+?)(?: \(order (\d+)\))?(?:,|$)")
MISSING = re.compile(r"boot-missing: (\S+)")
REASON = re.compile(r"debug-reason: (.+)")
THREAD = re.compile(r"^Thread (\d+) \((.*)\):?\s*$")
FRAME = re.compile(r"^#(\d+)\s+(?:0x[0-9a-fA-F]+ in )?(\S+?)\s*(\(.*?\))?(?: (?:at|from) (\S+))?\s*$")


def boot_facts(log: str) -> dict[str, Any]:
    """The newest step count, the last reached function and the stop, from the harness lines of a run log."""
    facts: dict[str, Any] = {}
    for line in log.splitlines():
        if found := CENSUS.search(line):
            facts.update(hit=int(found[1]), total=int(found[2]), last=found[3], order=found[4])
        elif found := STEP.search(line):
            facts.update(step=int(found[1]), total=int(found[2]), last=found[3])
        elif found := MISSING.search(line):
            facts["missing"] = found[1]
        elif found := REASON.search(line):
            facts["reason"] = found[1].strip()
    return facts


def frames(text: str) -> list[dict[str, str]]:
    """Every frame of a gdb backtrace (one thread or `thread apply all`), with its thread label."""
    out, thread = [], ""
    for line in text.splitlines():
        line = line.strip()
        if found := THREAD.match(line):
            name = re.search(r'"([^"]+)"', found[2])
            thread = f"thread {found[1]}" + (f" {name[1]}" if name else "")
            continue
        if found := FRAME.match(line):
            out.append(
                {"index": found[1], "function": found[2], "where": found[4] or "", "line": line, "thread": thread}
            )
    return out


def game_frames(stack: list[dict[str, str]], hide: tuple[str, ...]) -> list[dict[str, str]]:
    return [frame for frame in stack if not frame["function"].startswith(hide)]


def threads(stack: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for frame in stack:
        grouped.setdefault(frame["thread"], []).append(frame)
    return grouped


def stopped_thread(stack: list[dict[str, str]], stop: str, hide: tuple[str, ...]):
    """(the stopped thread's game frames, the other threads that run game code).

    The stopped thread is the one whose stack holds the stop symbol, else the one with the most game frames. Its
    rows run from the stop symbol (or its first game frame) down to its last game frame: harness frames above the
    stop (the halt that parks the thread) and below the thread entry are not the game.
    """
    grouped = {name: frames for name, frames in threads(stack).items() if game_frames(frames, hide)}
    if not grouped:
        return [], {}
    chosen = next(
        (name for name, frames in grouped.items() if stop and any(f["function"] == stop for f in frames)), None
    )
    chosen = chosen or max(grouped, key=lambda name: len(game_frames(grouped[name], hide)))
    frames_ = grouped.pop(chosen)
    shown = [i for i, f in enumerate(frames_) if not f["function"].startswith(hide)]
    start = next((i for i, f in enumerate(frames_) if stop and f["function"] == stop), shown[0])
    return frames_[start : shown[-1] + 1], grouped


def stack_text(sources: Any, stack_path: str | None) -> str:
    """The richest stack artifact of the run: all threads, else the full stack, else the visible one."""
    if not stack_path:
        return ""
    path = Path(stack_path)
    stem = path.name.removesuffix("-stack.txt")
    for candidate in (path.with_name(stem + "-threads.txt"), path.with_name(stem + "-stack-full.txt"), path):
        text = sources.text(candidate)
        if text.strip():
            return text
    return ""


def port_view(sources: Any, runtime: dict[str, Any], log: str, hide: tuple[str, ...] = HARNESS):
    """(facts, pins, rows) of the supervised port run."""
    facts = boot_facts(log)
    stopped = facts.get("missing") or facts.get("reason") or runtime.get("failure_reason") or ""
    total = facts.get("total")
    reached = facts.get("hit", facts.get("step"))
    step = f"boot {reached}/{total}" if total else f"boot {runtime.get('phase', 'not started')}"
    why = " not linked" if facts.get("missing") else ""
    stop = f"stop {stopped}{why}" if stopped else "running" if runtime.get("phase") == "running" else ""
    when = event_epoch({"time": runtime.get("updated_at")})
    rows = []
    stack, others = stopped_thread(frames(stack_text(sources, runtime.get("stack"))), stopped, hide)
    for frame in stack:
        title = kit.cells(f"#{frame['index']}", frame["function"], frame["where"])
        details = kit.block(("frame", frame["line"]), ("thread", frame["thread"]), ("stack", runtime.get("stack")))
        rows.append(Row(f"frame-{frame['index']}", title, details, "stop" if not rows else "event", when=None))
    if rows and others:
        waits = []
        for name, frames_ in others.items():
            game = [f["function"] for f in game_frames(frames_, hide)][:3]
            waits.append(f"{name}: " + " ← ".join(game))
        rows.append(Row("frame-others", f"{len(others)} other game threads", "\n".join(waits), "raw"))
    if not rows and stopped:
        rows.append(
            Row(
                "trail-stop",
                kit.cells(stopped, "stopped here" + (" (not linked)" if why else "")),
                kit.block(
                    ("reason", facts.get("reason")), ("log", runtime.get("log")), ("stack", runtime.get("stack"))
                ),
                "stop",
                when=when,
            )
        )
        if facts.get("last") and facts["last"] != stopped:
            order = f" (order {facts['order']})" if facts.get("order") else ""
            rows.append(
                Row(
                    "trail-last",
                    kit.cells(facts["last"], f"last game function reached{order}"),
                    kit.block(("reached", f"{reached}/{total} boot functions"), ("log", runtime.get("log"))),
                    "event",
                )
            )
    return [step, stop], [step, stop] if stop else [step], rows
