"""Generic event source: new commits on configured git refs become narration events.

Wakes on inotify of each ref's directory; a configured timer only runs `git pull` to move the refs.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from abu_decomp.stream.inotify import CREATE, MODIFY, MOVED_TO, drain, watch

from .feed import append_event


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


class RefSource:
    def __init__(self, profile: Any, config: dict[str, Any], append: Callable[..., Any] = append_event):
        self.profile, self.append = profile, append
        self.actor = str(config.get("actor", "git"))
        self.seed = int(config.get("seed_commits", 3))
        self.pull_seconds = float(config["pull_seconds"]) if config.get("pull_seconds") else None
        self.repos = [
            {
                "name": str(item["name"]),
                "path": profile.resolve_path(item["path"]),
                "ref": str(item.get("ref", "refs/heads/main")),
            }
            for item in config["repos"]
        ]
        self.stuck: dict[str, str] = {}  # repo -> why its last pull could not fast-forward
        self.touched: list[str] = []  # files the commits of the last scan changed, repo-relative
        self.state_path = profile.resolve_path(config["state_path"])
        self.seen: dict[str, str] = json.loads(self.state_path.read_text()) if self.state_path.is_file() else {}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.seen))
        os.replace(tmp, self.state_path)

    def scan(self) -> int:
        """Append one event per commit not yet reported; return how many."""
        count = 0
        self.touched = []
        for repo in self.repos:
            head = _git(repo["path"], "rev-parse", repo["ref"])
            if not head:
                continue
            old = self.seen.get(repo["name"])
            if old == head:
                continue
            span = f"{old}..{head}" if old else head
            limit = ["-n", str(self.seen_limit(old))]
            log = _git(repo["path"], "log", "--reverse", "--format=%h %s", *limit, span)
            names = _git(repo["path"], "log", "--format=", "--name-only", *limit, span)
            self.touched.extend(name for name in names.splitlines() if name)
            for line in log.splitlines():
                sha, _, subject = line.partition(" ")
                self.append(self.profile, self.actor, "info", f"{repo['name']} {sha} {subject}", kind="LAND")
                count += 1
            self.seen[repo["name"]] = head
        if count or any(r["name"] not in self.seen for r in self.repos):
            self._save()
        return count

    def seen_limit(self, old: str | None) -> int:
        return 50 if old else self.seed

    def pull(self) -> None:
        """`git pull --ff-only` per repo. A repo that cannot fast-forward (diverged, dirty, fetch failure) appends one
        RUN-FAIL naming why, again only when the reason changes, so a stuck checkout is never silent."""
        for repo in self.repos:
            done = subprocess.run(
                ["git", "-C", str(repo["path"]), "pull", "--ff-only", "--quiet"],
                capture_output=True,
                text=True,
                check=False,
            )
            if done.returncode == 0:
                self.stuck.pop(repo["name"], None)
                continue
            reason = self.stuck_reason(repo["path"], (done.stderr or done.stdout).strip())
            if self.stuck.get(repo["name"]) != reason:
                self.stuck[repo["name"]] = reason
                self.append(
                    self.profile,
                    self.actor,
                    "failed",
                    f"{repo['name']} cannot fast-forward: {reason}",
                    kind="RUN-FAIL",
                )

    @staticmethod
    def stuck_reason(path: Path, error: str) -> str:
        counts = _git(path, "rev-list", "--left-right", "--count", "HEAD...@{u}").split()
        dirty = len(_git(path, "status", "--porcelain", "--untracked-files=no").splitlines())
        parts = [f"ahead {counts[0]}, behind {counts[1]}"] if len(counts) == 2 else []
        if dirty:
            parts.append(f"{dirty} uncommitted file(s)")
        last = error.splitlines()[-1] if error else "git pull failed"
        return ", ".join([*parts, last[:120]])

    def watch_fds(self) -> list[int]:
        fds = []
        for repo in self.repos:
            ref_file = repo["path"] / ".git" / repo["ref"]
            fds.append(watch(ref_file.parent, MODIFY | CREATE | MOVED_TO))
        return fds

    def serve(self, *, iterations: int | None = None) -> None:
        fds = self.watch_fds()
        self.scan()
        next_pull = time.monotonic() + self.pull_seconds if self.pull_seconds else None
        done = 0
        while iterations is None or done < iterations:
            done += 1
            timeout = max(0.0, next_pull - time.monotonic()) if next_pull is not None else None
            ready = select.select(fds, [], [], timeout)[0]
            for fd in ready:
                drain(fd)
            if ready:
                self.scan()
            elif next_pull is not None:
                self.pull()
                next_pull = time.monotonic() + (self.pull_seconds or 0)
                self.scan()
