"""`decomp cycle`: one long-running process that rebuilds the decomp's evidence when its sources settle.

It blocks on inotify only: one watch per directory under the configured source roots (new directories are added as
they appear) and the landed refs' directories (RefSource, moved from the old console follower). A matching change
opens a burst; the burst ends after `settle_seconds` with no further matching change, and then the steps run once:
the configured Recipe Maker command (`decomp recipe ...`), then `unbake report`. MATCH carries the per-version numbers
of the previous and the new report when they differ; LAND is appended per new landed commit by RefSource; a failed
step appends RUN-FAIL. Nothing runs on a timer: the only timeout is the settle window of an open burst.

    "cycle": {
      "sources": ["src", "include"],          relative to tools.project; watched recursively
      "suffixes": [".c", ".h", ".s"],         a change counts only for these suffixes (case-sensitive)
      "exclude": ["build"],                   directory names never watched
      "settle_seconds": 2,
      "recipe": ["check", "--all"],           Recipe Maker command and arguments; omit for report-only
      "refs": {"repos": [{"name", "path", "ref"}], "state_path": "...", "seed_commits": 0},
      "actor": "cycle"
    }

Changes written while the steps run are kept and start the next burst, so the source roots must not contain the
steps' own outputs (unbake writes progress.json at the project root).
"""

from __future__ import annotations

import json
import os
import select
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, events, tools
from abu_decomp.config import Profile
from abu_decomp.stream.inotify import ATTRIB, CLOSE_WRITE, CREATE, DELETE, ISDIR, MOVED_TO, Watcher, drain

KEYS = frozenset({"sources", "suffixes", "exclude", "settle_seconds", "recipe", "refs", "actor"})
SOURCE_MASK = CLOSE_WRITE | MOVED_TO | CREATE | DELETE | ATTRIB
COUNTERS = ("code_matched", "data_matched", "functions_matched", "code_fuzzy")


def settings(profile: Profile) -> dict[str, Any]:
    table = profile.section("cycle")
    unknown = table.keys() - KEYS
    if unknown:
        raise DecompError("config.schema", f"cycle: unknown keys: {', '.join(sorted(unknown))}")
    for key in ("sources", "suffixes"):
        if not isinstance(table.get(key), list) or not table[key]:
            raise DecompError("config.schema", f"cycle.{key}: a nonempty list is required")
    project = tools.settings(profile).project
    return {
        "sources": [project / value for value in table["sources"]],
        "suffixes": tuple(table["suffixes"]),
        "exclude": frozenset(table.get("exclude", ())),
        "settle": float(table.get("settle_seconds", 2)),
        "recipe": list(table["recipe"]) if table.get("recipe") else None,
        "refs": table.get("refs"),
        "actor": str(table.get("actor", "cycle")),
    }


def versions(document: dict[str, Any]) -> dict[str, dict[str, int]]:
    """version -> the counters of one `unbake report` document."""
    return {
        name: {key: int(row[key]) for key in COUNTERS if key in row}
        for name, row in document["result"]["versions"].items()
    }


def delta_message(before: dict[str, dict[str, int]], after: dict[str, dict[str, int]]) -> str | None:
    parts = []
    for name in sorted(after):
        old = before.get(name, {})
        changes = [
            f"{key.split('_')[0]} {old.get(key, 0)}->{value} ({value - old.get(key, 0):+d})"
            for key, value in after[name].items()
            if old.get(key) != value
        ]
        if changes:
            parts.append(f"{name} " + ", ".join(changes))
    return "progress " + "; ".join(parts) if parts else None


