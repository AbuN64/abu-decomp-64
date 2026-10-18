"""Summarize a host log without copying its per-frame chatter to the console."""

from __future__ import annotations

import re
import time
from pathlib import Path

WARNING = re.compile(r"\b(warn(?:ing)?|error|failed|fatal|unsupported|unimplemented|abort(?:ed)?)\b", re.I)
SCREEN = re.compile(r"screen updates=(\d+).*graphics_tasks=(\d+)")
VI = re.compile(r"cpu_cadence vi=(\d+)")


class HostOutput:
    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.identity = None
        self.partial = ""
        self.frames = self.graphics_tasks = self.vi = "-"
        self.last_heartbeat = None

    def read_new(self, *, now: float | None = None) -> list[str]:
        now = time.monotonic() if now is None else now
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return []
        identity = (stat.st_dev, stat.st_ino)
        if identity != self.identity or stat.st_size < self.offset:
            self.identity, self.offset, self.partial = identity, 0, ""
            self.frames = self.graphics_tasks = self.vi = "-"
        if stat.st_size == self.offset:
            return []
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            data = stream.read().decode("utf-8", errors="replace")
            self.offset = stream.tell()
        parts = (self.partial + data).split("\n")
        self.partial = parts.pop()
        result = []
        metrics = False
        for line in parts:
            screen = SCREEN.search(line)
            cadence = VI.search(line)
            if screen:
                self.frames, self.graphics_tasks = screen.groups()
                metrics = True
            if cadence:
                self.vi = cadence.group(1)
                metrics = True
            if WARNING.search(line):
                result.append("HOST " + line)
        if metrics and (self.last_heartbeat is None or now - self.last_heartbeat >= 60):
            result.append(f"HOST heartbeat frames={self.frames} graphics_tasks={self.graphics_tasks} VI={self.vi}")
            self.last_heartbeat = now
        return result
