"""One-line status feed: print the latest event now and again after every append (for a bar status_command)."""

from __future__ import annotations

import select
import sys
from pathlib import Path
from typing import Any

from abu_decomp.stream.inotify import CLOSE_WRITE, CREATE, MODIFY, MOVED_TO, drain, watch

from .feed import EventReader, format_event


def line_for(record: dict[str, Any], width: int = 220) -> str:
    return format_event(record)[0][:width]


def follow(path: Path, out: Any = None, iterations: int | None = None) -> None:
    out = out or sys.stdout
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = watch(path.parent, MODIFY | CREATE | MOVED_TO | CLOSE_WRITE)
    reader = EventReader(path)
    done = 0
    while iterations is None or done < iterations:
        records = reader.read_new()
        if records:
            print(line_for(records[-1]), file=out, flush=True)
        elif done == 0:
            print("no events yet", file=out, flush=True)
        done += 1
        if iterations is not None and done >= iterations:
            break
        select.select([fd], [], [])
        drain(fd)
