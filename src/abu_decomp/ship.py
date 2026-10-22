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
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from unbake import treelock

from abu_decomp import DecompError, events, land, meter, nextcmd, priority, tools
from abu_decomp.config import Profile

Result = tuple[dict[str, Any], int, list[dict[str, Any]]]
IDENTITY = re.compile(r"^\s*(?P<name>[^<>]+?)\s*<(?P<email>[^<>\s]+)>\s*$")
LOCK_WAIT_SECONDS = 7200.0
WAIT_BEAT = 15.0  # seconds between the lines a waiting ship prints
# The findings that refuse one input of a ship that pushed the others: blocking for that input only.
INPUT_REFUSALS = frozenset({"land.file_refused", "land.input_build_failed", "land.function_split", *land.REFUSAL_KEYS})
PRIORITY = "UNBAKE_PRIORITY"  # unbake's pool: a command whose environment says "1" takes host slots first


# Findings one input alone can cause: that input is refused and the others still land.
PER_INPUT = ("land.evidence", "land.arity")


class ShipError(DecompError):
    pass


def _flocked(handle: int) -> bool:
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _process(pid: str) -> str:
    """`pid N: its command line` (cut short), for a line that says what a wait waits on."""
    try:
        words = Path(f"/proc/{int(pid)}/cmdline").read_bytes().split(b"\0")
    except (OSError, ValueError):
        return f"pid {pid}"
    return f"pid {pid}: " + " ".join(w.decode(errors="replace") for w in words if w)[:160]


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
        worktree: str | None = None,
        from_try: str | None = None,
        invoke: Callable[..., Any] = tools.invoke,
    ) -> None:
        self.profile, self.patches, self.messages = profile, list(patches), dict(messages or {})
        self.from_try, self.invoke = from_try, invoke  # from_try: the function whose proven candidate is the input
        self.worktree: str | None = worktree  # the work tree's own tracked changes are the input (see capture)
        self.saved: dict[str, Any] | None = None
        self.parked: list[tuple[Path, list[str]]] = []  # other work's tracked changes, set aside for the ship (park)
        self.tree = tree.expanduser().resolve()
        if not (self.tree / ".git").exists():
            raise ShipError("ship.tree", f"{self.tree} is not a git checkout")
        if sum(map(bool, (self.worktree, self.patches, self.from_try))) > 1:
            raise ShipError("ship.arguments", "--patch, --worktree and --from-try are each the input; give one")
        if not (self.patches or self.worktree or self.from_try):
            raise ShipError("ship.arguments", "ship needs at least one --patch, --worktree NAME or --from-try F")
        if len({lane for lane, _ in self.patches}) != len(self.patches):
            raise ShipError("ship.arguments", "two --patch inputs carry one lane name; each input needs its own")
        table = land.settings(profile, tree)
        self.remote, self.branch, self.author = table["remote"], table["branch"], table["author"]
        self.weight, self.cgroup, self.notes = table["lock_weight"], cgroup, []
        self.run, self.landing, self.append, self.clock, self.lock_wait = run, landing, append, clock, lock_wait
        self.steps: list[dict[str, Any]] = []
        self.cpu_mark = meter.cpu()
        self.refused: list[dict[str, Any]] = []  # inputs the ship refused whole before the land that pushed

    # git ----------------------------------------------------------------------------------------------------------
    def git(self, *args: str, ok: bool = True, env: dict[str, str] | None = None) -> tuple[int, str]:
        code, out, err = self.run(["git", *args], self.tree, {**os.environ, **env} if env else None, 600)
        if code and ok:
            raise ShipError("ship.git", f"git {' '.join(args[:3])}: {(err or out).strip()[-400:]}")
        return code, (out if not code else (err or out))

    def step(self, name: str, started: float, **detail: Any) -> None:
        now = meter.cpu()
        spent, self.cpu_mark = now - self.cpu_mark, now  # descendants' CPU since the previous step, beside its wall
        self.steps.append({"step": name, "seconds": round(self.clock() - started, 3), "cpu": round(spent, 2), **detail})

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
        """The repository's ship lock (in git's common dir, so every worktree of one clone shares it), served in
        arrival order: flock is not FIFO, so each ship first takes a ticket (`decomp-ship.tickets/<ns>-<pid>`) and
        only the oldest live ticket may take the lock."""
        common = Path(self.git("rev-parse", "--git-common-dir")[1].strip())
        path = (common if common.is_absolute() else self.tree / common) / "decomp-ship.lock"
        tickets = path.with_name("decomp-ship.tickets")
        tickets.mkdir(exist_ok=True)
        mine = tickets / f"{time.time_ns():020d}-{os.getpid()}"
        mine.touch()
        handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        began, said = self.clock(), None
        try:
            while True:
                if self.first(tickets, mine) and _flocked(handle):
                    self.boost()
                    return handle
                if self.clock() - began > self.lock_wait:
                    raise ShipError("ship.busy", f"another ship held {path} for {self.lock_wait:g}s")
                if said is None or self.clock() - said >= WAIT_BEAT:
                    said = self.clock()
                    ahead = [t for t in sorted(tickets.iterdir()) if t < mine]
                    who = _process(ahead[0].name.rpartition("-")[2]) if ahead else "the ship holding the lock"
                    sys.stderr.write(
                        f"decomp: waiting for the ship lock, {len(ahead)} ship(s) ahead; first: {who}"
                        f" ({said - began:.0f}s)\n"
                    )
                time.sleep(2)
        except BaseException:
            os.close(handle)
            raise
        finally:
            mine.unlink(missing_ok=True)

    @staticmethod
    def first(tickets: Path, mine: Path) -> bool:
        """Whether `mine` is the oldest ticket of a live ship (a dead ship's ticket is removed)."""
        for ticket in sorted(tickets.iterdir()):
            if ticket == mine:
                return True
            try:
                os.kill(int(ticket.name.rpartition("-")[2]), 0)
                return False
            except ProcessLookupError:
                ticket.unlink(missing_ok=True)
            except (PermissionError, ValueError):
                return False
        return True

    def boost(self) -> None:
        """The holder of the ship lock is the job every lane's next tree waits on: its own systemd unit (scope or
        service, from /proc/self/cgroup) gets `land.lock_weight` CPU and IO weight against the lane units beside it
        for as long as it runs. Whatever stops that is a printed finding, never a silent pass."""
        why, unit = "", None
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
            # configured, but this process is in no unit the user manager owns: a note in the result, no stderr line
            if not (why.startswith("this process runs in no systemd") or "not found" in why):
                sys.stderr.write(f"decomp: {message}\n")
            self.notes.append({"key": "ship.boost", "message": message, "blocking": False})

    def tip(self) -> str:
        """Fetch the branch and move the tree to its tip: only the files that differ are rewritten, so the build
        state stays warm. A tree with tracked changes is refused, never reset over."""
        started = self.clock()
        self.git("fetch", "-q", self.remote, self.branch)
        sha = self.git("rev-parse", "--verify", "FETCH_HEAD^{commit}")[1].strip()  # a remote name, URL or path
        dirty = self.git("status", "--porcelain", "--untracked-files=no")[1].splitlines()
        if dirty and self.worktree and self.saved is None:
            self.capture(sha)
        elif dirty:
            self.park()
        if self.git("rev-parse", "HEAD")[1].strip() != sha:
            self.git("checkout", "-q", "--detach", sha)
        self.step("tip", started, sha=sha)
        return sha

    def capture(self, sha: str) -> None:
        """The work tree's tracked changes become the input patch, saved beside the repository's ship lock. The tree is
        then moved to `sha` clean (only tracked paths are rewritten; ignored build state and untracked files stay) and
        the patch must apply there; if it does not, the tree is put back exactly and the ship is refused."""
        diff = self.git("diff", "HEAD", "--binary", "--full-index")[1]
        head = self.git("rev-parse", "HEAD")[1].strip()
        code, ref = self.git("symbolic-ref", "-q", "HEAD", ok=False)
        common = Path(self.git("rev-parse", "--git-common-dir")[1].strip())
        path = (common if common.is_absolute() else self.tree / common) / f"decomp-ship-worktree-{self.worktree}.patch"
        path.write_text(diff)
        count = len(self.git("diff", "HEAD", "--name-only")[1].splitlines())
        self.saved = {"diff": diff, "head": head, "branch": "" if code else ref.strip(), "path": path}
        self.git("checkout", "-q", "--force", "--detach", head)  # drops tracked changes only; the patch holds them
        self.git("checkout", "-q", "--detach", sha)
        code, text = self.git("apply", "--check", str(path), ok=False)
        if code:
            self.restore()
            raise ShipError(
                "ship.worktree_conflict",
                f"the work tree's {count} changed file(s) do not apply on {sha[:12]} ({text.strip()[-300:]}); "
                "the tree is unchanged",
                next=f"git -C {self.tree} log -5 --oneline FETCH_HEAD",
            )
        self.patches = [(self.worktree or "worktree", path)]

    def park(self) -> None:
        """Tracked changes that are no input of this ship (a lane's landing or fuzzy save left in the shared tree) never
        block it: saved beside the ship lock and the tree checked out clean, under unbake's landing lock so no landing
        is caught half written; `unpark` puts them back once the ship is done."""
        with self.landing_lock():
            diff = self.git("diff", "HEAD", "--binary", "--full-index")[1]
            files = self.git("diff", "HEAD", "--name-only")[1].split()
            if not diff:
                return
            common = Path(self.git("rev-parse", "--git-common-dir")[1].strip())
            path = (
                common if common.is_absolute() else self.tree / common
            ) / f"decomp-ship-parked-{time.time_ns()}.patch"
            path.write_text(diff)
            self.git("checkout", "-q", "--force", "--detach", "HEAD")  # tracked paths only; the patch holds them
        self.parked.append((path, files))
        sys.stderr.write(
            f"decomp: set aside {len(files)} tracked change(s) of other work for this ship: {' '.join(files)}\n"
        )

    def unpark(self) -> list[dict[str, Any]]:
        """The parked changes back on the tree as the ship leaves it; one that no longer applies (the ship changed the
        same lines) stays in its patch file, named in a finding."""
        found = []
        with self.landing_lock():
            for path, files in self.parked:
                code, text = self.git("apply", "--binary", str(path), ok=False)
                if code:
                    message = f"set-aside changes of {' '.join(files)} do not apply on the shipped tree: kept in {path}"
                    found.append(
                        {"key": "ship.parked", "message": f"{message} ({text.strip()[-200:]})", "blocking": False}
                    )
                    sys.stderr.write(f"decomp: {message}\n")
                else:
                    path.unlink(missing_ok=True)
        self.parked = []
        return found

    @contextmanager
    def landing_lock(self) -> Iterator[None]:
        """unbake's landing (journal) lock of the tree: a landing writes, builds and restores its files under it."""
        path = self.tree / ".unbake" / "journal.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        began, said = self.clock(), None
        try:
            while not _flocked(handle):
                if said is None or self.clock() - said >= WAIT_BEAT:
                    said, holder = self.clock(), os.pread(handle, 200, 0).decode(errors="replace").split()
                    who = _process(holder[0]) if holder else "another process"
                    sys.stderr.write(
                        f"decomp: ship waits for an unbake landing to finish writing the tree: {who}"
                        f" ({said - began:.0f}s)\n"
                    )
                time.sleep(0.5)
            yield
        finally:
            os.close(handle)

    def restore(self) -> None:
        """Put the work tree back as the ship found it: its commit (and branch), then its captured changes."""
        saved = self.saved
        if not saved:
            return
        self.git("clean", "-q", "-f", "-d", ok=False)
        name = saved["branch"].removeprefix("refs/heads/")
        self.git("checkout", "-q", "--force", name or saved["head"])
        if saved["diff"]:
            self.git("apply", "--binary", str(saved["path"]))
        if self.git("diff", "HEAD", "--binary", "--full-index")[1] != saved["diff"]:
            raise ShipError("ship.worktree_restore", f"the work tree does not match its capture {saved['path']}")
        self.saved = None

    def enter(self, name: str) -> None:
        """`--from-try F`: the candidate `recipe try F` proved exact becomes the tree's tracked change, written by
        `unbake submit` at the branch tip (F out of its NON_MATCHING fence or its INCLUDE_ASM replaced, its own pool
        rows brought, plus the header, alias or config files under work/F.overlay/ at their project paths); the ship
        then takes it as a --worktree input, so no lane writes a diff."""
        self.tip()
        file = self.tree / ".unbake/recipe/work" / f"{name}.c"
        if not file.is_file():
            raise ShipError(
                "ship.from_try", f"{file} does not exist: no candidate for {name}", next=f"recipe work {name}"
            )
        overlay = self.tree / ".unbake/recipe/work" / f"{name}.overlay"  # companions: project path -> its new text
        extras = [f"{p.relative_to(overlay)}={p}" for p in sorted(overlay.rglob("*")) if p.is_file()]
        called = self.invoke("unbake", self.profile, ["submit", str(file), *extras], project=self.tree)
        result = called.document.get("result") or {}
        rows = result.get("submissions") or [{}]
        refused = (result.get("drain") or {}).get("refused") or []
        found = [*(rows[0].get("findings") or []), *(called.document.get("findings") or []), *refused]
        if not called.ok or rows[0].get("operation") != "publish" or refused or not self.git("diff", "HEAD")[1]:
            why = next((str(f.get("message") or f.get("key")) for f in found if isinstance(f, dict)), "not exact")
            raise ShipError("ship.from_try", f"unbake submit refused {name}: {why}", next=f"recipe try {name}")
        self.worktree = name

    def abandon(self) -> None:
        """A ship that pushed nothing puts a --worktree input back; a --from-try input was never the lane's own
        work, so the shared tree goes back to the branch clean instead of staying dirty."""
        self.restore()
        if self.from_try:
            self.clean(self.git("rev-parse", "HEAD")[1].strip())

    def settle(self, sha: str) -> None:
        """After a push the work tree is clean at the new commit; a tree that sat on a branch moves it there."""
        saved, self.saved = self.saved, None
        if saved and saved["branch"] and self.git("merge-base", "--is-ancestor", saved["head"], sha, ok=False)[0] == 0:
            self.git("checkout", "-q", "-B", saved["branch"].removeprefix("refs/heads/"), sha)

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
            result, code, findings = outcome
            split = [f for f in findings if f.get("key") == "land.file_refused"]
            if code and not split:
                split = self.evidence_refusals(result, findings)  # only the input whose function the gate refused
            if not split or (code and not any(f.get("key") in PER_INPUT for f in split)):
                return outcome
            self.clean(base)
            lanes = {f.get("lane") for f in split}
            kept = [row for row in inputs if row[0] not in lanes]
            if not kept or len(kept) == len(inputs):  # nothing left, or a refusal no input owns: nothing lands
                return {**outcome[0], "pass": False}, 1, findings
            for lane in sorted(lane for lane in lanes if lane):
                reason = "; ".join(f["message"] for f in split if f.get("lane") == lane)
                key = next(f["key"] for f in split if f.get("lane") == lane)
                self.refused.append({"lane": lane, "key": key, "message": reason})
                self.notes.append({"key": "ship.input_refused", "message": f"{lane}: {reason}", "blocking": False})
            inputs = kept

    def evidence_refusals(self, result: dict[str, Any], findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The evidence gate's refusals, each tagged with the lane(s) whose files carry the refused function."""
        owners: dict[str, list[str]] = {}
        for row in result.get("lanes") or []:
            for item in row.get("files") or []:
                owners.setdefault(item if isinstance(item, str) else str(item.get("file", "")), []).append(row["lane"])
        found = []
        for finding in findings:
            if finding.get("key") in PER_INPUT and owners.get(finding.get("file", "")):
                found += [{**finding, "lane": lane} for lane in owners[finding["file"]]]
        blocking = [f for f in findings if f.get("blocking") and f.get("key") not in PER_INPUT]
        return [] if blocking else found

    def execute(self) -> Result:
        started = self.clock()
        handle = self.lock()
        try:
            # sync, check and commit rewrite tracked files and the extraction: readers of the tree (recipe try, work,
            # unbake probe) wait for the window, and their waits are the readers' to print
            with treelock.hold(self.tree, exclusive=True, who="ship"):
                kept = self.keep_extraction()
                try:
                    if self.from_try:
                        self.enter(self.from_try)
                    outcome = self.attempts(started)
                except BaseException:
                    self.abandon()
                    self.unwind(kept, started)
                    self.unpark()
                    raise
                if outcome[0].get("pushed"):
                    self.settle(outcome[0]["pushed"])
                    self.drop(kept)
                else:
                    self.abandon()
                    self.unwind(kept, started)
                outcome[2].extend(self.unpark())
                return outcome
        finally:
            os.close(handle)

    def keep_extraction(self) -> Path | None:
        """The extraction under asm/ as the ship found it, kept as hard links: a land's sync re-extracts from the
        patched symbols, and a refused land leaves that extraction (and objects built from it) under HEAD's symbols."""
        kept = self.tree / ".unbake/ship-asm"  # under .unbake (git-ignored): never an untracked .s of the tree
        shutil.rmtree(self.tree / "asm.pre-ship", ignore_errors=True)  # where an earlier ship version kept it
        shutil.rmtree(kept, ignore_errors=True)  # a killed ship's leftover
        if not (self.tree / "asm").is_dir():
            return None
        kept.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["cp", "-al", str(self.tree / "asm"), str(kept)], check=True)
        return kept

    def unwind(self, kept: Path | None, started: float) -> None:
        """A ship that pushed nothing puts the extraction back and removes every object and assembly the land built
        (newer than the ship's start, so built under its candidate Makefile and extraction): the next make rebuilds."""
        if kept is None or not kept.is_dir():
            return
        shutil.rmtree(self.tree / "asm", ignore_errors=True)
        kept.rename(self.tree / "asm")
        since = time.time() - (self.clock() - started)
        for path in (self.tree / "build").rglob("*"):
            if path.suffix in (".o", ".s", ".prev", ".textsize") and path.is_file() and path.stat().st_mtime >= since:
                path.unlink()

    def drop(self, kept: Path | None) -> None:
        if kept is not None:
            shutil.rmtree(kept, ignore_errors=True)

    def attempts(self, started: float) -> Result:
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
        if self.worktree:  # a refusal of a --worktree ship names the --worktree form
            findings = [
                {**f, "next": nextcmd.as_worktree(f["next"], self.worktree)} if f.get("next") else f for f in findings
            ]
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
            "actor": land.settings(self.profile, self.tree)["actor"],
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
