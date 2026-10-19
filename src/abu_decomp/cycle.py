"""`decomp cycle`: one long-running process that rebuilds the decomp's evidence when its sources settle.

It blocks on inotify only: one watch per directory under the configured source roots (new directories are added as
they appear) and the landed refs' directories (RefSource, moved from the old console follower). A matching change
opens a burst; the burst ends after `settle_seconds` with no further matching change, and then the steps run once:
the configured Recipe Maker command (`decomp recipe ...`), the `propose` steps, then `unbake report`. MATCH carries
the per-version numbers of the previous and the new report when they differ; LAND is appended per new landed
commit by RefSource; a failed
step appends RUN-FAIL. Nothing runs on a timer: the only timeouts are the settle window of an open burst and, when
a refs table sets `pull_seconds`, the `git pull --ff-only` that moves the local refs (RefSource's own timer).

A run's steps, in order, each optional: `build` (a fresh `decomp build ARGS` process, so it reads the profile file as
it is now), `recipe`, `propose` (Recipe's proposal verbs on the files the landed commits changed, as `decomp land`
runs them on its proof tree: abu_decomp.proposals; the result is one LAND event from the cycle's actor carrying the
summary, and nothing is applied), then `unbake report` with its MATCH (`report`, default true). `run_at_start` runs
once before waiting, as the old build loop did. `land_refs` is a second refs table that only appends LAND and
triggers nothing, except that with `propose` its new commits run the proposals alone.

    "cycle": {
      "sources": ["src", "include"],          relative to tools.project; watched recursively
      "suffixes": [".c", ".h", ".s"],         a change counts only for these suffixes (case-sensitive)
      "exclude": ["build"],                   directory names never watched
      "settle_seconds": 2,
      "recipe": ["check", "--all"],           Recipe Maker command and arguments; omit for report-only
      "propose": [{"name": "structure", "args": ["structure", "--json", "--paths", "{files}"]}],
      "refs": {"repos": [{"name", "path", "ref"}], "state_path": "...", "seed_commits": 0, "pull_seconds": 60},
      "land_refs": {...same shape, its own state_path...},
      "build": ["once"], "report": true, "run_at_start": false,
      "actor": "cycle"
    }

Changes written while the steps run are kept and start the next burst, so the source roots must not contain the
steps' own outputs (unbake writes progress.json at the project root).
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, events, proposals, tools
from abu_decomp.config import Profile
from abu_decomp.stream.inotify import ATTRIB, CLOSE_WRITE, CREATE, DELETE, ISDIR, MOVED_TO, Watcher, drain

KEYS = frozenset(
    {
        "sources",
        "suffixes",
        "exclude",
        "settle_seconds",
        "recipe",
        "propose",
        "refs",
        "land_refs",
        "build",
        "report",
        "run_at_start",
        "actor",
    }
)
SOURCE_MASK = CLOSE_WRITE | MOVED_TO | CREATE | DELETE | ATTRIB
COUNTERS = ("code_matched", "data_matched", "functions_matched", "code_fuzzy")


def settings(profile: Profile) -> dict[str, Any]:
    table = profile.section("cycle")
    unknown = table.keys() - KEYS
    if unknown:
        raise DecompError("config.schema", f"cycle: unknown keys: {', '.join(sorted(unknown))}")
    sources = table.get("sources", [])
    if not isinstance(sources, list) or not (sources or table.get("refs")):
        raise DecompError("config.schema", "cycle.sources: a list is required, nonempty unless cycle.refs is set")
    if sources and (not isinstance(table.get("suffixes"), list) or not table["suffixes"]):
        raise DecompError("config.schema", "cycle.suffixes: a nonempty list is required with sources")
    report = table.get("report", True)
    propose = (
        proposals.check_steps(profile.source_file, "cycle.propose", table["propose"]) if "propose" in table else []
    )
    project = tools.settings(profile).project if sources or report or table.get("recipe") or propose else None
    return {
        "sources": [project / value for value in sources] if project else [],
        "suffixes": tuple(table.get("suffixes", ())),
        "exclude": frozenset(table.get("exclude", ())),
        "settle": float(table.get("settle_seconds", 2)),
        "recipe": list(table["recipe"]) if table.get("recipe") else None,
        "propose": propose,
        "refs": table.get("refs"),
        "land_refs": table.get("land_refs"),
        "build": list(table["build"]) if table.get("build") else None,
        "report": bool(report),
        "run_at_start": bool(table.get("run_at_start", False)),
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
        run_command: Callable[..., Any] = subprocess.run,
    ) -> None:
        self.profile, self.invoke, self.append, self.run_command = profile, invoke, append, run_command
        self.config = settings(profile)
        self.feed = events.events_path(profile)
        self.watcher = Watcher()
        for root in self.config["sources"]:
            self.add_tree(root)
        self.refs = self.land_refs = None
        if self.config["refs"] or self.config["land_refs"]:
            from abu_decomp.console.gitrefs import RefSource

            factory = ref_source or RefSource
            if self.config["refs"]:
                self.refs = factory(profile, {"seed_commits": 0, "actor": "git", **self.config["refs"]})
            if self.config["land_refs"]:
                self.land_refs = factory(profile, {"seed_commits": 0, "actor": "git", **self.config["land_refs"]})
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

    def propose(self, landed: list[str] | None) -> dict[str, Any] | None:
        """Recipe's proposals for the landed files (else the changed sources of the burst); one LAND event."""
        project = tools.settings(self.profile).project
        files = proposals.source_files(project, landed or [])
        summary = proposals.run(self.profile, None, self.config["propose"], files, invoke=self.invoke)
        self.append(
            self.feed,
            {
                "actor": self.config["actor"],
                "status": "info" if all(step.get("ok") for step in summary.values()) else "failed",
                "kind": "LAND",
                "message": f"{proposals.line(summary)} for {len(files)} landed file(s)",
                "payload": {"recipe": summary, "files": files[:50]},
            },
        )
        return summary

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

    def build(self) -> bool:
        """`decomp build ARGS` in a fresh process; its own started/terminal events go to the feed."""
        args = self.config["build"]
        argv = [sys.executable, "-m", "abu_decomp", "--profile", str(self.profile.source_file), "build", *args]
        called = self.run_command(argv, capture_output=True, text=True, check=False)
        if called.stderr:
            sys.stderr.write(called.stderr)
        try:
            envelope = json.loads(called.stdout)
        except ValueError:
            envelope = {"ok": False, "findings": [{"message": f"no JSON envelope, exit {called.returncode}"}]}
        if called.returncode == 0 and envelope.get("ok"):
            return True
        finding = (envelope.get("findings") or [{}])[0]
        self.fail("build " + " ".join(args), str(finding.get("message", f"exit {called.returncode}")))
        return False

    def run(self, trigger: list[str], landed: list[str] | None = None) -> dict[str, Any]:
        """The steps for one settled burst: build, Recipe, then the report and its MATCH (each when configured)."""
        record: dict[str, Any] = {"trigger": sorted(set(trigger)), "build": None, "recipe": None, "match": None}
        if set(trigger) == {"landed tree"}:  # commits of the landed-only refs: just Recipe's proposals
            record["propose"] = self.propose(landed)
            self.runs.append(record)
            print("cycle run " + json.dumps(record), file=sys.stderr, flush=True)
            return record
        if self.config["build"]:
            record["build"] = self.build()
        if self.config["recipe"]:
            try:
                result = self.invoke("recipe", self.profile, self.config["recipe"])
                record["recipe"] = result.ok
                if not result.ok:
                    self.fail("recipe " + " ".join(self.config["recipe"]), f"exit {result.returncode}")
            except DecompError as error:
                record["recipe"] = False
                self.fail("recipe " + " ".join(self.config["recipe"]), error.detail)
        if self.config["propose"]:
            record["propose"] = self.propose(landed)
        now = self.report() if self.config["report"] else None
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
        if baseline and self.config["report"]:
            self.last = self.report()
        print(f"cycle watching {len(self.watcher.directories)} directories", file=sys.stderr, flush=True)
        sources = [source for source in (self.refs, self.land_refs) if source]
        fds = {fd: source for source in sources for fd in source.watch_fds()}
        pulls = {
            id(source): time.monotonic() + source.pull_seconds
            for source in sources
            if getattr(source, "pull_seconds", None)
        }
        burst: list[str] = []
        landed: list[str] = []
        deadline: float | None = None
        if self.land_refs:
            self.land_refs.scan()
        if self.refs and self.refs.scan():  # commits that landed while the cycle was down
            burst, deadline = ["landed refs"], time.monotonic() + self.config["settle"]
            landed.extend(self.refs.touched)
        if self.config["run_at_start"] and not self.runs:
            self.run(["start"])
        while max_runs is None or len(self.runs) < max_runs:
            wakes = [*pulls.values(), *([deadline] if deadline is not None else [])]
            timeout = None if not wakes else max(0.0, min(wakes) - time.monotonic())
            ready = select.select([self.watcher.fd, *fds], [], [], timeout)[0]
            woke = []
            if self.watcher.fd in ready:
                woke.extend(self.relevant(self.watcher.read()))
            for fd, source in fds.items():
                if fd in ready:
                    drain(fd)
                    if source.scan() and source is self.refs:
                        woke.append("landed refs")
                        landed.extend(source.touched)
                    elif source is self.land_refs and self.config["propose"] and source.touched:
                        woke.append("landed tree")
                        landed.extend(source.touched)
            for source in sources:
                due = pulls.get(id(source))
                if due is not None and time.monotonic() >= due:
                    source.pull()  # moves the local refs; the ref inotify above reports what landed
                    pulls[id(source)] = time.monotonic() + source.pull_seconds
            if woke:
                burst.extend(woke)
                deadline = time.monotonic() + self.config["settle"]
            elif deadline is not None and time.monotonic() >= deadline:
                self.run(burst, landed)
                burst, landed, deadline = [], [], None
        return self.runs

    def close(self) -> None:
        self.watcher.close()


def serve(profile: Profile) -> dict[str, Any]:
    cycle = Cycle(profile)
    try:
        return {"runs": cycle.serve()}
    finally:
        cycle.close()
