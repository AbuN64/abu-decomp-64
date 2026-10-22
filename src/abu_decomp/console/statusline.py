"""One-line status feed: print the latest event now and again after every append (for a bar status_command)."""

from __future__ import annotations

import json
import select
import sys
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from abu_decomp.stream.inotify import CLOSE_WRITE, CREATE, MODIFY, MOVED_TO, drain, watch

from .feed import EventReader, format_event


def line_for(record: dict[str, Any], width: int = 220) -> str:
    return format_event(record)[0][:width]


def blocks(line: str, buttons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One bar update: a clickable block per button, then the newest event."""
    row = [
        {"name": f"button-{index}", "full_text": f" {button['label']} ", "color": "#11111b", "background": "#f9e2af"}
        for index, button in enumerate(buttons)
    ]
    return [*row, {"name": "event", "full_text": line}]


def listen(stream: Iterable[str], buttons: list[dict[str, Any]], run: Callable[[list[str]], Any]) -> None:
    """Run the command of every button the bar reports as clicked (i3bar click events, one JSON object per line)."""
    for raw in stream:
        text = raw.strip().lstrip("[,").strip()
        if not text:
            continue
        try:
            name = json.loads(text).get("name")
        except (ValueError, AttributeError):
            continue
        for index, button in enumerate(buttons):
            if name == f"button-{index}":
                run(button["command"])


def follow(
    path: Path,
    out: Any = None,
    iterations: int | None = None,
    buttons: list[dict[str, Any]] | None = None,
    run: Callable[[list[str]], Any] | None = None,
    clicks: Iterable[str] | None = None,
) -> None:
    """With buttons the bar speaks the i3bar protocol so a click on a button runs its command."""
    out = out or sys.stdout
    buttons = buttons or []
    if buttons:
        print('{"version":1,"click_events":true}\n[', file=out, flush=True)
        threading.Thread(
            target=listen, args=(clicks if clicks is not None else sys.stdin, buttons, run), daemon=True
        ).start()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = watch(path.parent, MODIFY | CREATE | MOVED_TO | CLOSE_WRITE)
    reader = EventReader(path)
    done = 0
    while iterations is None or done < iterations:
        records = reader.read_new()
        line = line_for(records[-1]) if records else "no events yet" if done == 0 else None
        if line is not None:
            print(json.dumps(blocks(line, buttons)) + "," if buttons else line, file=out, flush=True)
        done += 1
        if iterations is not None and done >= iterations:
            break
        select.select([fd], [], [])
        drain(fd)
