"""`decomp ship`: one matched input from queue to the remote branch in minutes, on one warm tree.

The tree stays built at the remote branch: every ship moves it there (git rewrites only the files that changed, so
setup and make stay incremental), lands the one input with every gate `decomp land` runs, commits the land's plan,
and pushes fast-forward. One lock per repository serializes ships. A refused input leaves the tree clean at the
branch; a push the remote refuses because the branch moved lands the input again on the new tip, once.
"""

from __future__ import annotations

import fcntl
import os
import re
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, events, land
from abu_decomp.config import Profile

Result = tuple[dict[str, Any], int, list[dict[str, Any]]]
IDENTITY = re.compile(r"^\s*(?P<name>[^<>]+?)\s*<(?P<email>[^<>\s]+)>\s*$")
LOCK_WAIT_SECONDS = 7200.0


class ShipError(DecompError):
    pass


class Ship:
    def __init__(
        self,
        profile: Profile,
        tree: Path,
        patches: Sequence[tuple[str, Path]],
        *,
        messages: dict[str, str] | None = None,
        run: land.Run = land.run_process,
        landing: Callable[..., land.Result] = land.land,
        append: Callable[[Path, dict[str, Any]], dict[str, Any]] = events.append,
        clock: Callable[[], float] = time.monotonic,
        lock_wait: float = LOCK_WAIT_SECONDS,
    ) -> None:
        self.profile, self.patches, self.messages = profile, list(patches), dict(messages or {})
        self.tree = tree.expanduser().resolve()
        if not (self.tree / ".git").exists():
            raise ShipError("ship.tree", f"{self.tree} is not a git checkout")
        if len(self.patches) != 1:
            raise ShipError("ship.arguments", "ship lands exactly one --patch; batch inputs go through `decomp land`")
        table = land.settings(profile)
        self.remote, self.branch, self.author = table["remote"], table["branch"], table["author"]
        self.run, self.landing, self.append, self.clock, self.lock_wait = run, landing, append, clock, lock_wait
        self.steps: list[dict[str, Any]] = []

    # git ----------------------------------------------------------------------------------------------------------
    def git(self, *args: str, ok: bool = True, env: dict[str, str] | None = None) -> tuple[int, str]:
        code, out, err = self.run(["git", *args], self.tree, {**os.environ, **env} if env else None, 600)
        if code and ok:
            raise ShipError("ship.git", f"git {' '.join(args[:3])}: {(err or out).strip()[-400:]}")
        return code, (out if not code else (err or out))

    def step(self, name: str, started: float, **detail: Any) -> None:
        self.steps.append({"step": name, "seconds": round(self.clock() - started, 3), **detail})

    def identity(self) -> dict[str, str]:
        """GIT_AUTHOR/COMMITTER for the commits: `land.author` ("Name <email>"), else the tree's own git config."""
        if not self.author:
            return {}
        found = IDENTITY.match(self.author)
        if not found:
            raise ShipError("config.schema", f"land.author: expected 'Name <email>', got {self.author!r}")
        name, email = found["name"], found["email"]
        return {
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
        }

    def lock(self) -> int:
        """The repository's ship lock (in git's common dir, so every worktree of one clone shares it)."""
        common = Path(self.git("rev-parse", "--git-common-dir")[1].strip())
        path = (common if common.is_absolute() else self.tree / common) / "decomp-ship.lock"
        handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        deadline = self.clock() + self.lock_wait
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except BlockingIOError:
                if self.clock() > deadline:
                    os.close(handle)
                    raise ShipError("ship.busy", f"another ship held {path} for {self.lock_wait:g}s") from None
                time.sleep(2)

    def tip(self) -> str:
        """Fetch the branch and move the tree to its tip: only the files that differ are rewritten, so the build
        state stays warm. A tree with tracked changes is refused, never reset over."""
        started = self.clock()
        self.git("fetch", "-q", self.remote, self.branch)
        sha = self.git("rev-parse", "--verify", "FETCH_HEAD^{commit}")[1].strip()  # a remote name, URL or path
        dirty = self.git("status", "--porcelain", "--untracked-files=no")[1].splitlines()
        if dirty:
            raise ShipError("ship.dirty", f"{self.tree} has {len(dirty)} tracked changes; ship never resets over them")
        if self.git("rev-parse", "HEAD")[1].strip() != sha:
            self.git("checkout", "-q", "--detach", sha)
        self.step("tip", started, sha=sha)
        return sha

    def clean(self, base: str) -> None:
        """The tree back at `base` with nothing the land wrote: tracked files reset, untracked (not ignored) files
        removed. Ignored build state stays, so the next ship is still warm."""
        self.git("checkout", "-q", "--detach", "--force", base)
        self.git("clean", "-q", "-f", "-d")

    def commit(self, result: dict[str, Any]) -> str:
        started = self.clock()
        env = self.identity()
        for plan in result["commits"]:
            if not plan["files"] or not plan["message"]:
                continue
            self.git("update-index", "--add", "--remove", "--", *plan["files"])
            body = ["-m", plan["body"]] if plan["body"] else []
            self.git("commit", "-q", "-m", plan["message"], *body, "--", *plan["files"], env=env)
        sha = self.git("rev-parse", "HEAD")[1].strip()
        self.step("commit", started, sha=sha)
        return sha

    def push(self) -> bool:
        """Fast-forward only: a refused push (the branch moved) is False, any other failure raises."""
        started = self.clock()
        code, text = self.git("push", "-q", self.remote, f"HEAD:refs/heads/{self.branch}", ok=False)
        self.step("push", started, ok=code == 0)
        if code and re.search(r"non-fast-forward|fetch first|rejected", text):
            return False
        if code:
            raise ShipError("ship.push", f"git push {self.remote} {self.branch}: {text.strip()[-400:]}")
        return True

    def execute(self) -> Result:
        started = self.clock()
        handle = self.lock()
        try:
            for attempt in (1, 2):
                base = self.tip()
                result, code, findings = self.landing(
                    self.profile, self.tree, base, self.patches, messages=self.messages
                )
                if code:
                    return self.finish(result, findings, base, None, started)
                if any(f.get("key") == "land.file_refused" for f in findings):  # one input lands whole or not at all
                    self.clean(base)
                    return self.finish({**result, "pass": False}, findings, base, None, started)
                sha = self.commit(result)
                if sha == base:  # nothing to commit: the branch already holds the input
                    self.clean(base)
                    findings.append(
                        {"key": "ship.empty", "message": f"the input changes nothing on {base[:12]}", "blocking": True}
                    )
                    return self.finish({**result, "pass": False}, findings, base, None, started)
                if self.push():
                    return self.finish(result, findings, base, sha, started)
                self.clean(base)  # the landed commits stay local only; the next tip moves on
                if attempt == 2:
                    findings.append(
                        {"key": "ship.moved", "message": f"{self.remote}/{self.branch} moved twice during the ship",
                         "blocking": True}
                    )  # fmt: skip
                    return self.finish(result, findings, base, None, started)
            raise AssertionError("unreachable")
        finally:
            os.close(handle)

    def finish(
        self, result: dict[str, Any], findings: list[dict[str, Any]], base: str, sha: str | None, started: float
    ) -> Result:
        lane = self.patches[0][0]
        names = sorted({n for row in result.get("lanes", []) for n in (row.get("functions") or {}).get("names", [])})
        refusal = next((f for f in findings if f.get("blocking")), None)
        shipped = {
            "lane": lane,
            "base": base,
            "pushed": sha,
            "functions": names,
            "seconds": round(self.clock() - started, 3),
            "steps": self.steps,
            "refused": None if sha else (refusal or {"key": "ship.refused", "message": "the land refused the input"}),
            "land": result,
        }
        record = {
            "actor": land.settings(self.profile)["actor"],
            "status": "passed" if sha else "failed",
            "message": (
                f"shipped {lane} as {sha[:12]} to {self.remote}/{self.branch} in {shipped['seconds']:.0f}s"
                + (f": {', '.join(names)}" if names else "")
                if sha
                else f"ship refused {lane}: {shipped['refused']['key']}"
            ),
            "kind": "LAND",
            "subject": {"repo": str(self.tree), "ref": sha or base},
            "payload": {"op": "shipped", "lane": lane, "functions": names, "base": base, "pushed": sha},
        }
        try:
            self.append(events.events_path(self.profile), record)
        except DecompError as exc:
            findings.append({**exc.finding(), "blocking": False})
        if sha:
            findings = [{**f, "blocking": False} if f.get("key") == "land.file_refused" else f for f in findings]
        return shipped, int(sha is None), findings


def ship(profile: Profile, tree: Path, patches: Sequence[tuple[str, Path]], **hooks: Any) -> Result:
    return Ship(profile, tree, patches, **hooks).execute()
