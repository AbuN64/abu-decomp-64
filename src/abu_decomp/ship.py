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
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from unbake import build as unbake_build
from unbake import treelock

from abu_decomp import DecompError, claims, events, land, meter, nextcmd, priority, shipproof, tools
from abu_decomp.config import Profile
from abu_decomp.ship_batch import Batch, restrict

Result = tuple[dict[str, Any], int, list[dict[str, Any]]]
IDENTITY = re.compile(r"^\s*(?P<name>[^<>]+?)\s*<(?P<email>[^<>\s]+)>\s*$")
LOCK_WAIT_SECONDS = 7200.0
WAIT_BEAT = 15.0  # seconds between the lines a waiting ship prints
# The findings that refuse one input of a ship that pushed the others: blocking for that input only.
INPUT_REFUSALS = frozenset({"land.file_refused", "land.input_build_failed", "land.function_split", *land.REFUSAL_KEYS})
PRIORITY = "UNBAKE_PRIORITY"  # unbake's pool: a command whose environment says "1" takes host slots first


# Findings one input alone can cause: that input is refused and the others still land.
PER_INPUT = ("land.evidence", "land.arity")


class Adopted(Exception):
    """Another ship of the queue landed this ship's inputs in its round; `result` is this ship's share."""

    def __init__(self, result: Any) -> None:
        super().__init__("adopted")
        self.result = result


class ShipError(DecompError):
    pass


def _parked_line(row: dict[str, Any]) -> str:
    """The one line a parked-change notice prints (Recipe prints the same rows from `.unbake/ship-parked.json`)."""
    who = ", ".join(row["owners"]) or "owner unknown (no live lease names them)"
    return (
        f"ship.parked: {' '.join(row['files'])} set aside by a ship at {row['time']} no longer apply; owners: {who}; "
        f"git apply --3way {row['patch']} then delete it"
    )


def _changed(asm: Path, kept: Path) -> dict[str, set[str]]:
    """Per version, the extracted files (relative to asm/<v>/) that differ between `asm` and `kept`, or are in one only;
    a file the land's extraction left alone is still the kept one's hard link."""
    out: dict[str, set[str]] = {}
    for top in {p.name for p in (*asm.iterdir(), *kept.iterdir()) if p.is_dir()}:
        mine = {p.relative_to(asm / top): p for p in (asm / top).rglob("*") if p.is_file()}
        theirs = {p.relative_to(kept / top): p for p in (kept / top).rglob("*") if p.is_file()}
        names = {str(rel) for rel in mine.keys() ^ theirs.keys()}
        for rel in mine.keys() & theirs.keys():
            a, b = mine[rel].stat(), theirs[rel].stat()
            if a.st_ino != b.st_ino and (a.st_size != b.st_size or mine[rel].read_bytes() != theirs[rel].read_bytes()):
                names.add(str(rel))
        names.discard(".extracted")  # the mark: what changed is handled file by file
        if names:
            out[top] = names
    return out


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


