"""`decomp port remap`: carry a source repository's symbol renames into the facts a tree keeps about that source.
A port spells symbols of the game it builds (manifest, bindings, boot trace, ledgers). The game repository renames
symbols all the time (a cleanup gives hundreds of functions and data a clean name), and a name the source no longer
defines breaks the port's link or its facts. The rename map between two source commits comes from the source's own
address tables: an address that lost exactly one name and gained exactly one is a rename. `apply` rewrites every
whole-word spelling of an old name in the tree's fact paths; a name the source dropped without a successor and the
tree still spells is a finding, never a guess. `land.trees.<tree>.pin.remap` names the source's fact files and the
tree's paths (project facts live in the profile, not here); `decomp repin` runs it with every pin move.
A source may also spell a table name by an alias (`#define D_80146398 fz_D_80146398`: the object holds the alias); a
fact naming an alias the source no longer defines is dropped from a JSON fact file (the entry has no symbol to bind),
and named as stale anywhere else.
A source FILE the source moved or merged away is carried too: a git rename, or a deleted unit whose functions another
file now defines (a merge of units), rewrites the path; a line that then repeats a JSON list's entry is dropped, and a
deleted unit with no successor is dropped from JSON lists and named as stale elsewhere (10-25: a merged unit left
rage-ports' boot source list naming a deleted file, and every train round skipped the port build).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from abu_decomp import DecompError

Run = Callable[[Sequence[str], Path, "dict[str, str] | None", "float | None"], "tuple[int, str, str]"]
FACT = re.compile(r"^\s*(\w+)\s*=\s*(0x[0-9A-Fa-f]+)\s*;(.*)$")
ALIAS = re.compile(r"^[ \t]*#[ \t]*define[ \t]+(\w+)[ \t]+(\w+)[ \t]*(?://.*|/\*.*\*/)?$", re.M)
MEMBER = r'^[ \t]*"{}"[ \t]*:[ \t]*(?:"[^"\n]*"|[\w.+-]+)[ \t]*,?[ \t]*\n'  # a one-line JSON member
TOKEN = re.compile(r"\w+")
MAX_BYTES = 8 << 20
SKIP_DIRS = frozenset({".git", "build", ".unbake", "__pycache__"})


class RemapError(DecompError):
    pass


def parse(text: str) -> dict[tuple[int, str], set[str]]:
    """(address, func|data) -> the names an address table gives it; a line is `name = 0xADDR; // attributes`."""
    out: dict[tuple[int, str], set[str]] = {}
    for line in text.splitlines():
        found = FACT.match(line)
        if found:
            kind = "func" if "type:func" in found.group(3) else "data"
            out.setdefault((int(found.group(2), 16), kind), set()).add(found.group(1))
    return out


def facts(run: Run, source: Path, rev: str, files: Sequence[str]) -> dict[tuple[int, str], set[str]]:
    """The address tables `files` (paths in the source) as they were at `rev`; a table absent there is empty."""
    merged: dict[tuple[int, str], set[str]] = {}
    for name in files:
        code, out, err = run(["git", "-C", str(source), "show", f"{rev}:{name}"], source, None, 120)
        if code:
            if "does not exist" in err or "exists on disk, but not in" in err:
                continue
            raise RemapError("remap.facts", f"git show {rev[:12]}:{name} failed in {source}: {err.strip()[-200:]}")
        for key, names in parse(out).items():
            merged.setdefault(key, set()).update(names)
    return merged


def aliases(run: Run, source: Path, rev: str, table: dict[tuple[int, str], set[str]]) -> set[str]:
    """The alias spellings the source's C defines at `rev` for names of its address `table`: `#define NAME ALIAS` with
    NAME in the table and ALIAS not."""
    code, out, _err = run(
        ["git", "-C", str(source), "grep", "-h", "-I", "-E", r"^\s*#\s*define\s+\w+\s+\w+", rev], source, None, 120
    )
    if code not in (0, 1):
        raise RemapError("remap.aliases", f"git grep #define at {rev[:12]} failed in {source}")
    names = {name for found in table.values() for name in found}
    return {m.group(2) for m in ALIAS.finditer(out) if m.group(1) in names and m.group(2) not in names}


def renames(old: dict[tuple[int, str], set[str]], new: dict[tuple[int, str], set[str]]) -> dict[str, Any]:
    """{"renames": old name -> new name, "ambiguous": [...], "gone": names no longer defined anywhere}: an address
    that lost exactly one name and gained exactly one is a rename; several lost or gained names are ambiguous."""
    ren: dict[str, str] = {}
    ambiguous: list[dict[str, Any]] = []
    survivors = {name for names in new.values() for name in names}
    for key in sorted(old.keys() & new.keys()):
        lost, gained = old[key] - new[key], new[key] - old[key]
        if not lost or not gained:
            continue
        if len(lost) == 1 and len(gained) == 1:
            (was,), (now,) = lost, gained
            if was not in survivors:
                ren[was] = now
        else:
            ambiguous.append({"address": f"0x{key[0]:08X}", "lost": sorted(lost), "gained": sorted(gained)})
    gone = {name for names in old.values() for name in names} - survivors
    return {"renames": ren, "ambiguous": ambiguous, "gone": gone - set(ren)}


def files_under(tree: Path, paths: Sequence[str]) -> list[Path]:
    """The text files below `paths` (files or directories of the tree): no build products, binaries or huge files."""
    found: list[Path] = []
    for rel in paths:
        root = tree / rel
        if root.is_file():
            found.append(root)
        elif root.is_dir():
            found += sorted(
                p for p in root.rglob("*")
                if p.is_file() and not SKIP_DIRS & set(p.relative_to(root).parts) and p.stat().st_size <= MAX_BYTES
            )  # fmt: skip
    return found


def read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except (UnicodeDecodeError, OSError):
        return None


def apply(tree: Path, paths: Sequence[str], found: dict[str, Any], write: bool = True) -> dict[str, Any]:
    """Rewrite every whole-word spelling of a renamed symbol in the tree's fact paths. `files` is file -> spellings
    changed; `stale` is file -> dropped names (no successor) the tree still spells: findings the caller refuses."""
    ren, gone = found["renames"], found["gone"]
    dead = found.get("dead_aliases", set())
    changed: dict[str, int] = {}
    stale: dict[str, list[str]] = {}
    dropped: dict[str, list[str]] = {}
    for path in files_under(tree, paths):
        text = read_text(path)
        if text is None:
            continue
        count = 0

        def swap(match: re.Match[str]) -> str:
            nonlocal count
            new = ren.get(match.group(0))
            if new is None:
                return match.group(0)
            count += 1
            return new

        out = TOKEN.sub(swap, text)
        rel = str(path.relative_to(tree))
        if path.suffix == ".json" and (gone_here := sorted(dead & set(TOKEN.findall(out)))):
            pruned, cut = _drop_members(out, gone_here)
            if cut:
                out, dropped[rel], count = pruned, cut, count + len(cut)
        if count:
            changed[rel] = count
            if write:
                path.write_text(out)
        left = sorted({t for t in TOKEN.findall(out) if t in gone or t in dead})
        if left:
            stale[rel] = left
    return {"files": changed, "spellings": sum(changed.values()), "stale": stale, "dropped": dropped}


def _drop_members(text: str, names: Sequence[str]) -> tuple[str, list[str]]:
    """TEXT without the one-line members keyed by NAMES (a comma left before a closing brace removed), and the names
    cut; the text unchanged when it is no JSON before or after."""
    try:
        json.loads(text)
    except ValueError:
        return text, []
    out, cut = text, []
    for name in names:
        pruned = re.sub(MEMBER.format(re.escape(name)), "", out, flags=re.M)
        if pruned != out:
            out, cut = re.sub(r",(\s*\n[ \t]*[}\]])", r"\1", pruned), [*cut, name]
    try:
        json.loads(out)
    except ValueError:
        return text, []
    return out, cut


DEFINED = re.compile(r"^[A-Za-z_][\w \t\*]*?\b([A-Za-z_]\w*)\s*\([^;{]*\)\s*\{?\s*$", re.M)
SOURCE_DIRS = ("src",)


DEFINITIONS_PER_GREP = 400


def definitions(run: Run, source: Path, rev: str, names: Sequence[str]) -> dict[str, set[str]]:
    """name -> the files of REV that declare or define a function of that name (a line starting at column 0 with a type
    and ending in `name(`): one git grep per batch of names, never one per name."""
    hits: dict[str, set[str]] = {}
    for start in range(0, len(names), DEFINITIONS_PER_GREP):
        batch = "|".join(re.escape(n) for n in names[start : start + DEFINITIONS_PER_GREP])
        code, out, _err = run(
            [
                "git",
                "-C",
                str(source),
                "grep",
                "-o",
                "-E",
                rf"^[A-Za-z_][^;]*\b({batch})\s*\(",
                rev,
                "--",
                *SOURCE_DIRS,
            ],
            source,
            None,
            300,
        )
        for line in out.splitlines() if code == 0 else []:
            path, _, text = line.removeprefix(f"{rev}:").partition(":")
            if named := re.search(r"(\w+)\s*\($", text):
                hits.setdefault(named.group(1), set()).add(path)
    return hits


def moves(run: Run, source: Path, old: str, new: str) -> dict[str, str | None]:
    """Source files of `old` that `new` lacks: path -> the file that holds them now (a git rename, or for a deleted
    unit the one file defining most of its functions at `new`), or None when nothing does."""
    code, out, err = run(  # the rename limit is raised past the files a long range moves: none is left as delete + add
        [
            *("git", "-C", str(source), "-c", "diff.renameLimit=20000", "diff", "--name-status", "-M", "-z"),
            *(old, new, "--", *SOURCE_DIRS),
        ],
        source,
        None,
        120,
    )
    if code:
        raise RemapError("remap.moves", f"git diff {old[:12]}..{new[:12]} failed in {source}: {err.strip()[-200:]}")
    words, found = out.split("\0"), {}
    index = 0
    while index < len(words) and words[index]:
        status = words[index]
        if status.startswith("R"):
            found[words[index + 1]] = words[index + 2]
            index += 3
            continue
        if status == "D":
            found[words[index + 1]] = None
        index += 2
    wanted: dict[str, list[str]] = {}
    for path, now in found.items():
        if now is not None:
            continue
        code, text, _err = run(["git", "-C", str(source), "show", f"{old}:{path}"], source, None, 120)
        wanted[path] = list(dict.fromkeys(m.group(1) for m in DEFINED.finditer(text or "")))[:8] if not code else []
    hits = definitions(run, source, new, sorted({n for names in wanted.values() for n in names}))
    for path, names in wanted.items():
        votes: dict[str, int] = {}
        for name in names:
            for hit in hits.get(name, ()):
                votes[hit] = votes.get(hit, 0) + 1
        if votes:
            found[path] = max(sorted(votes), key=lambda f: votes[f])
    return found


_LIST_LINE = r'^[ \t]*"{}"[ \t]*,?[ \t]*\n'


def carry_moves(tree: Path, paths: Sequence[str], moved: dict[str, str | None], write: bool = True) -> dict[str, Any]:
    """Rewrite the tree's fact paths for MOVED source files (`moves`): {"files": rel -> paths carried, "stale": rel ->
    deleted paths with no successor still named outside a JSON list}."""
    changed: dict[str, list[str]] = {}
    stale: dict[str, list[str]] = {}
    for path in files_under(tree, paths):
        text = read_text(path)
        if text is None:
            continue
        out, done, left = text, [], []
        for was, now in moved.items():
            spelled = re.compile(rf"(?<![\w/.]){re.escape(was)}(?![\w/.])")
            if not spelled.search(out):
                continue
            line = re.compile(_LIST_LINE.format(re.escape(was)), re.M)
            if (
                path.suffix == ".json"
                and line.search(out)
                and (now is None or re.search(_LIST_LINE.format(re.escape(now)), out, re.M))
            ):
                out = re.sub(
                    r",(\s*\n[ \t]*[}\]])", r"\1", line.sub("", out)
                )  # the list holds it already, or nothing does
            elif now is not None:
                out = spelled.sub(now, out)
            else:
                left.append(was)
                continue
            done.append(was)
        rel = str(path.relative_to(tree))
        if done and path.suffix == ".json":
            try:
                json.loads(out)
            except ValueError:
                left, done, out = left + done, [], text
        if done:
            changed[rel] = done
            if write:
                path.write_text(out)
        if left:
            stale[rel] = left
    return {"files": changed, "stale": stale}


def remap(
    run: Run, tree: Path, pin: dict[str, Any], old: str, new: str, write: bool = True
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Result and findings of carrying the source's renames from `old` to `new` into `tree` (pin config `remap`)."""
    config = pin.get("remap")
    if not config:
        raise RemapError("remap.config", "the tree's land.trees pin has no `remap` (facts, paths)")
    source = pin["source"]
    before, after = facts(run, source, old, config["facts"]), facts(run, source, new, config["facts"])
    found = renames(before, after)
    survivors = {n for names in after.values() for n in names} | aliases(run, source, new, after)
    found["dead_aliases"] = aliases(run, source, old, before) - survivors
    done = apply(tree, config["paths"], found, write)
    carried = carry_moves(tree, config["paths"], moves(run, source, old, new), write)
    result = {
        "tree": str(tree), "from": old, "to": new, "renamed": len(found["renames"]), "files": done["files"],
        "spellings": done["spellings"], "ambiguous": len(found["ambiguous"]), "written": write,
        "dropped_aliases": done["dropped"], "moved_sources": carried["files"],
    }  # fmt: skip
    findings = [
        {"key": "remap.stale", "blocking": True,
         "message": f"{rel} spells {', '.join(names[:5])}{' ...' if len(names) > 5 else ''}: the source dropped "
         f"{'it' if len(names) == 1 else 'them'} with no successor name, so the tree cannot link or load",
         "next": f"git -C {source} log -S{names[0]} --oneline {old[:12]}..{new[:12]}"}
        for rel, names in sorted(done["stale"].items())
    ] + [
        {"key": "remap.stale", "blocking": True,
         "message": f"{rel} names {', '.join(files[:3])}: the source deleted {'it' if len(files) == 1 else 'them'} and "
         "no file defines its functions now",
         "next": f"git -C {source} log --oneline --diff-filter=D {old[:12]}..{new[:12]} -- {files[0]}"}
        for rel, files in sorted(carried["stale"].items())
    ]  # fmt: skip
    if found["ambiguous"]:
        sample = found["ambiguous"][0]
        findings.append(
            {"key": "remap.ambiguous", "blocking": False,
             "message": f"{len(found['ambiguous'])} addresses changed several names (first {sample['address']}: "
             f"{','.join(sample['lost'])} -> {','.join(sample['gained'])}); those names are not remapped"}
        )  # fmt: skip
    return result, findings


