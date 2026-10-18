"""`decomp land`: apply lane patches to a proof tree and run the landing gates; it never commits or pushes.

unbake owns the objective rules (setup, sync, the sync check, source audit) and `unbake land` lands inbox
submissions, which is a different job; this command only orders those public calls (through `decomp unbake`'s
isolated invoke, so the pinned tool runs) around the project's own `check` build and adds the gates that need git.

Gates, in order; the first failure stops the run and the rest are reported as skipped:
  base         the tree is clean (tracked files) and HEAD is --base
  apply        each patch applies with `git apply --3way --index`; one that fails is excluded (the tree is put back to
               the state before it) and reported with git's reason; no patch applying at all fails the gate
  setup        `unbake setup` when the pinned unbake commit differs from the tree's last setup (a marker in the git dir)
  sync         `unbake sync`
  check        the configured check command with jobs from the configured admit command
  sync_after   `unbake sync` again, after check
  sync_check   `unbake sync --check`
  markers      no marker line (INCLUDE_ASM, NON_MATCHING, ...) added in a file no applied patch touched
  untracked_s  no untracked assembly file (every one a patch or sync produced is tracked or staged)
  readme       no code percentage in the README lower than at the base

Section `land` of the seat profile:

    "land": {
      "check": ["make", "check"],                     required; run in the tree, `-j N` appended when `jobs` is set
      "jobs": ["/path/admit", "8"],                   prints the job count; omit for make's own default
      "markers": ["INCLUDE_ASM", "NON_MATCHING"],     substrings marking a stand-in line (default adds GLOBAL_ASM)
      "asm_suffix": ".s",
      "readme": "README.md",
      "timeout_seconds": 7200,                        per check/jobs call
      "actor": "land"
    }

Project facts (the markers, the README, the check command) come only from that section; the tree is the argument.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, events, tools
from abu_decomp.config import Profile, check_keys

Result = tuple[dict[str, Any], int, list[dict[str, Any]]]
Run = Callable[[Sequence[str], Path, "dict[str, str] | None", "float | None"], "tuple[int, str, str]"]

GATES = ("base", "apply", "setup", "sync", "check", "sync_after", "sync_check", "markers", "untracked_s", "readme")
DEFAULT_MARKERS = ("INCLUDE_ASM", "NON_MATCHING", "GLOBAL_ASM")
_PERCENT = re.compile(r"\bcode\b[^%\n]*?(\d+(?:\.\d+)?)%")
_NAME = re.compile(r"([A-Za-z_]\w*)\s*\)\s*;?\s*$")
_PATH_NAME = re.compile(r"([A-Za-z_]\w*)\.\w+\"")


class LandError(DecompError):
    pass


def parse_patches(rows: Sequence[str]) -> list[tuple[str, Path]]:
    """`LANE=PATH` rows, in order; lane names are unique."""
    patches: list[tuple[str, Path]] = []
    for row in rows:
        lane, sep, path = row.partition("=")
        if not sep or not lane.strip() or not path.strip():
            raise LandError("land.arguments", f"--patch expects LANE=PATH, got {row!r}")
        if any(lane == seen for seen, _ in patches):
            raise LandError("land.arguments", f"lane {lane} is given twice")
        patches.append((lane, Path(path).expanduser().resolve()))
    return patches


def settings(profile: Profile) -> dict[str, Any]:
    table = profile.section("land")
    check_keys(
        profile.source_file,
        "land",
        table,
        frozenset({"check"}),
        frozenset({"check", "jobs", "markers", "asm_suffix", "readme", "timeout_seconds", "actor"}),
    )

    def argv(key: str) -> list[str] | None:
        value = table.get(key)
        if value is None:
            return None
        if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
            raise LandError("config.schema", f"{profile.source_file}:land.{key}: expected a nonempty list of strings")
        return value

    markers = table.get("markers", list(DEFAULT_MARKERS))
    if not isinstance(markers, list) or not markers or not all(isinstance(m, str) and m for m in markers):
        raise LandError("config.schema", f"{profile.source_file}:land.markers: expected a nonempty list of strings")
    timeout = table.get("timeout_seconds", 7200)
    if type(timeout) not in (int, float) or timeout <= 0:
        raise LandError("config.schema", f"{profile.source_file}:land.timeout_seconds: expected a positive number")
    return {
        "check": argv("check"),
        "jobs": argv("jobs"),
        "markers": tuple(markers),
        "suffix": str(table.get("asm_suffix", ".s")),
        "readme": str(table.get("readme", "README.md")),
        "timeout": float(timeout),
        "actor": str(table.get("actor", "land")),
    }


def run_process(
    argv: Sequence[str], cwd: Path, env: dict[str, str] | None, timeout: float | None
) -> tuple[int, str, str]:
    try:
        done = subprocess.run(
            list(argv), cwd=cwd, env=env, capture_output=True, text=True, errors="replace", timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise LandError("land.timeout", f"{argv[0]} exceeded {timeout:g}s") from exc
    except OSError as exc:
        raise LandError("land.process", f"cannot execute {argv[0]}: {exc}") from exc
    return done.returncode, done.stdout, done.stderr


def diff_lines(text: str) -> dict[str, tuple[list[str], list[str]]]:
    """file -> (added lines, removed lines) of a `git diff -U0` document."""
    files: dict[str, tuple[list[str], list[str]]] = {}
    current: tuple[list[str], list[str]] | None = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            name = line.rsplit(" b/", 1)[-1]
            current = files.setdefault(name, ([], []))
        elif current is None or line.startswith(("+++ ", "--- ")):
            continue
        elif line.startswith("+"):
            current[0].append(line[1:])
        elif line.startswith("-"):
            current[1].append(line[1:])
    return files


def marker_lines(lines: Sequence[str], markers: Sequence[str]) -> list[str]:
    return [line for line in lines if any(marker in line for marker in markers)]


def function_name(line: str) -> str | None:
    found = _NAME.search(line) or _PATH_NAME.search(line)
    return found[1] if found else None


def code_percentages(text: str) -> list[float]:
    """Every `code ... NN.NN%` figure of a README, in order (one per version, plus any unnamed sections)."""
    return [float(value) for value in _PERCENT.findall(text)]


class Landing:
    def __init__(
        self,
        profile: Profile,
        tree: Path,
        base: str,
        patches: Sequence[tuple[str, Path]],
        *,
        invoke: Callable[..., Any] = tools.invoke,
        run: Run = run_process,
        append: Callable[[Path, dict[str, Any]], dict[str, Any]] = events.append,
        pin: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = settings(profile)
        self.tree = tree.expanduser().resolve()
        if not self.tree.is_dir():
            raise LandError("land.tree", f"proof tree {self.tree} is not a directory")
        self.profile = profile
        self.base, self.patches = base, list(patches)
        self.invoke, self.run, self.append, self.clock = invoke, run, append, clock
        self.pin = pin or (lambda: tools.pins()["unbake"].commit)
        self.gates: list[dict[str, Any]] = []
        self.findings: list[dict[str, Any]] = []
        self.lanes: list[dict[str, Any]] = []
        self.extra: dict[str, Any] = {}

    # git -------------------------------------------------------------------------------------------------------
    def git(self, *args: str, ok: bool = True) -> str:
        code, out, err = self.run(["git", "-c", "core.quotepath=off", *args], self.tree, None, 600)
        if code and ok:
            raise LandError("land.git", f"git {' '.join(args[:3])}: {(err or out).strip()[-400:]}")
        return out if not code else ""

    def snapshot(self) -> str:
        return self.git("write-tree").strip()

    # gates -----------------------------------------------------------------------------------------------------
    def gate(self, name: str, passed: bool, started: float, **detail: Any) -> bool:
        self.gates.append({"gate": name, "pass": passed, "seconds": round(self.clock() - started, 3), **detail})
        return passed

    def fail(self, key: str, message: str, **detail: Any) -> None:
        self.findings.append({"key": key, "message": message, "blocking": True, **detail})

    def check_base(self) -> bool:
        started = self.clock()
        head = self.git("rev-parse", "--verify", "HEAD^{commit}").strip()
        code, out, _ = self.run(["git", "rev-parse", "--verify", f"{self.base}^{{commit}}"], self.tree, None, 60)
        wanted = out.strip() if code == 0 else ""
        dirty = self.git("status", "--porcelain", "--untracked-files=no").splitlines()
        self.extra.update(head=head, base=wanted or self.base)
        if not wanted:
            self.fail("land.base", f"base ref {self.base} does not exist in {self.tree}")
        elif head != wanted:
            self.fail("land.base", f"tree HEAD {head[:12]} is not base {self.base} ({wanted[:12]})")
        elif dirty:
            self.fail("land.dirty", f"tree has {len(dirty)} tracked changes before landing", files=dirty[:20])
        return self.gate("base", not self.findings, started, head=head)

    def apply_patches(self) -> bool:
        started = self.clock()
        for lane, path in self.patches:
            row: dict[str, Any] = {"lane": lane, "patch": str(path), "status": "applied", "files": []}
            self.lanes.append(row)
            if not path.is_file():
                row.update(status="excluded", reason=f"patch file {path} does not exist")
                continue
            before = self.snapshot()
            code, out, err = self.run(["git", "apply", "--3way", "--index", str(path)], self.tree, None, 600)
            if code:
                reason = (err or out).strip()
                # Put the index and tree back to the state before this patch; the tree is the landing's own.
                self.git("read-tree", "-u", "--reset", before)
                row.update(status="excluded", reason=reason[-600:] or f"git apply exited {code}")
                continue
            after = self.snapshot()
            changed = diff_lines(self.git("diff-tree", "-r", "-p", "-U0", "--no-renames", "--no-color", before, after))
            row["files"] = sorted(changed)
            added = [(f, marker_lines(a, self.config["markers"])) for f, (a, _) in changed.items()]
            removed = [x for _, r in changed.values() for x in marker_lines(r, self.config["markers"])]
            added_lines = [x for _, rows in added for x in rows]
            row["functions"] = {
                "converted": len(removed),
                "added": len(added_lines),
                "net": len(removed) - len(added_lines),
                "names": sorted({n for n in map(function_name, removed) if n}),
            }
        applied = [row for row in self.lanes if row["status"] == "applied"]
        for row in self.lanes:
            if row["status"] == "excluded":
                self.findings.append(
                    {
                        "key": "land.patch_excluded",
                        "message": f"lane {row['lane']} excluded: {(row['reason'].splitlines() or [''])[-1]}",
                        "blocking": False,
                        "lane": row["lane"],
                    }
                )
        if not applied:
            self.fail("land.no_patches", "no patch applied; nothing to land")
        return self.gate(
            "apply",
            bool(applied),
            started,
            applied=[r["lane"] for r in applied],
            excluded=[{"lane": r["lane"], "reason": r["reason"]} for r in self.lanes if r["status"] == "excluded"],
        )

    def unbake(self, name: str, gate: str, *args: str) -> bool:
        started = self.clock()
        called = self.invoke("unbake", self.profile, list(args), project=self.tree)
        if not called.ok:
            findings = called.document.get("findings") or [
                {"message": f"unbake {' '.join(args)} exited {called.returncode}"}
            ]
            first = findings[0]
            self.fail(
                f"land.{gate}",
                f"unbake {' '.join(args)} refused: {first.get('message') or first.get('key')}",
                unbake_findings=findings[:10],
            )
        if called.stderr:
            sys.stderr.write(called.stderr)
        return self.gate(name, called.ok, started)

    def setup(self) -> bool:
        started = self.clock()
        pinned = self.pin()
        marker = Path(self.git("rev-parse", "--absolute-git-dir").strip()) / "abu-decomp-land.json"
        try:
            last = json.loads(marker.read_text()).get("unbake")
        except (OSError, ValueError, AttributeError):
            last = None
        if last == pinned:
            return self.gate("setup", True, started, skipped=True, unbake=pinned)
        if not self.unbake("setup", "setup", "setup"):
            return False
        marker.write_text(json.dumps({"unbake": pinned}) + "\n")
        self.gates[-1].update(skipped=False, unbake=pinned, previous=last)
        return True

    def check(self) -> bool:
        started = self.clock()
        command = list(self.config["check"])
        jobs = None
        if self.config["jobs"]:
            code, out, err = self.run(self.config["jobs"], self.tree, None, 60)
            text = out.strip()
            if code or not text.isdigit() or int(text) < 1:
                self.fail(
                    "land.jobs", f"the jobs command gave no job count (exit {code}): {(text or err.strip())[:200]}"
                )
                return self.gate("check", False, started)
            jobs = int(text)
            command += [f"-j{jobs}"]
        env = dict(os.environ)
        env["UNBAKE_HOST"] = str(tools.settings(self.profile).host)
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        code, out, err = self.run(command, self.tree, env, self.config["timeout"])
        sys.stderr.write(out + err)
        if code:
            tail = "\n".join((out + err).strip().splitlines()[-12:])
            self.fail("land.check", f"{' '.join(command)} exited {code}", tail=tail)
        return self.gate("check", code == 0, started, jobs=jobs, command=command)

    # post-run gates -----------------------------------------------------------------------------------------------
    def changed_files(self) -> list[str]:
        return sorted(self.git("diff", "HEAD", "--name-only", "--no-renames").splitlines())

    def markers(self) -> bool:
        started = self.clock()
        touched = {f for row in self.lanes if row["status"] == "applied" for f in row["files"]}
        diff = diff_lines(self.git("diff", "HEAD", "-U0", "--no-renames", "--no-color"))
        bad = {
            f: marker_lines(added, self.config["markers"])
            for f, (added, _) in diff.items()
            if f not in touched and marker_lines(added, self.config["markers"])
        }
        if bad:
            self.fail(
                "land.markers",
                f"{len(bad)} file(s) no patch touched gained marker lines (sync or check re-added stand-ins): "
                + ", ".join(sorted(bad)[:8]),
                files={f: rows[:5] for f, rows in sorted(bad.items())[:20]},
            )
        return self.gate("markers", not bad, started, files=len(bad))

    def untracked(self) -> bool:
        started = self.clock()
        others = self.git("ls-files", "--others", "--exclude-standard").splitlines()
        stray = sorted(f for f in others if f.endswith(self.config["suffix"]))
        self.extra["untracked_other"] = sorted(set(others) - set(stray))
        if stray:
            self.fail(
                "land.untracked_s",
                f"{len(stray)} untracked {self.config['suffix']} file(s) a patch or sync produced: "
                + ", ".join(stray[:8]),
                files=stray[:50],
            )
        return self.gate("untracked_s", not stray, started, files=len(stray))

    def readme(self) -> bool:
        started = self.clock()
        name = self.config["readme"]
        code, before, _ = self.run(["git", "show", f"{self.extra['base']}:{name}"], self.tree, None, 60)
        path = self.tree / name
        if code or not path.is_file():
            self.fail("land.readme", f"cannot compare {name}: missing at the base or in the tree")
            return self.gate("readme", False, started)
        old, new = code_percentages(before), code_percentages(path.read_text(errors="replace"))
        lower = [{"index": i, "base": a, "now": b} for i, (a, b) in enumerate(zip(old, new, strict=False)) if b < a]
        if not old or len(old) != len(new):
            self.fail("land.readme", f"{name} code percentages changed shape: base {len(old)}, tree {len(new)}")
        elif lower:
            self.fail("land.readme", f"{name} code percentage fell in {len(lower)} place(s)", lower=lower)
        return self.gate(
            "readme", not any(f["key"] == "land.readme" for f in self.findings), started, base=old, now=new
        )

    # grouping -----------------------------------------------------------------------------------------------------
    def groups(self) -> dict[str, Any]:
        """Per-lane file groups: a file belongs to the last applied lane whose patch changed it; changed files no
        patch owns (README, SVG, progress, anything sync wrote) join the last group."""
        owner: dict[str, str] = {}
        shared: dict[str, list[str]] = {}
        for row in self.lanes:
            if row["status"] != "applied":
                continue
            for f in row["files"]:
                if f in owner:
                    shared.setdefault(f, [owner[f]]).append(row["lane"])
                owner[f] = row["lane"]
        applied = [row["lane"] for row in self.lanes if row["status"] == "applied"]
        groups = {lane: sorted(f for f, o in owner.items() if o == lane) for lane in applied}
        generated = [f for f in self.changed_files() if f not in owner]
        if applied:
            groups[applied[-1]] = sorted(groups[applied[-1]] + generated)
        return {
            "groups": [{"lane": lane, "files": files} for lane, files in groups.items()],
            "generated": generated,
            "shared_files": shared,
        }

    # run --------------------------------------------------------------------------------------------------------
    def stages(self) -> list[Callable[[], bool]]:
        unbake = self.unbake
        return [
            self.check_base,
            self.apply_patches,
            self.setup,
            lambda: unbake("sync", "sync", "sync"),
            self.check,
            lambda: unbake("sync_after", "sync_after", "sync"),
            lambda: unbake("sync_check", "sync_check", "sync", "--check"),
            self.markers,
            self.untracked,
            self.readme,
        ]

    def execute(self) -> Result:
        failed = False
        for stage in self.stages():
            if not stage():
                failed = True
                break
        done = {row["gate"] for row in self.gates}
        for name in GATES:
            if name not in done:
                self.gates.append({"gate": name, "pass": None, "skipped": True})
        result: dict[str, Any] = {
            "tree": str(self.tree),
            "base": self.extra.get("base", self.base),
            "head": self.extra.get("head"),
            "pass": not failed,
            "gates": self.gates,
            "lanes": self.lanes,
            "untracked_other": self.extra.get("untracked_other", []),
            **self.groups(),
        }
        result["functions"] = {
            "converted": sum(r["functions"]["converted"] for r in self.lanes if r["status"] == "applied"),
            "added": sum(r["functions"]["added"] for r in self.lanes if r["status"] == "applied"),
        }
        self.write_event(result)
        return result, int(not result["pass"]), self.findings

    def write_event(self, result: dict[str, Any]) -> None:
        names = [row["lane"] for row in self.lanes if row["status"] == "applied"]
        blocking = next((f for f in self.findings if f["blocking"]), None)
        message = (
            f"land {len(names)} lane(s) at {str(result['head'] or '')[:12]}: "
            f"{result['functions']['converted']} converted"
            if result["pass"]
            else f"land failed: {blocking['key'] if blocking else 'unknown'}"
        )
        record = {
            "actor": self.config["actor"],
            "status": "passed" if result["pass"] else "failed",
            "message": message,
            "kind": "LAND",
            "subject": {"repo": str(self.tree), "ref": str(result["head"] or "")},
            "payload": {
                "base": result["base"],
                "gates": {row["gate"]: row["pass"] for row in self.gates},
                "lanes": {row["lane"]: {"status": row["status"], **(row.get("functions") or {})} for row in self.lanes},
            },
        }
        try:
            result["event"] = self.append(events.events_path(self.profile), record)
        except DecompError as exc:
            self.findings.append(exc.finding())
            result["event"] = None
            result["pass"] = False


def land(profile: Profile, tree: Path, base: str, patches: Sequence[tuple[str, Path]], **hooks: Any) -> Result:
    return Landing(profile, tree, base, patches, **hooks).execute()
