"""Proof-carrying patches: the build moves out of the ship lock into the lane's own work tree (10-22: one ship held the
lock 26 minutes with 11 queued, 120 s of it the land's own setup, check, sync and audit).

A lane proves its patch where it works: `prove` applies the patch to the lane's work tree (`<shared>/../wt/<LANE>`, at
the base commit B it works from), runs the same setup and `make check` a land runs, and writes a proof record for the
patch hash: base B, the files the patch touches, every unit whose preprocessed text depends on one of them (its
`.i` hash and the files it includes), the five ROM hashes, the gates that passed. The record is stored beside the ship
lock (`decomp-ship.proofs/<patch sha>.json`) and, with each landed commit, as a git note (`refs/notes/ship-proof`).

At landing (`judge`) the ship asks only whether the proof still holds on the branch tip M: a unit's object is a pure
function of its preprocessed text, so if no commit since B touched a file any of the patch's units read, or a row the
patch also edits in a fact file, the proof carries over and the land builds nothing. Anything else (a header change,
a build input such as config.toml, an overlap) goes through the full land as before."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, land, meter, tools, worktree
from abu_decomp.config import Profile

VERSION = 1
PROOFS = "decomp-ship.proofs"
LANDED = "decomp-ship.landed"
NOTES = "ship-proof"  # refs/notes/ship-proof
# Files every unit's object reads without naming them in its .d: a commit touching one of these proves nothing carries.
GLOBAL_INPUTS = (
    "config.toml",
    "Makefile",
    "local.mk",
    "rom.toml",
    "blobs.toml",
    "RageWars.ld",
    "versions/*/text_sizes.mk",
    "tools/*",
)
# Rows merged one by one (a commit and a patch editing different rows never conflict).
ROW_FILES = ("versions/*/symbol_addrs.txt", "versions/*/aliases.txt", "versions/*/splat.yaml")
# What `unbake sync` writes after a build: never part of a patch, regenerated in a batch (ship --regen).
GENERATED = ("README.md", "progress.json", "docs/*")
HEADER = re.compile(r"\.(h|hpp|inc)$", re.I)
SHA1_ROW = re.compile(r"^SHA1_([\w-]+)\s*:?=\s*([0-9a-f]{40})\s*$", re.M)


TYPES = "-types"  # the lane name suffix of the header half of a split patch (a types-train input)
BLOCK = re.compile(r"^diff --git a/(?P<a>\S+) b/(?P<b>\S+)", re.M)


class NoProof(DecompError):
    """The patch cannot be proved in a work tree (no tree, another state, a tool refusal): the full land takes it."""


def matches(name: str, globs: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, glob) for glob in globs)


def patch_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_generated(name: str, extra: Sequence[str] = ()) -> bool:
    return matches(name, (*GENERATED, *extra))


def row_ids(added: Sequence[str], removed: Sequence[str]) -> list[str]:
    """The rows a change edits, by identity: a symbol fact is its name and its address (two facts at one address or of
    one name are the same row), any other row is its text."""
    ids: set[str] = set()
    for line in (*added, *removed):
        if not line.strip():
            continue
        fact = land.symbol_line(line)
        ids.update((f"name:{fact[0]}", f"addr:{fact[1]:#x}") if fact else (f"row:{line.strip()}",))
    return sorted(ids)


def describe(text: str, generated: Sequence[str] = ()) -> dict[str, Any]:
    """What a patch touches: files, headers among them, the rows of the row files, the generated files."""
    changed = land.diff_lines(text)
    files = sorted(changed)
    return {
        "files": [f for f in files if not is_generated(f, generated)],
        "headers": [f for f in files if HEADER.search(f) and not is_generated(f, generated)],
        "rows": {f: row_ids(*changed[f]) for f in files if matches(f, ROW_FILES)},
        "generated": [f for f in files if is_generated(f, generated)],
    }


def parse_deps(text: str) -> list[str]:
    """The files a `.d` rule names, source first: `target: a b \\` continued lines, plus the -MP phony `h:` lines."""
    head = text.replace("\\\n", " ").splitlines()
    if not head:
        return []
    _, _, rest = head[0].partition(":")
    return rest.split()


def split_patch(text: str, generated: Sequence[str] = ()) -> tuple[str, str] | None:
    """(types, body): the file sections of a patch that are headers, and the others (generated files drop). None when
    the patch is not both: a header-only or a body-only patch needs no split."""
    starts = [m.start() for m in BLOCK.finditer(text)]
    types, body = [], []
    for begin, end in zip(starts, [*starts[1:], len(text)], strict=True):
        block = text[begin:end]
        found = BLOCK.match(block)
        name = found["b"] if found else ""
        if is_generated(name, generated):
            continue
        (types if HEADER.search(name) else body).append(block if block.endswith("\n") else block + "\n")
    return ("".join(types), "".join(body)) if types and body else None


def body_record(record: dict[str, Any], body: str, generated: Sequence[str] = ()) -> dict[str, Any]:
    """The proof of the body half, derived from the proof of the whole patch (built at the same base with the headers
    applied): the headers it needs are the types train's, recognised at landing by their blob (see judge)."""
    shape = describe(body, generated)
    return {
        **record,
        "patch": hashlib.sha256(body.encode()).hexdigest(),
        "files": shape["files"],
        "headers": [],
        "rows": shape["rows"],
        "generated": shape["generated"],
        "closure": closure(shape["files"], record["units"]),
        "split_of": record["patch"],
    }


def sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def collect_units(tree: Path, touched: set[str]) -> dict[str, dict[str, Any]]:
    """Per `version:source`, every built unit whose source is touched or whose preprocessed text reads a touched file:
    the units whose `.i` the patch can change, with the `.i` hash the proof built and the files it reads."""
    out: dict[str, dict[str, Any]] = {}
    build = tree / "build"
    for dep in sorted(build.rglob("*.d")):
        try:
            deps = parse_deps(dep.read_text(errors="replace"))
        except OSError:
            continue
        if not deps or not (touched & set(deps)):
            continue
        version = dep.relative_to(build).parts[0]
        out[f"{version}:{deps[0]}"] = {"i": sha256(dep.with_name(dep.name[:-2] + ".i")), "reads": sorted(set(deps))}
    return out


def rom_report(tree: Path) -> dict[str, dict[str, Any]]:
    """Each version's built ROM hashed against the Makefile's SHA1 row."""
    try:
        wanted = dict(SHA1_ROW.findall((tree / "Makefile").read_text()))
    except OSError:
        return {}
    report = {}
    for version, expected in sorted(wanted.items()):
        rom = tree / "build" / version / "RageWars.z64"
        try:
            got = hashlib.sha1(rom.read_bytes()).hexdigest()
        except OSError:
            got = None
        report[version] = {"sha1": got, "ok": got == expected}
    return report


def closure(files: Sequence[str], units: dict[str, dict[str, Any]]) -> list[str]:
    names = set(files)
    for unit in units.values():
        names.update(unit["reads"])
    return sorted(names)


def build_record(
    lane: str,
    base: str,
    patch: str,
    shape: dict[str, Any],
    units: dict[str, dict[str, Any]],
    roms: dict[str, dict[str, Any]],
    gates: dict[str, bool],
    **extra: Any,
) -> dict[str, Any]:
    return {
        "v": VERSION,
        "lane": lane,
        "base": base,
        "patch": patch,
        "files": shape["files"],
        "headers": shape["headers"],
        "rows": shape["rows"],
        "generated": shape["generated"],
        "units": units,
        "closure": closure(shape["files"], units),
        "roms": roms,
        "gates": gates,
        "pass": all(gates.values()) and all(r["ok"] for r in roms.values()),
        "made": time.time(),
        **extra,
    }


