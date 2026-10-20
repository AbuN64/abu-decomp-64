"""Event-driven readers for the shared console; paths and project facts stay in profiles."""

from __future__ import annotations

import json
from pathlib import Path

from .dashboard_model import Dashboard, Event, EventLog, Row, mission_model
from .feed import EventReader, canonical_kind
from .host_output import HostOutput
from .panes import PANES, lifecycle_model
from .port import HARNESS, port_view


class Sources:
    def __init__(
        self,
        events: Path,
        *,
        title: str,
        config: dict | None = None,
        kinds: set[str] | None = None,
        host_log: Path | None = None,
        limit: int = 200,
        actors: tuple[str, ...] = (),
    ):
        self.title, self.config, self.kinds, self.limit = title, config or {}, kinds, limit
        self.actors = frozenset(actors)
        paths = [events, *(Path(path) for path in self.config.get("events", []))]
        self.readers = [EventReader(path) for path in dict.fromkeys(paths)]
        self.records: dict[Path, list[dict]] = {reader.path: [] for reader in self.readers}
        self.host_path = host_log or (Path(self.config["host_log"]) if self.config.get("host_log") else None)
        self.host = HostOutput(self.host_path) if self.host_path else None
        self.cache: dict[Path, tuple[tuple, str]] = {}
        self.watched: set[Path] = set(paths)
        self.errors: list[str] = []
        self.ordered: tuple[EventLog, EventLog] | None = None
        self.last_receipt = None
        self.landings: dict[str, dict] = {}  # confirmed pushes, retained beyond the display tail
        self.panes = {name: PANES[name](options) for name, options in self.config.get("panes", {}).items()}

    def shown(self, event: dict) -> bool:
        """Kind filter; kindless events of the tile's `actors` (e.g. build) are shown too."""
        kind = canonical_kind(event)
        if self.kinds is None or kind in self.kinds:
            return True
        return kind is None and event.get("actor") in self.actors

    def text(self, path: str | Path | None, *, limit: int = 262144) -> str:
        if not path:
            return ""
        file = Path(path)
        self.watched.add(file)
        try:
            stat = file.stat()
            signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size, limit)
            if file in self.cache and self.cache[file][0] == signature:
                return self.cache[file][1]
            with file.open("rb") as stream:
                stream.seek(max(0, stat.st_size - limit))
                content = stream.read(limit).decode(errors="replace")
            self.cache[file] = signature, content
            return content
        except FileNotFoundError:
            return ""
        except OSError as error:
            self.errors.append(f"{file}: {error}")
            return ""

    def json(self, path: str | Path | None) -> dict:
        text = self.text(path, limit=2097152)
        if not text:
            return {}
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else {}
        except ValueError as error:
            self.errors.append(f"{path}: {error}")
            return {}

    def read(self) -> Dashboard:
        self.errors = []
        self.watched = {reader.path for reader in self.readers}
        for reader in self.readers:
            previous = reader.count
            try:
                new = reader.read_new()
                if reader.count < previous + len(new):
                    self.records[reader.path] = []
                if new or reader.count < previous + len(new):
                    self.ordered = None
                for event in new:
                    payload = event.get("payload") or {}
                    if payload.get("op") == "shipped" and payload.get("pushed") and event.get("status") == "passed":
                        self.landings[payload["pushed"]] = event
                self.records[reader.path] = (self.records[reader.path] + [Event(event) for event in new])[-4000:]
            except (ValueError, OSError) as error:
                self.errors.append(f"{reader.path}: {error}")
        if self.ordered is None:
            all_events = EventLog(
                sorted(
                    (event for records in self.records.values() for event in records),
                    key=lambda event: event.get("time", ""),
                )
            )
            self.ordered = (
                all_events,
                EventLog(event for event in all_events if self.shown(event)),
            )
        all_events, events = self.ordered
        if self.host:
            self.watched.add(self.host_path)
            self.host.read_new()
        if self.config.get("build_profile") or self.config.get("supervisor_status"):
            model = self.port_model(events)
        elif self.panes:
            model = lifecycle_model(self.panes, self, events, self.title)
        else:
            runtime = self.json(self.config.get("run_status"))
            run_id = runtime.get("run_id") or runtime.get("id")
            if not run_id and runtime.get("run_dir"):
                run_id = Path(runtime["run_dir"]).name
            if not run_id:
                run_id = next(
                    (
                        (event.get("payload") or {}).get("run_id")
                        for event in reversed(events)
                        if (event.get("payload") or {}).get("run_id")
                    ),
                    "unknown",
                )
            model = mission_model(
                events[-self.limit :],
                title=self.title,
                frames=self.host.frames if self.host else "?",
                graphics=self.host.graphics_tasks if self.host else "?",
                run_id=str(run_id),
                raw_log=self.text(self.host_path, limit=32768),
            )
        for index, error in enumerate(self.errors):
            model.rows.insert(0, Row(f"source-error-{index}", "Source unavailable", error, "stop"))
        return model

    def port_model(self, events: EventLog) -> Dashboard:
        """The supervised port: boot step and stop on the title line, the game's C stack (port.port_view), then
        the tile's panes (a lane's own rows) above it, so the stack sits at the tail."""
        runtime = self.json(self.config.get("supervisor_status"))
        raw_path = runtime.get("log")
        if not raw_path and runtime.get("stack"):
            raw_path = str(Path(runtime["stack"]).parent / "output.log")
        hide = tuple(self.config.get("stack_hide", HARNESS))
        facts, pins, stack = port_view(self, runtime, self.text(raw_path), hide)
        lane = lifecycle_model(self.panes, self, events, self.title) if self.panes else Dashboard(self.title, "")
        rows = [row for row in lane.rows if not row.key.endswith("-none")] + stack
        return Dashboard(self.title, "", rows, None, lane.footer, [*pins, *lane.pinned], facts)

    def directories(self) -> set[Path]:
        directories = set()
        for path in self.watched:
            parent = path.parent
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            directories.add(parent)
        return directories