class Cycle:
    def __init__(
        self,
        profile: Profile,
        *,
        invoke: Callable[..., Any] = tools.invoke,
        append: Callable[[Path, dict[str, Any]], Any] = events.append,
        ref_source: Callable[..., Any] | None = None,
    ) -> None:
        self.profile, self.invoke, self.append = profile, invoke, append
        self.config = settings(profile)
        self.feed = events.events_path(profile)
        self.watcher = Watcher()
        for root in self.config["sources"]:
            self.add_tree(root)
        self.refs = None
        if self.config["refs"]:
            from abu_decomp.console.gitrefs import RefSource

            factory = ref_source or RefSource
            self.refs = factory(profile, {"seed_commits": 0, "actor": "git", **self.config["refs"]})
        self.last: dict[str, dict[str, int]] | None = None
        self.runs: list[dict[str, Any]] = []

    def add_tree(self, root: Path) -> None:
        for directory, names, _files in os.walk(root):
            names[:] = [name for name in names if name not in self.config["exclude"]]
            self.watcher.add(Path(directory), SOURCE_MASK)

    def relevant(self, changes: list[tuple[Path, int]]) -> list[str]:
        """Source files among the changes; a new directory is watched (and its files counted) on the spot."""
        found = []
        for path, mask in changes:
            if mask & ISDIR:
                if mask & (CREATE | MOVED_TO) and path.name not in self.config["exclude"] and path.is_dir():
                    self.add_tree(path)
                    found.extend(str(item) for item in path.rglob("*") if item.name.endswith(self.config["suffixes"]))
                continue
            if path.name.endswith(self.config["suffixes"]):
                found.append(str(path))
        return found

    def fail(self, step: str, detail: str) -> None:
        self.append(
            self.feed,
            {
                "actor": self.config["actor"],
                "status": "failed",
                "kind": "RUN-FAIL",
                "message": f"cycle {step} failed: {detail}",
            },
        )

    def report(self) -> dict[str, dict[str, int]] | None:
        try:
            result = self.invoke("unbake", self.profile, ["report"])
        except DecompError as error:
            self.fail("unbake report", error.detail)
            return None
        if not result.ok:
            self.fail("unbake report", f"exit {result.returncode}")
            return None
        return versions(result.document)

    def run(self, trigger: list[str]) -> dict[str, Any]:
        """The steps for one settled burst: Recipe (when configured), then the report and its MATCH."""
        record: dict[str, Any] = {"trigger": sorted(set(trigger)), "recipe": None, "match": None}
        if self.config["recipe"]:
            try:
                result = self.invoke("recipe", self.profile, self.config["recipe"])
                record["recipe"] = result.ok
                if not result.ok:
                    self.fail("recipe " + " ".join(self.config["recipe"]), f"exit {result.returncode}")
            except DecompError as error:
                record["recipe"] = False
                self.fail("recipe " + " ".join(self.config["recipe"]), error.detail)
        now = self.report()
        if now is not None:
            message = delta_message(self.last, now) if self.last is not None else None
            if message:
                payload = {"before": self.last, "after": now, "trigger": record["trigger"][:20]}
                self.append(
                    self.feed,
                    {
                        "actor": self.config["actor"],
                        "status": "info",
                        "kind": "MATCH",
                        "message": message,
                        "payload": payload,
                    },
                )
                record["match"] = message
            self.last = now
        self.runs.append(record)
        print("cycle run " + json.dumps(record), file=sys.stderr, flush=True)
        return record

    def serve(self, *, max_runs: int | None = None, baseline: bool = True) -> list[dict[str, Any]]:
        """Block on inotify; run the steps once per settled burst. Returns after max_runs runs (tests)."""
        if baseline:
            self.last = self.report()
        print(f"cycle watching {len(self.watcher.directories)} directories", file=sys.stderr, flush=True)
        ref_fds = self.refs.watch_fds() if self.refs else []
        burst: list[str] = []
        deadline: float | None = None
        if self.refs and self.refs.scan():  # commits that landed while the cycle was down
            burst, deadline = ["landed refs"], time.monotonic() + self.config["settle"]
        while max_runs is None or len(self.runs) < max_runs:
            timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
            ready = select.select([self.watcher.fd, *ref_fds], [], [], timeout)[0]
            woke = []
            if self.watcher.fd in ready:
                woke.extend(self.relevant(self.watcher.read()))
            for fd in ref_fds:
                if fd in ready:
                    drain(fd)
                    if self.refs.scan():
                        woke.append("landed refs")
            if woke:
                burst.extend(woke)
                deadline = time.monotonic() + self.config["settle"]
            elif deadline is not None and time.monotonic() >= deadline:
                self.run(burst)
                burst, deadline = [], None
        return self.runs

    def close(self) -> None:
        self.watcher.close()


def serve(profile: Profile) -> dict[str, Any]:
    cycle = Cycle(profile)
    try:
        return {"runs": cycle.serve()}
    finally:
        cycle.close()