# store --------------------------------------------------------------------------------------------------------------
def store(common: Path, record: dict[str, Any]) -> Path:
    folder = common / PROOFS
    folder.mkdir(exist_ok=True)
    path = folder / f"{record['patch']}.json"
    temp = path.with_name(f".{path.name}.{os.getpid()}")
    temp.write_text(json.dumps(record))
    temp.rename(path)
    return path


def load(common: Path, patch: str) -> dict[str, Any] | None:
    try:
        record = json.loads((common / PROOFS / f"{patch}.json").read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("v") == VERSION else None


def prune(common: Path, keep_seconds: float = 3 * 86400.0) -> None:
    cutoff = time.time() - keep_seconds
    for folder in (common / PROOFS, common / LANDED):
        if folder.is_dir():
            for old in folder.glob("*.json"):
                if old.stat().st_mtime < cutoff:
                    old.unlink(missing_ok=True)


# the proof, built in the lane's own tree --------------------------------------------------------------------------
def worktree_of(shared: Path, lane: str) -> Path | None:
    path = worktree.base(shared) / lane
    return path if (path / ".git").exists() else None


def prove(
    profile: Profile,
    shared: Path,
    lane: str,
    patch: Path,
    *,
    run: land.Run = land.run_process,
    build: land.Run = land.build_process,
    invoke: Callable[..., Any] = tools.invoke,
    tree: Path | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Build the proof record of `patch` in the lane's work tree; NoProof when it cannot be built there. The record's
    `pass` is False when the build or a ROM hash refused it (its `tail` says why): the patch cannot land at all."""
    work = tree or worktree_of(shared, lane)
    if work is None:
        raise NoProof("ship.proof_skipped", f"{shared.parent / 'wt' / lane} is not a work tree: the ship builds it")
    started, cpu = clock(), meter.cpu()
    text = patch.read_text(errors="replace")
    table = land.settings(profile, shared)
    shape = describe(text, table["regenerated_paths"])

    def git(*args: str, ok: bool = True) -> tuple[int, str]:
        code, out, err = run(["git", "-C", str(work), "-c", "core.quotepath=off", *args], work, None, 600)
        if code and ok:
            raise NoProof("ship.proof_skipped", f"git {' '.join(args[:3])} in {work}: {(err or out).strip()[-300:]}")
        return code, out if not code else (err or out)

    base = git("rev-parse", "--verify", "HEAD^{commit}")[1].strip()
    applied_here = False
    if git("apply", "-R", "--check", str(patch), ok=False)[0] != 0:  # not applied yet: apply it for the proof
        if git("apply", "--check", str(patch), ok=False)[0] != 0:
            raise NoProof(
                "ship.proof_skipped", f"the patch applies on {work} at {base[:12]} neither forward nor in reverse"
            )
        git("apply", str(patch))
        applied_here = True
    try:
        dirty = {line[3:].strip() for line in git("status", "--porcelain", "--untracked-files=no")[1].splitlines()}
        named = set(land.diff_lines(text))
        if extra := sorted(dirty - named):
            raise NoProof(
                "ship.proof_skipped",
                f"{work} has {len(extra)} tracked change(s) beyond the patch ({', '.join(extra[:4])}): "
                "a proof of that tree would not be the patch's",
            )
        called = invoke("unbake", profile, ["setup", "--no-make"], project=work)
        if not called.ok:
            raise NoProof("ship.proof_skipped", f"unbake setup refused in {work}")
        jobs = None
        if table["jobs"]:
            code, out, _ = run(table["jobs"], work, None, 60)
            jobs = int(out.strip()) if not code and out.strip().isdigit() else None
        command = land.build_command(table["check"], jobs)
        env = {**os.environ, "UNBAKE_HOST": str(tools.host_file(profile))}
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        code, out, err = build(command, work, env, table["timeout"])
        verdict = invoke("unbake", profile, ["verify", str(code)], project=work)
        gates = {"check": code == 0, "verify": bool(verdict.ok)}
        units = collect_units(work, set(shape["files"])) if code == 0 else {}
        blobs = {}
        for header in shape[
            "headers"
        ]:  # the header texts this proof built with: a types train that lands them keeps it
            got = git("hash-object", "--", header, ok=False)
            if got[0] == 0 and got[1].strip():
                blobs[header] = got[1].strip()
        roms = rom_report(work) if code == 0 else {}
        record = build_record(
            lane,
            base,
            patch_hash(patch),
            shape,
            units,
            roms,
            gates,
            tree=str(work),
            blobs=blobs,
            seconds=round(clock() - started, 1),
            cpu=round(meter.cpu() - cpu, 1),
        )
        if not record["pass"]:
            record["tail"] = "\n".join((out + err).strip().splitlines()[-12:])
        return record
    finally:
        if applied_here:
            git("apply", "-R", str(patch), ok=False)


# the carry-over decision, under the lock ---------------------------------------------------------------------------
def judge(
    record: dict[str, Any] | None,
    patch: str,
    tip: str,
    git: Callable[..., tuple[int, str]],
    generated: Sequence[str] = (),
) -> tuple[bool, str]:
    """(True, why) when the proof of `record` still holds on `tip`: nothing landed since its base touched a file the
    patch's units read or a row the patch edits. `git(*args)` runs git in the shared tree as (exit, output)."""
    if record is None:
        return False, "no proof record for this patch"
    if record.get("patch") != patch:
        return False, "the patch changed after it was proved"
    if not record.get("pass"):
        return False, "the proof did not pass"
    base = record["base"]
    if base == tip:
        return True, "the branch is still at the proof base"
    if git("merge-base", "--is-ancestor", base, tip)[0] != 0:
        return False, f"proof base {base[:12]} is not an ancestor of {tip[:12]}: rebase the work tree"
    names = git("diff", "--name-only", "--no-renames", base, tip)[1].split()
    others = [n for n in names if not is_generated(n, generated)]
    for name in others:
        if matches(name, GLOBAL_INPUTS):
            return False, f"{name} changed since the proof: every unit reads it"
    closure_names, rows = set(record["closure"]), record["rows"]
    headers = bool(record["headers"])
    blobs = record.get("blobs") or {}
    for name in others:
        if name in blobs and git("rev-parse", f"{tip}:{name}")[1].strip() == blobs[name]:
            continue  # the branch holds exactly the header text the proof built with (the types train landed it)
        if matches(name, ROW_FILES):
            mine = set(rows.get(name, ()))
            if mine:
                added, removed = land.diff_lines(git("diff", "-U0", "--no-renames", base, tip, "--", name)[1]).get(
                    name, ([], [])
                )
                clash = sorted(mine & set(row_ids(added, removed)))
                if clash:
                    return False, f"{name}: rows the patch edits changed since the proof ({', '.join(clash[:3])})"
            continue
        if name in closure_names:
            readers = sorted(u for u, row in record["units"].items() if name in row["reads"])[:3]
            return False, f"{name} changed since the proof" + (f", read by {', '.join(readers)}" if readers else "")
        if headers:
            return False, f"the patch changes a header and {name} changed since the proof: a types train takes it"
    return True, f"{len(others)} file(s) landed since the proof, none read by its {len(record['units'])} unit(s)"


def annotate(
    run: land.Run, tree: Path, sha: str, payload: dict[str, Any], env: dict[str, str] | None = None
) -> tuple[int, str]:
    """The record stored with a landed commit: a git note under refs/notes/ship-proof."""
    code, out, err = run(
        ["git", "notes", f"--ref={NOTES}", "add", "-f", "-m", json.dumps(payload, sort_keys=True), sha],
        tree,
        {**os.environ, **env} if env else None,
        60,
    )
    return code, (err or out)
