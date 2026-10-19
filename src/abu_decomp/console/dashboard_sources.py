"""Event-driven readers for the shared console; paths and project facts stay in profiles."""

from __future__ import annotations

import glob
import json
import time
from pathlib import Path

from .dashboard_model import Dashboard, Event, EventLog, Row, boot_model, event_epoch, mission_model
from .feed import EventReader, canonical_kind
from .host_output import HostOutput
from .panes import PANES, lifecycle_model


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

    def plan(self) -> tuple[str, str]:
        pattern = str(self.config.get("boot_plan", ""))
        if not pattern:
            return "", ""
        if glob.has_magic(pattern):
            base = Path(pattern.split("*", 1)[0]).parent
            self.watched.add(base / ".console-watch")
            matches = [Path(path) for path in glob.glob(pattern)]
            path = max(matches, key=lambda file: file.stat().st_mtime_ns) if matches else None
        else:
            path = Path(pattern)
        return self.text(path), str(path or pattern)

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
        if self.config.get("build_profile"):
            profile = self.json(self.config["build_profile"])
            workspace = Path(profile.get("build", {}).get("workspace", "."))
            status = self.json(workspace / "status.json")
            receipt = self.json(status.get("receipt"))
            steps = receipt.get("steps", [])
            gate_names = {
                command["name"] for command in profile.get("build", {}).get("commands", []) if command.get("expect_all")
            }
            gate_log = "\n".join(self.text(step.get("log")) for step in steps if step["name"] in gate_names)
            if status.get("gate") in gate_names and not gate_log:
                gate_log = self.text(status.get("log_path"))
            test_step = self.config.get("test_step", "ctest")
            test_log = "\n".join(self.text(step.get("log")) for step in steps if step["name"] == test_step)
            runtime = self.json(self.config.get("supervisor_status"))
            raw_path = runtime.get("log")
            if not raw_path and runtime.get("stack"):
                raw_path = str(Path(runtime["stack"]).parent / "output.log")
            plan, plan_path = self.plan()
            release = (
                self.json(Path(self.config["release_current"]) / "manifest.json")
                if self.config.get("release_current")
                else {}
            )
            boot = boot_model(
                profile,
                status,
                receipt,
                gate_log,
                runtime,
                all_events,
                test_log=test_log,
                plan=plan,
                plan_path=plan_path,
                plan_after=self.config.get("plan_after", ""),
                plan_before=self.config.get("plan_before", ""),
                release=release,
                raw_log=self.text(raw_path),
                test_step=test_step,
                build_step=self.config.get("build_step", "build"),
            )
            model = self.with_lane(boot, status, receipt, runtime, events) if self.panes else boot
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

    def boot_is_current(self, status: dict, receipt: dict, runtime: dict) -> bool:
        """A boot gate is current when its build or run was written within `boot_fresh_seconds` (default 900)."""
        stamps = [
            event_epoch({"time": value})
            for value in (runtime.get("updated_at"), status.get("started_at"), receipt.get("started_at"))
            if value
        ]
        newest = max((s for s in stamps if s), default=None)
        return newest is not None and time.time() - newest <= float(self.config.get("boot_fresh_seconds", 900))

    def with_lane(self, boot: Dashboard, status: dict, receipt: dict, runtime: dict, events: EventLog) -> Dashboard:
        """The tile's panes (a lane's live effort) with the boot gate's rows and status lines only while current."""
        model = lifecycle_model(self.panes, self, events, self.title)
        if self.boot_is_current(status, receipt, runtime):
            model.rows.extend(boot.rows)
            model.pinned.extend(boot.pinned)
        return model

    def directories(self) -> set[Path]:
        directories = set()
        for path in self.watched:
            parent = path.parent
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            directories.add(parent)
        return directories