def _starved(pid: str, tree_lock: Path) -> bool:
    """Whether process `pid` has the tree lock file open yet holds no flock on it: it waits for the tree lock (the
    hierarchy is ship lock, tree lock, journal lock, so a journal holder that waits for the tree is a deadlock)."""
    try:
        inode = tree_lock.stat().st_ino
        opened = any(
            Path(f"/proc/{int(pid)}/fd/{fd}").stat().st_ino == inode
            for fd in os.listdir(f"/proc/{int(pid)}/fd")
            if os.path.exists(f"/proc/{int(pid)}/fd/{fd}")
        )
        rows = Path("/proc/locks").read_text().splitlines()
    except (OSError, ValueError):
        return False
    held = any(f" {pid} " in row and f":{inode} " in row and "->" not in row for row in rows)
    return opened and not held


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
        build: land.Run = land.build_process,
        proof: bool = True,
        prove: Callable[..., dict[str, Any]] = shipproof.prove,
        regen: bool = False,
        regen_min: int = 6,
        regen_age: float = 900.0,
        regen_now: bool = False,
        spawn: Callable[[], None] | None = None,
    ) -> None:
        self.proof, self.prove, self.spawn, self.build = proof, prove, spawn, build
        self.regen, self.regen_min, self.regen_age, self.regen_now = regen, regen_min, regen_age, regen_now
        self.records: dict[str, dict[str, Any]] = {}  # lane -> its proof record (shipproof), when it has one
        self.carry, self.kept = False, None  # this round builds nothing / the extraction kept for a refused land
        self.landed: dict[str, str] = {}  # lane -> its commit
        self.held = 0.0  # when this ship took the lock
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
        if self.regen and (self.patches or self.worktree or self.from_try):
            raise ShipError("ship.arguments", "--regen takes no input")
        if not (self.patches or self.worktree or self.from_try or self.regen):
            raise ShipError("ship.arguments", "ship needs at least one --patch, --worktree NAME or --from-try F")
        if len({lane for lane, _ in self.patches}) != len(self.patches):
            raise ShipError("ship.arguments", "two --patch inputs carry one lane name; each input needs its own")
        table = land.settings(profile, tree)
        self.remote, self.branch, self.author = table["remote"], table["branch"], table["author"]
        self.weight, self.cgroup, self.notes = table["lock_weight"], cgroup, []
        self.run, self.landing, self.append, self.clock, self.lock_wait = run, landing, append, clock, lock_wait
        self.steps: list[dict[str, Any]] = []
        self.cpu_mark = self.cpu_start = meter.cpu()
        self.refused: list[dict[str, Any]] = []  # inputs the ship refused whole before the land that pushed
        self.batch: Batch | None = None  # the queue's shared inputs (--patch ships only)
        self.own = [lane for lane, _ in self.patches]  # this ship's lanes; the round may carry others'

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
        batch = self.batch = Batch(tickets.parent, mine.name) if self.patches and not self.worktree else None
        if batch and not self.from_try:
            batch.publish(self.patches, self.messages)
        handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        began, said = self.clock(), None
        try:
            while True:
                state, got = batch.poll() if batch else ("queued", None)
                if state == "done":
                    raise Adopted(got)
                if state == "claimed":  # a ship ahead of us lands our input in its round: wait for its result
                    if said is None or self.clock() - said >= WAIT_BEAT:
                        said = self.clock()
                        sys.stderr.write(
                            f"decomp: ship pid {got} lands this input in its round ({said - began:.0f}s)\n"
                        )
                    time.sleep(1)
                    continue
                if self.first(tickets, mine) and _flocked(handle):
                    self.adopt()
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
            if batch:
                batch.withdraw()

    def adopt(self) -> None:
        """The inputs of the ships queued behind this one land in this round (one commit per input)."""
        if self.batch is None or self.from_try:
            return
        rows = self.batch.adopt({lane for lane, _ in self.patches}, self.accepts())
        self.patches += rows
        self.messages.update(self.batch.messages)
        if rows:
            lanes = ", ".join(lane for lane, _ in rows)
            self.steps.append({"step": "batch", "seconds": 0.0, "adopted": [lane for lane, _ in rows]})
            sys.stderr.write(f"decomp: landing the queued inputs of {lanes} in this round\n")

    def proven(self, rows: Sequence[tuple[str, Path]]) -> list[dict[str, Any]] | None:
        """The passing proof records of `rows` (built here or stored by their own ship), None if any input has none."""
        found = []
        for lane, path in rows:
            try:
                record = self.records.get(lane) or shipproof.load(self.common(), shipproof.patch_hash(path))
            except OSError:
                return None
            if not record or not record.get("pass"):
                return None
            found.append(record)
        return found

    def accepts(self) -> Callable[[list[tuple[str, Path]]], bool] | None:
        """Which queued inputs may join this round (10-22: an unproven input in a batch of 8 broke the build and the
        bisect held the lock 58 min): none unless this ship's own inputs carry passing proofs, and then only inputs
        with their own passing proof whose fact-file rows (name or address) clash with no input already in the round.
        While the round carries over, an input must also hold on the branch tip."""
        if self.worktree or self.from_try:
            return None
        own = self.proven(self.patches) if self.proof else None
        if own is None:
            return lambda rows: False
        try:
            tip = self.peek()
        except DecompError:
            return lambda rows: False
        carrying = self.carry_mode(tip, self.patches)
        taken = {row for record in own for ids in record["rows"].values() for row in ids}

        def accept(rows: list[tuple[str, Path]]) -> bool:
            records = self.proven(rows)
            if records is None or (carrying and not self.carry_mode(tip, rows)):
                return False
            ids = {row for record in records for found in record["rows"].values() for row in found}
            if ids & taken:
                return False
            taken.update(ids)
            return True

        return accept

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

    def peek(self) -> str:
        """The branch tip, fetched; the tree stays where it is."""
        self.git("fetch", "-q", self.remote, self.branch)
        return self.git("rev-parse", "--verify", "FETCH_HEAD^{commit}")[1].strip()

    def generated_globs(self) -> tuple[str, ...]:
        return tuple(land.settings(self.profile, self.tree)["regenerated_paths"])

    def proofs(self) -> dict[str, Any] | None:
        """Before the queue wait: every `--patch` input that has a work tree (`<tree>/../wt/<LANE>`) gets its proof
        record, built there in the lane's own time (shipproof.prove) and stored beside the lock. A patch whose proof
        build fails is refused here, at once, not after the queue. The refusing finding, None when all pass."""
        if not self.proof or self.worktree or self.from_try:
            return None
        common = self.common()
        for lane, path in self.patches:
            try:
                digest = shipproof.patch_hash(path)
            except OSError:
                continue  # a missing patch file is the land's refusal
            record = shipproof.load(common, digest)
            if record is None or not record["pass"]:
                if shipproof.worktree_of(self.tree, lane) is None:
                    continue
                began = self.clock()
                try:
                    record = self.prove(self.profile, self.tree, lane, path, run=self.run)
                except shipproof.NoProof as why:
                    self.notes.append({"key": why.key, "message": f"{lane}: {why.detail}", "blocking": False})
                    continue
                self.step("proof", began, lane=lane, ok=record["pass"], units=len(record["units"]))
                shipproof.store(common, record)
                if not record["pass"]:
                    broken = [v for v, row in record["roms"].items() if not row["ok"]]
                    why = f"ROM {', '.join(broken)} differs" if broken else "the build in the work tree failed"
                    return {
                        "key": "ship.proof_failed",
                        "message": f"lane {lane}: the proof build of this patch failed in its work tree ({why}); "
                        "`--no-proof` lands it with the full gates",
                        "blocking": True,
                        "lane": lane,
                        "tail": record.get("tail", ""),
                    }
            self.records[lane] = record
        return None

    def carry_mode(self, tip: str, rows: Sequence[tuple[str, Path]]) -> bool:
        """Whether every input of `rows` brings a proof that still holds on `tip` (shipproof.judge): such a round
        builds nothing. The reason of the first input that does not is kept in `self.carry_why`."""
        if not self.proof or self.worktree or self.from_try or not rows:
            return False
        common, generated = self.common(), self.generated_globs()
        for lane, path in rows:
            try:
                digest = shipproof.patch_hash(path)
            except OSError:
                self.carry_why = f"{lane}: patch file unreadable"
                return False
            ok, why = shipproof.judge(
                self.records.get(lane) or shipproof.load(common, digest),
                digest,
                tip,
                lambda *args: self.git(*args, ok=False),
                generated,
            )
            if not ok:
                self.carry_why = f"{lane}: {why}"
                return False
        return True

    carry_why = ""

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
        """Tracked changes that are no input of this ship never block it. A generated file (`unbake sync` output: the
        Makefile, a file whose HEAD copy carries the generated mark) is no one's work: it goes back to HEAD and the
        land's own sync writes it again. The rest (a lane's landing or fuzzy save left in the shared tree) is saved
        beside the ship lock and the tree checked out clean, under unbake's landing lock so no landing is caught half
        written; `unpark` puts it back once the ship is done."""
        with self.landing_lock():
            files = self.git("diff", "HEAD", "--name-only")[1].split()
            made = [f for f in files if self.generated(f)]
            kept = [f for f in files if f not in made]
            diff = self.git("diff", "HEAD", "--binary", "--full-index", "--", *kept)[1] if kept else ""
            path = self.common() / f"decomp-ship-parked-{time.time_ns()}.patch"
            if diff:
                path.write_text(diff)
            self.git("checkout", "-q", "--force", "--detach", "HEAD")  # tracked paths only; the patch holds them
        if made:
            sys.stderr.write(f"decomp: dropped {len(made)} generated file(s) unbake sync rewrites: {' '.join(made)}\n")
        if diff:
            self.parked.append((path, kept))
            sys.stderr.write(f"decomp: set aside {len(kept)} tracked change(s) of other work: {' '.join(kept)}\n")

    def generated(self, name: str) -> bool:
        head = self.git("show", f"HEAD:{name}", ok=False)
        return name == "Makefile" or (not head[0] and any(mark in head[1][:400] for mark in land.GENERATED_MARKS))

    def common(self) -> Path:
        common = Path(self.git("rev-parse", "--git-common-dir")[1].strip())
        return common if common.is_absolute() else self.tree / common

    def unpark(self) -> list[dict[str, Any]]:
        """The parked changes back on the tree as the ship leaves it, file by file. A file that no longer applies (the
        ship changed the same lines) stays in the patch: the finding and `.unbake/ship-parked.json` name it with the
        lanes whose leased items it touches, and every later ship and recipe command prints it until the patch is
        gone (`git apply PATCH` by its owner, then delete the patch)."""
        found = []
        with self.landing_lock():
            for path, files in self.parked:
                lost = [f for f in files if self.git("apply", "--binary", f"--include={f}", str(path), ok=False)[0]]
                if not lost:
                    path.unlink(missing_ok=True)
                    continue
                owners = self.owners(path.read_text(errors="replace"))
                row = {"patch": str(path), "files": lost, "owners": owners, "time": time.strftime("%H:%M")}
                notice = self.tree / ".unbake/ship-parked.json"
                rows = [*self.notices(), row]
                notice.write_text(json.dumps(rows))
                message = _parked_line(row)
                found.append({"key": "ship.parked", "message": message, "blocking": False})
                sys.stderr.write(f"decomp: {message}\n")
        self.parked = []
        return found

    def owners(self, text: str) -> list[str]:
        """`SESSION (item)` for each live recipe lease whose item the patch text names."""
        out = []
        for lease in sorted((self.tree / ".unbake/recipe/leases").glob("*.json")):
            with suppress(OSError, ValueError):
                row = json.loads(lease.read_text())
                if row.get("session") and re.search(rf"\b{re.escape(row['item'])}\b", text):
                    out.append(f"{row['session']} ({row['item']})")
        return out

    def notices(self) -> list[dict[str, Any]]:
        """The parked-change notices whose patch still exists (its owner has not applied and deleted it yet)."""
        try:
            rows = json.loads((self.tree / ".unbake/ship-parked.json").read_text())
        except (OSError, ValueError):
            return []
        return [r for r in rows if Path(r.get("patch", "")).is_file()]

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
                holder = os.pread(handle, 200, 0).decode(errors="replace").split()
                if holder and self.clock() - began >= 2 and _starved(holder[0], self.tree / ".unbake/tree.lock"):
                    time.sleep(1)  # twice, a second apart: a landing between two holds is not a wait
                    if _starved(holder[0], self.tree / ".unbake/tree.lock"):
                        raise ShipError(
                            "ship.deadlock",
                            f"ship pid {os.getpid()} holds the tree lock and waits for the journal lock held by "
                            f"{_process(holder[0])}, which waits for the tree lock: it took the journal first (the "
                            "order is ship, tree, journal)",
                            next=f"kill {holder[0]} (its submit retries after the ship)",
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
            self.landed[plan["lane"]] = self.git("rev-parse", "HEAD")[1].strip()
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
            more = {"carry": True} if self.carry else {}
            outcome = self.landing(self.profile, self.tree, base, inputs, messages=self.messages, **more)
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

    def precheck(self) -> dict[str, Any] | None:
        """The checks that need no tree and no turn, before the queue wait (10-22: a 75-character subject was refused
        only after 15 minutes in the queue): the commit subject limit and the claims an explicit message must cite.
        The refusing finding, None when the inputs pass."""
        table = land.settings(self.profile, self.tree)
        for lane, message in self.messages.items():
            subject = (message.splitlines() or [""])[0]
            path = next((p for name, p in self.patches if name == lane), None)
            if len(subject) > table["subject_limit"]:
                return {
                    "key": "land.commit_subject",
                    "message": f"lane {lane}: commit subject is {len(subject)} characters, the limit is "
                    f"{table['subject_limit']}; shorten the message",
                    "blocking": True,
                    "lane": lane,
                    "next": nextcmd.ship_command(
                        self.profile.source_file, self.tree, lane, message, path=path,
                        worktree=bool(self.worktree), limit=table["subject_limit"],
                    ),
                }  # fmt: skip
            if table.get("claims"):
                for problem in claims.check(self.profile, message)["problems"]:
                    return {
                        "key": "land.claim_uncited",
                        "message": f"lane {lane}: {claims.refusal_text(problem)}",
                        "blocking": True,
                        "lane": lane,
                    }
        return None

    def execute(self) -> Result:
        outcome = self.locked()
        self.after_lock(outcome)
        return outcome

    def locked(self) -> Result:
        started = self.clock()
        if refusal := self.precheck():
            lanes = ", ".join(lane for lane, _ in self.patches) or self.worktree or self.from_try or ""
            shipped = {"lane": lanes, "inputs": [], "base": None, "pushed": None, "functions": [], "seconds": 0.0,
                       "cpu": 0.0, "steps": [], "refused": refusal, "land": {}}  # fmt: skip
            return shipped, 1, [refusal]
        if refusal := self.proofs():  # the lane's own work tree proves the patch before it takes a turn
            lanes = ", ".join(lane for lane, _ in self.patches)
            shipped = {"lane": lanes, "inputs": [], "base": None, "pushed": None, "functions": [], "seconds":
                       round(self.clock() - started, 3), "cpu": 0.0, "steps": self.steps, "refused": refusal,
                       "land": {}}  # fmt: skip
            return shipped, 1, [refusal]
        try:
            handle = self.lock()
        except Adopted as adopted:
            shipped, code, findings = adopted.result
            return {**shipped, "seconds": round(self.clock() - started, 3)}, code, findings
        self.step("queue", started)  # the wait for the ticket and the ship lock, apart from the ship's own work
        self.held = self.clock()
        try:
            if self.regen:
                with treelock.hold(self.tree, exclusive=True, who="ship regen"):
                    regenerated = self.regenerate()
                    regenerated[2].extend(self.unpark())
                    return regenerated
            for row in self.notices():  # every ship says it until the owner takes the parked change back
                self.notes.append({"key": "ship.parked", "message": _parked_line(row), "blocking": False})
                sys.stderr.write(f"decomp: {_parked_line(row)}\n")
            # sync, check and commit rewrite tracked files and the extraction: readers of the tree (recipe try, work,
            # unbake probe) wait for the window, and their waits are the readers' to print
            with treelock.hold(self.tree, exclusive=True, who="ship"):
                # a round whose inputs all carry proofs that hold builds nothing: the extraction is not touched
                try:
                    self.carry = self.carry_mode(self.peek(), self.patches)
                except DecompError:
                    self.carry = False
                self.kept = None if self.carry else self.keep_extraction()
                try:
                    if self.from_try:
                        self.enter(self.from_try)
                    outcome = self.attempts(started)
                except BaseException:
                    self.abandon()
                    self.unwind(self.kept)
                    self.unpark()
                    raise
                if outcome[0].get("pushed"):
                    self.settle(outcome[0]["pushed"])
                    self.drop(self.kept)
                else:
                    self.abandon()
                    self.unwind(self.kept)
                    gates = [
                        g for a in (outcome[0].get("land") or {}).get("attempts") or () for g in a.get("gates", ())
                    ]
                    if any(g.get("gate") == "check" and not g.get("pass") for g in gates):  # the build itself refused
                        outcome[2].extend(self.health(outcome[0]["base"]))
                outcome[2].extend(self.unpark())
                if self.batch and self.batch.adopted:
                    self.batch.release(outcome)
                    outcome = restrict(outcome, set(self.own))
                return outcome
        finally:
            if self.batch:
                self.batch.release(None)  # anything not given a result goes back to the queue
            os.close(handle)

    def health(self, base: str) -> list[dict[str, Any]]:
        """After a refused build: does HEAD itself build on this warm tree? `unbake setup` first re-extracts what no
        longer matches its facts, then the same check runs at the base (incremental: the unwind kept every object the
        input did not reach). HEAD green: the refusal is the input's. HEAD red: the tree drifted from HEAD, so every
        object of each version the build names is dropped and the check runs once more; green then is a repaired tree
        (ship again), red still is a HEAD that does not build here (10-22: a drifted tree refused 75 min of inputs)."""
        started = self.clock()
        table = land.settings(self.profile, self.tree)
        self.invoke("unbake", self.profile, ["setup", "--no-make"], project=self.tree)
        jobs = None
        if table["jobs"]:
            code, out, _ = self.run(table["jobs"], self.tree, None, 60)
            jobs = int(out.strip()) if not code and out.strip().isdigit() else None
        env = {**os.environ, "UNBAKE_HOST": str(tools.host_file(self.profile))}
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")

        def build() -> tuple[int, str]:
            code, out, err = self.run(land.build_command(table["check"], jobs), self.tree, env, table["timeout"])
            return code, out + err

        code, text = build()
        found: list[dict[str, Any]] = []
        if code == 0:
            message = f"HEAD {base[:12]} builds on this tree: the refused build is the input's"
            found.append({"key": "ship.base_ok", "blocking": False, "message": message})
        else:
            versions = sorted(set(re.findall(r"build/([\w-]+)/", text))) or [
                p.name for p in (self.tree / "build").iterdir() if p.is_dir()
            ]
            for version in versions:
                for obj in (self.tree / "build" / version).rglob("*.o"):
                    obj.unlink()
            code, text = build()
            tail = "\n".join(text.strip().splitlines()[-8:])
            found.append(
                {"key": "ship.tree_repaired", "blocking": False, "versions": versions,
                 "message": f"HEAD {base[:12]} did not build on this tree (drift, not the input); "
                            f"{', '.join(versions)} rebuilt from clean and HEAD builds now: ship again"}
                if code == 0
                else {"key": "ship.tree_drift", "blocking": True, "versions": versions, "tail": tail,
                      "message": f"HEAD {base[:12]} does not build on this tree, even with {', '.join(versions)} "
                                 "rebuilt from clean: no input can land until it does"}
            )  # fmt: skip
        self.step("health", started, ok=code == 0)
        return found

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

    def unwind(self, kept: Path | None) -> None:
        """A ship that pushed nothing puts the extraction back and leaves make exactly what the land made stale. The
        tracked files git restores carry a fresh mtime already; each extracted file that differs from the kept one is
        touched (its own .s.o rebuilds), the C objects that include it are dropped (unbake's `drop_including`, the
        rule its own re-extraction follows) and the version's rendered linker scripts go. A changed top-level file
        (macro.inc, labels.inc) drops every object of its version. Everything else stays warm for the next ship (10-22:
        dropping every object a refused ship built made the next ship rebuild ~2000 units, 182 s CPU)."""
        if kept is None or not kept.is_dir():
            return
        asm = self.tree / "asm"
        changed = _changed(asm, kept)
        shutil.rmtree(asm, ignore_errors=True)
        kept.rename(asm)
        held = SimpleNamespace(project=SimpleNamespace(root=self.tree))
        for version, names in changed.items():
            for name in names:
                with suppress(FileNotFoundError):
                    os.utime(asm / version / name)
            if any("/" not in name for name in names):  # a top-level include: every object of the version
                for obj in (self.tree / "build" / version).rglob("*.o"):
                    obj.unlink()
            else:
                unbake_build.drop_including(held, version, names)
            for script in (self.tree / "build" / version).glob("*.ld"):
                script.unlink()

    def drop(self, kept: Path | None) -> None:
        if kept is not None:
            shutil.rmtree(kept, ignore_errors=True)

    def attempts(self, started: float) -> Result:
        for attempt in (1, 2):
            base = self.tip()
            self.carry = self.carry_mode(base, self.patches)
            if not self.carry and self.kept is None:
                self.kept = self.keep_extraction()  # the branch moved under a carried round: this one builds
            self.refused = []
            landing = self.clock()
            result, code, findings = self.land_all(base, list(self.patches))
            self.step("land", landing)  # setup, check, sync, audit: the land's gates carry their own wall and CPU
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
                self.stamp(base, result)
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
            "lock_seconds": round(self.clock() - self.held, 1) if self.held else 0.0,  # holding the ship lock
            "carried": self.carry,  # no build: the lanes' proofs held on the branch tip
            "cpu": round(meter.cpu() - self.cpu_start, 1),  # the ship's own and its children's, beside the wall
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
                f"{shipped['seconds']:.0f}s wall, {shipped['cpu']:.0f}s CPU"
                + (f": {', '.join(names)}" if names else "")
                + (f"; refused {', '.join(f'{r["lane"]} ({r["key"]})' for r in lost)}" if lost else "")
                if sha
                else f"ship refused {', '.join(lanes)}: {shipped['refused']['key']} after "
                f"{shipped['seconds']:.0f}s wall, {shipped['cpu']:.0f}s CPU"
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

    # proof records and the batch regeneration -------------------------------------------------------------------
    def stamp(self, base: str, result: dict[str, Any]) -> None:
        """Store the proof with each commit this ship pushed: `decomp-ship.landed/<sha>.json` and a git note under
        refs/notes/ship-proof (pushed after the lock is released). A commit of a full land carries the gates it ran."""
        folder = self.common() / shipproof.LANDED
        folder.mkdir(exist_ok=True)
        gates = [{k: g.get(k) for k in ("gate", "pass", "seconds")} for g in result.get("gates", [])]
        for lane, sha in self.landed.items():
            body = {
                "v": shipproof.VERSION,
                "lane": lane,
                "commit": sha,
                "base": base,
                "mode": "carried" if self.carry else "full",
                "proof": self.records.get(lane),
                "gates": gates,
                "lock_seconds": round(self.clock() - self.held, 1),
            }
            (folder / f"{sha}.json").write_text(json.dumps(body, default=str))
            note = {**body, "proof": body["proof"] and {k: v for k, v in body["proof"].items() if k != "units"}}
            code, text = shipproof.annotate(self.run, self.tree, sha, note, self.identity())
            if code:
                self.notes.append({"key": "ship.proof_note", "message": f"no note on {sha[:12]}: {text.strip()[-200:]}",
                                   "blocking": False})  # fmt: skip

    def after_lock(self, outcome: Result) -> None:
        """With the ship lock released: the notes go to the remote, and a carried push starts the batch regeneration
        of the generated files (README, maps, overview, progress) that no carried commit wrote."""
        if not outcome[0].get("pushed") or self.regen or not self.held:  # only the ship that held the lock
            return
        code, text = self.git("push", "-q", self.remote, f"refs/notes/{shipproof.NOTES}", ok=False)
        if code:
            sys.stderr.write(f"decomp: proof notes not pushed: {text.strip()[-200:]}\n")
        if outcome[0].get("carried"):
            with suppress(OSError, DecompError):
                (self.spawn or self.start_regen)()

    def start_regen(self) -> None:
        """`decomp ship --regen` detached (one at a time): it takes a ticket like any ship and regenerates when due."""
        from abu_decomp.ship_batch import alive

        pending = self.common() / "decomp-ship.regen.pending"
        with suppress(OSError, ValueError):
            if alive(int(pending.read_text())):
                return
        argv = [sys.executable, "-m", "abu_decomp", "--profile", str(self.profile.source_file),
                "ship", "--tree", str(self.tree), "--regen"]  # fmt: skip
        with (self.common() / "decomp-regen.log").open("a") as log:
            child = subprocess.Popen(
                argv, cwd=self.tree, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True
            )
        pending.write_text(str(child.pid))

    def regenerate(self) -> Result:
        """`--regen` (the ship lock and the tree lock held): once enough carried ships or enough time passed since the
        last regeneration, build the branch tip (the verifier: red is a finding), run `unbake sync --settle` and commit
        the generated files it changes in one commit. Not due, or nothing changed: no commit."""
        started = self.clock()
        common = self.common()
        mark_path, pending = common / "decomp-regen.json", common / "decomp-ship.regen.pending"
        try:
            mark = json.loads(mark_path.read_text())
        except (OSError, ValueError):
            mark = {}
        try:
            sha = self.tip()
            last = mark.get("sha")
            span = f"{last}..{sha}" if last and self.git("cat-file", "-e", last, ok=False)[0] == 0 else sha
            subjects = self.git("log", "--format=%s", span)[1].splitlines()
            count = len([s for s in subjects if not s.startswith("Regenerate ")])
            age = time.time() - mark.get("time", 0.0)
            due = self.regen_now or (count > 0 and (count >= self.regen_min or age >= self.regen_age))
            report: dict[str, Any] = {"due": due, "pending": count, "tip": sha}
            if not due:
                return {"lane": "regen", "pushed": None, "regen": report, "steps": self.steps}, 0, self.notes
            table = land.settings(self.profile, self.tree)
            began = self.clock()
            self.invoke("unbake", self.profile, ["setup", "--no-make"], project=self.tree)
            jobs = None
            if table["jobs"]:
                code, out, _ = self.run(table["jobs"], self.tree, None, 60)
                jobs = int(out.strip()) if not code and out.strip().isdigit() else None
            env = {**os.environ, "UNBAKE_HOST": str(tools.host_file(self.profile))}
            env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
            code, out, err = self.build(land.build_command(table["check"], jobs), self.tree, env, table["timeout"])
            verdict = self.invoke("unbake", self.profile, ["verify", str(code)], project=self.tree)
            self.step("regen_build", began, ok=code == 0)
            red = common / "decomp-ship.red.json"
            if code or not verdict.ok:
                tail = "\n".join((out + err).strip().splitlines()[-12:])
                red.write_text(json.dumps({"sha": sha, "time": time.time(), "tail": tail}))
                self.clean(sha)
                finding = {"key": "ship.main_red", "blocking": True, "tail": tail,
                           "message": f"{self.remote}/{self.branch} {sha[:12]} does not build all five ROMs "
                                      f"({red}): a forward fix on main"}  # fmt: skip
                return {"lane": "regen", "pushed": None, "regen": report, "steps": self.steps}, 1, [finding]
            red.unlink(missing_ok=True)
            began = self.clock()
            called = self.invoke("unbake", self.profile, ["sync", "--settle"], project=self.tree)
            self.step("regen_sync", began, ok=bool(called.ok))
            if not called.ok:
                self.clean(sha)
                found = [{"key": "ship.regen_sync", "blocking": True, "message": "unbake sync --settle refused"}]
                return {"lane": "regen", "pushed": None, "regen": report, "steps": self.steps}, 1, found
            result = called.document.get("result") if isinstance(called.document, dict) else None
            written = {w for w in (result or {}).get("written", []) if isinstance(w, str)}
            names = set(self.git("diff", "HEAD", "--name-only", "--no-renames")[1].splitlines())
            names |= written & set(self.git("ls-files", "--others", "--exclude-standard")[1].splitlines())
            files = sorted(names)
            pushed = None
            if files:
                self.git("update-index", "--add", "--remove", "--", *files)
                self.git("commit", "-q", "-m", f"Regenerate maps and progress after {count} ships", "--", *files,
                         env=self.identity())  # fmt: skip
                pushed = self.git("rev-parse", "HEAD")[1].strip()
                if not self.push():
                    self.clean(sha)
                    pushed = None
                    moved = "the branch moved during the regeneration; the next one redoes it"
                    self.notes.append({"key": "ship.moved", "blocking": False, "message": moved})
                    return {"lane": "regen", "pushed": None, "regen": report, "steps": self.steps}, 0, self.notes
            mark_path.write_text(json.dumps({"sha": pushed or sha, "time": time.time()}))
            report["files"] = files
            return {"lane": "regen", "pushed": pushed, "regen": report, "steps": self.steps,
                    "seconds": round(self.clock() - started, 1)}, 0, self.notes  # fmt: skip
        finally:
            with suppress(OSError, ValueError):
                if int(pending.read_text()) == os.getpid():
                    pending.unlink()


def ship(profile: Profile, tree: Path, patches: Sequence[tuple[str, Path]], **hooks: Any) -> Result:
    return Ship(profile, tree, patches, **hooks).execute()