def from_map(tree: Path, pin: dict[str, Any], maps: Sequence[Path], write: bool = True) -> dict[str, Any]:
    """Carry the renames of name-ledger batch maps (renames/batch-K.tsv: old, new, ...) into the tree facts. The map is
    the batch's proven token map, so no source history is read; a name the maps rename twice keeps its last spelling."""
    config = pin.get("remap")
    if not config:
        raise RemapError("remap.config", "the tree's land.trees pin has no `remap` (facts, paths)")
    ren: dict[str, str] = {}
    for path in maps:
        for line in path.read_text(encoding="utf-8").splitlines():
            cols = line.split("\t")
            if len(cols) >= 2 and cols[0] and cols[1] and cols[0] != cols[1]:
                ren[cols[0]] = cols[1]
    for old in list(ren):  # a chain a -> b -> c lands on c
        seen = {old}
        while ren[old] in ren and ren[old] not in seen:
            seen.add(ren[old])
            ren[old] = ren[ren[old]]
    done = apply(tree, config["paths"], {"renames": ren, "gone": set(), "dead_aliases": set()}, write)
    return {
        "tree": str(tree), "maps": [str(m) for m in maps], "renamed": len(ren), "files": done["files"],
        "spellings": done["spellings"], "written": write,
    }  # fmt: skip
