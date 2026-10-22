"""`decomp tas watch [--next]`: one JSON line per game event, read from the running page's /state.

Events: `start` (a new run), `stop` / `crash` (the port held a stop or host fault: subject, polls, run folder, stack
files), `hang` (running, no new poll for `HANG_SECONDS`), `end` (the port exited: exit code) and `mark` (the page's
Weird button: poll, note, frame path, saved movie name). `Watcher` derives them from successive /state payloads and
needs no server, so an agent loop calls `decomp tas watch --next` and gets exactly the next one.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from typing import Any

from abu_decomp import DecompError, arity, tas
from abu_decomp.config import Profile

HANG_SECONDS = 15.0


class Watcher:
    def __init__(self, hang_seconds: float = HANG_SECONDS) -> None:
        self.hang_seconds = hang_seconds
        self.prev: dict[str, Any] | None = None
        self.moved = 0.0
        self.hung = False

    def feed(self, state: dict[str, Any], now: float) -> list[dict[str, Any]]:
        """The events `state` shows beyond the previous one. The first state is only the baseline."""
        prev, self.prev = self.prev, state
        if prev is None:
            self.moved = now
            return []
        base = {"run": state.get("run"), "polls": state.get("polls", 0)}
        events: list[dict[str, Any]] = []
        new_run = state.get("run") != prev.get("run")
        if new_run:
            self.moved, self.hung = now, False
            if state.get("run"):
                events.append({"event": "start", **base})
        elif state.get("polls") != prev.get("polls"):
            self.moved, self.hung = now, False
        stopped = state.get("stopped") or {}
        if stopped and (new_run or not prev.get("stopped")):
            events.append(
                {
                    "event": stopped.get("outcome", "stop"),
                    "subject": stopped.get("subject"),
                    **base,
                    "stacks": state.get("stacks", []),
                }
            )
        if state.get("running") and not self.hung and now - self.moved >= self.hang_seconds:
            self.hung = True
            events.append({"event": "hang", "idle_seconds": round(now - self.moved), **base})
        if state.get("exit") is not None and (new_run or prev.get("exit") is None) and state.get("run"):
            events.append({"event": "end", "exit": state["exit"], **base})
        old = 0 if new_run else (prev.get("marks") or {}).get("count", 0)
        marks = state.get("marks") or {}
        if marks.get("count", 0) > old and marks.get("last"):
            last = marks["last"]
            events.append(
                {
                    "event": "mark",
                    "poll": last.get("poll"),
                    "note": last.get("note", ""),
                    "frame": last.get("frame"),
                    "movie": last.get("movie"),
                    **{k: v for k, v in base.items() if k == "run"},
                }
            )
        return events


def state_url(profile: Profile) -> str:
    listen = str(dict(profile.section("tas").get("serve", {})).get("listen", "127.0.0.1:8097"))
    host, _, port = listen.rpartition(":")
    return f"http://{'127.0.0.1' if host in ('', '0.0.0.0') else host}:{port}/state"


def fetch(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=10) as reply:
        return json.load(reply)


def watch(profile: Profile, *, once: bool, interval: float = 1.0) -> int:
    tas.settings(profile)  # the section must be valid
    url = state_url(profile)
    watcher = Watcher()
    first = True
    while True:
        try:
            events = watcher.feed(fetch(url), time.monotonic())
            first = False
        except (OSError, ValueError) as error:
            if first:
                raise DecompError("tas.watch", f"cannot read {url}: {error}") from error
            events = []  # the page restarting is not an event
        for event in events:
            if event.get("stacks"):
                found = arity.notes(profile, arity.frame_names(event["stacks"]))
                if found:
                    event["arity"] = found
            sys.stdout.write(json.dumps(event, sort_keys=True) + "\n")
            sys.stdout.flush()
            if once:
                return 0
        time.sleep(interval)
