"""Video overlay text: one terse line on a video tile, in the console rows' value style.

    "tiles": {"roles": {"port-video": {"overlay_app_id": "game", "hud": {"console": "port-console"}}}}

A role with a `hud` object runs `decomp console hud ROLE` in its tile terminal. The role's own window (the video)
floats over the tile but leaves the tile's bottom `hud.height` pixels uncovered (stream.layout), so the line printed
on the terminal's last row sits at the video's bottom edge; with no video window it shows on the black tile. A
covering window could not be raised without taking the keyboard focus, hence the uncovered strip.

The line is the newest scored function as `func: 87.3% match`, bold green `func: CRACKED!` at 100%, then the
pinned status lines of the console tile named by `hud.console` (a port's boot status) when there is one. Sources
come from the profile like any console tile; the line prints again whenever a source changes.
"""

from __future__ import annotations

import os
import select
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any

from abu_decomp import DecompError
from abu_decomp.stream.inotify import CLOSE_WRITE, CREATE, MODIFY, MOVED_TO, drain, watch

from .dashboard_sources import Sources
from .feed import events_path
from .human import payload_of, sentence
from .tiles import tile_sources

BOLD_GREEN, RESET = "\033[1;32m", "\033[0m"
SEPARATOR = " \u00b7 "
REFRESH_SECONDS = 2.0


def hud_spec(profile: Any, role: str) -> dict[str, Any]:
    roles = profile.section("tiles")["roles"]
    if role not in roles or "hud" not in roles[role]:
        raise DecompError("console.hud", f"tile {role} has no hud object")
    return roles[role]["hud"]


def hud_sources(profile: Any, spec: dict[str, Any]) -> Sources:
    if spec.get("console"):
        arguments = tile_sources(profile, spec["console"])
        return Sources(
            arguments["path"], title=arguments["title"], config=arguments["sources"], host_log=arguments["host_log"]
        )
    return Sources(events_path(profile), title="hud")


def newest_attempt(sources: Sources) -> dict[str, Any] | None:
    attempts = [
        event
        for records in sources.records.values()
        for event in records
        if payload_of(event).get("op") == "attempt" and event.get("status") != "started"
    ]
    return max(attempts, key=lambda event: str(event.get("time", "")), default=None)


def hud_line(sources: Sources, pinned: list[str]) -> str:
    """The strip's text with ANSI styling; CRACKED! is bold green."""
    parts = []
    event = newest_attempt(sources)
    if event:
        text = sentence(event)
        parts.append(text.replace("CRACKED!", f"{BOLD_GREEN}CRACKED!{RESET}") if "CRACKED!" in text else text)
    parts.extend(pinned[:2])
    return SEPARATOR.join(parts)


def visible(text: str) -> str:
    """The text cut to the terminal width, never in the middle of an escape sequence."""
    width = max(10, shutil.get_terminal_size((80, 1)).columns - 1)
    out, count, index = [], 0, 0
    while index < len(text) and count < width:
        if text[index] == "\033":
            end = text.index("m", index) + 1
            out.append(text[index:end])
            index = end
            continue
        out.append(text[index])
        count += 1
        index += 1
    return "".join(out) + RESET


def render(sources: Sources) -> str:
    model = sources.read()
    return visible(hud_line(sources, model.pinned))


def run_hud(profile: Any, role: str, out: Any = None, iterations: int | None = None) -> dict[str, Any]:
    spec = hud_spec(profile, role)
    sources, out = hud_sources(profile, spec), out or sys.stdout
    previous, done = None, 0
    watches: dict[Path, int] = {}
    wake_read, wake_write = os.pipe()
    os.set_blocking(wake_read, False)
    os.set_blocking(wake_write, False)
    # a resized tile prints on its new last row
    old = signal.signal(signal.SIGWINCH, lambda *_: os.write(wake_write, b"."))
    watches[Path("<resize>")] = wake_read
    try:
        while iterations is None or done < iterations:
            line = render(sources)
            shown = (line, shutil.get_terminal_size((80, 24)))
            if shown != previous:
                # clear, hide the cursor, and print on the last row, where the video window leaves the tile uncovered
                out.write(f"\033[2J\033[?25l\033[{shutil.get_terminal_size((80, 24)).lines};1H{line}")
                out.flush()
                previous = shown
            done += 1
            if iterations is not None and done >= iterations:
                break
            wanted = sources.directories() | {Path("<resize>")}
            for directory in watches.keys() - wanted:
                os.close(watches.pop(directory))
            for directory in wanted - watches.keys():
                watches[directory] = watch(directory, MODIFY | CREATE | MOVED_TO | CLOSE_WRITE)
            ready = select.select(list(watches.values()), [], [], REFRESH_SECONDS)[0]
            for fd in ready:
                drain(fd)
            if ready:
                time.sleep(0.2)  # let a burst of appends settle into one redraw
    finally:
        signal.signal(signal.SIGWINCH, old)
        for fd in (*watches.values(), wake_write):
            os.close(fd)
    return {"closed": True, "role": role}
