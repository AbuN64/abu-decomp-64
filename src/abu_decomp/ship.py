"""`decomp ship`: every ready input from queue to the remote branch in minutes, on one warm tree.

The tree stays built at the remote branch: every ship moves it there (git rewrites only the files that changed, so
setup and make stay incremental), lands all its inputs in one round with every gate `decomp land` runs (one setup, one
check; a build one input breaks is bisected and only that input is refused), commits the land's plan (one commit per
input, in order) and pushes fast-forward once. One lock per repository serializes ships. An input that lands only in
part is refused whole and the others land again without it; a ship with nothing left leaves the tree clean at the
branch; a push the remote refuses because the branch moved lands the inputs again on the new tip, once.
"""

from __future__ import annotations

import fcntl
import os
import re
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, events, land, priority
from abu_decomp.config import Profile

Result = tuple[dict[str, Any], int, list[dict[str, Any]]]
IDENTITY = re.compile(r"^\s*(?P<name>[^<>]+?)\s*<(?P<email>[^<>\s]+)>\s*$")
LOCK_WAIT_SECONDS = 7200.0
# The findings that refuse one input of a ship that pushed the others: blocking for that input only.
INPUT_REFUSALS = frozenset({"land.file_refused", "land.input_build_failed", "land.function_split", *land.REFUSAL_KEYS})
PRIORITY = "UNBAKE_PRIORITY"  # unbake's pool: a command whose environment says "1" takes host slots first


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
        cgroup: Callable[[], str] = lambda: Path("/proc/self/cgroup").read_text(),
    ) -> None:
        self.profile, self.patches, self.messages = profile, list(patches), dict(messages or {})
        self.tree = tree.expanduser().resolve()
        if not (self.tree / ".git").exists():
            raise ShipError("ship.tree", f"{self.tree} is not a git checkout")
        if not self.patches:
            raise ShipError("ship.arguments", "ship needs at least one --patch")
        if len({lane for lane, _ in self.patches}) != len(self.patches):
            raise ShipError("ship.arguments", "two --patch inputs carry one lane name; each input needs its own")
        table = land.settings(profile)
        self.remote, self.branch, self.author = table["remote"], table["branch"], table["author"]
        self.weight, self.cgroup, self.notes = table["lock_weight"], cgroup, []
        self.run, self.landing, self.append, self.clock, self.lock_wait = run, landing, append, clock, lock_wait
        self.steps: list[dict[str, Any]] = []
        self.refused: list[dict[str, Any]] = []  # inputs the ship refused whole before the land that pushed

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
                self.boost()
                return handle
            except BlockingIOError:
                if self.clock() > deadline:
                    os.close(handle)
                    raise ShipError("ship.busy", f"another ship held {path} for {self.lock_wait:g}s") from None
                time.sleep(2)

    def boost(self) -> None:
        """The holder of the ship lock is the job every lane's next tree waits on: its own systemd unit (scope or
        service, from /proc/self/cgroup) gets `land.lock_weight` CPU and IO weight against the lane units beside it
        for as long as it runs. Whatever stops that is a printed finding, never a silent pass."""
        why = ""
        if self.weight is None:
            why = "land.lock_weight is not set in the seat profile"
        else:
            try:
                unit = priority.own_unit(self.cgroup())
            except OSError as exc:
                unit, why = None, f"cannot read /proc/self/cgroup: {exc}"
            if unit is None and not why:
                why = "this process runs in no systemd scope or service"
            elif unit is not None:
                try:
                    code, out, err = self.run(priority.weight_argv(unit, self.weight), self.tree, None, 30)
                except DecompError as exc:
                    code, out, err = 1, "", exc.finding().get("message", str(exc))
                if code:
                    why = f"systemctl --user set-property {unit} failed: {(err or out).strip()[-300:]}"
                else:
                    self.steps.append({"step": "boost", "seconds": 0.0, "unit": unit, "weight": self.weight})
        os.environ[PRIORITY] = "1"  # unbake admits this job's pool work ahead of every command without it
        if why:
            message = f"ship lock held without a CPU/IO boost: {why}"
            sys.stderr.write(f"decomp: {message}\n")
            self.notes.append({"key": "ship.boost", "message": message, "blocking": False})

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

    def land_all(self, base: str, inputs: list[tuple[str, Path]]) -> Result:
        """Land the inputs on `base` in one round. An input that lands only in part (`land.file_refused`) is refused
        whole: the tree goes back to `base` and the others land again without it."""
        while True:
            outcome = self.landing(self.profile, self.tree, base, inputs, messages=self.messages)
            _, code, findings = outcome
            split = [f for f in findings if f.get("key") == "land.file_refused"]
            if code or not split:
                return outcome
            self.clean(base)
            lanes = {f.get("lane") for f in split}
            kept = [row for row in inputs if row[0] not in lanes]
            if not kept or len(kept) == len(inputs):  # nothing left, or a refusal no input owns: nothing lands
                return {**outcome[0], "pass": False}, 1, findings
            for lane in sorted(lane for lane in lanes if lane):
                reason = "; ".join(f["message"] for f in split if f.get("lane") == lane)
                self.refused.append({"lane": lane, "key": "land.file_refused", "message": reason})
                self.notes.append({"key": "ship.input_refused", "message": f"{lane}: {reason}", "blocking": False})
            inputs = kept

    def execute(self) -> Result:
        started = self.clock()
        handle = self.lock()
        try:
            for attempt in (1, 2):
                base = self.tip()
                self.refused = []
                result, code, findings = self.land_all(base, list(self.patches))
                if code:
                    if any(f.get("key") == "land.file_refused" for f in findings):
                        self.clean(base)
                    return self.finish(result, findings, base, None, started)
                sha = self.commit(result)
                if sha == base:  # nothing to commit: the branch already holds the inputs
                    self.clean(base)
                    findings.append(
                        {"key": "ship.empty", "message": f"the inputs change nothing on {base[:12]}", "blocking": True}
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

    def inputs(self, result: dict[str, Any], sha: str | None) -> list[dict[str, Any]]:
        """Each input's outcome, in order: shipped (landed and pushed) or refused with the reason."""
        refused = {row["lane"]: row for row in self.refused}
        refused.update(
            {row["lane"]: {"lane": row["lane"], "key": "land.input_build_failed", "message": row.get("reason", "")}
             for row in result.get("refused_inputs") or [] if row.get("lane")}
        )  # fmt: skip
        rows = {row.get("lane"): row for row in result.get("lanes") or []}
        out = []
        for lane, _ in self.patches:
            row = rows.get(lane) or {}
            if lane in refused:
                out.append({"lane": lane, "status": "refused", **refused[lane]})
            elif row.get("status") == "excluded":
                out.append({"lane": lane, "status": "refused", "key": "land.patch_excluded", "message": row["reason"]})
            elif sha:
                out.append(
                    {"lane": lane, "status": "shipped", "functions": (row.get("functions") or {}).get("names", [])}
                )
            else:
                out.append({"lane": lane, "status": "refused", "key": "ship.refused", "message": "the ship refused"})
        return out

    def finish(
        self, result: dict[str, Any], findings: list[dict[str, Any]], base: str, sha: str | None, started: float
    ) -> Result:
        lanes = [lane for lane, _ in self.patches]
        findings = [*self.notes, *findings]
        names = sorted({n for row in result.get("lanes", []) for n in (row.get("functions") or {}).get("names", [])})
        refusal = next((f for f in findings if f.get("blocking")), None)
        inputs = self.inputs(result, sha)
        shipped_lanes = [row["lane"] for row in inputs if row["status"] == "shipped"]
        shipped = {
            "lane": ", ".join(lanes),
            "inputs": inputs,
            "base": base,
            "pushed": sha,
            "functions": names,
            "seconds": round(self.clock() - started, 3),
            "steps": self.steps,
            "refused": None if sha else (refusal or {"key": "ship.refused", "message": "the land refused the inputs"}),
            "land": result,
        }
        lost = [row for row in inputs if row["status"] == "refused"]
        record = {
            "actor": land.settings(self.profile)["actor"],
            "status": "passed" if sha else "failed",
            "message": (
                f"shipped {', '.join(shipped_lanes)} as {sha[:12]} to {self.remote}/{self.branch} in "
                f"{shipped['seconds']:.0f}s"
                + (f": {', '.join(names)}" if names else "")
                + (f"; refused {', '.join(f'{r["lane"]} ({r["key"]})' for r in lost)}" if lost else "")
                if sha
                else f"ship refused {', '.join(lanes)}: {shipped['refused']['key']}"
            ),
            "kind": "LAND",
            "subject": {"repo": str(self.tree), "ref": sha or base},
            "payload": {
                "op": "shipped",
                "lane": ", ".join(lanes),
                "inputs": [{"lane": r["lane"], "status": r["status"], "key": r.get("key")} for r in inputs],
                "functions": names,
                "base": base,
                "pushed": sha,
            },
        }
        try:
            self.append(events.events_path(self.profile), record)
        except DecompError as exc:
            findings.append({**exc.finding(), "blocking": False})
        if sha:  # what refused an input is that input's outcome (`inputs`), not the ship's
            findings = [{**f, "blocking": False} if f.get("key") in INPUT_REFUSALS else f for f in findings]
        return shipped, int(sha is None), findings


def ship(profile: Profile, tree: Path, patches: Sequence[tuple[str, Path]], **hooks: Any) -> Result:
    return Ship(profile, tree, patches, **hooks).execute()
