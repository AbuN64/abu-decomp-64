"""`decomp port remap`: carry a source repository's symbol renames into the facts a tree keeps about that source.
A port spells symbols of the game it builds (manifest, bindings, boot trace, ledgers). The game repository renames
symbols all the time (a cleanup gives hundreds of functions and data a clean name), and a name the source no longer
defines breaks the port's link or its facts. The rename map between two source commits comes from the source's own
address tables: an address that lost exactly one name and gained exactly one is a rename. `apply` rewrites every
whole-word spelling of an old name in the tree's fact paths; a name the source dropped without a successor and the
tree still spells is a finding, never a guess. `land.trees.<tree>.pin.remap` names the source's fact files and the
tree's paths (project facts live in the profile, not here); `decomp repin` runs it with every pin move.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from abu_decomp import DecompError

Run = Callable[[Sequence[str], Path, "dict[str, str] | None", "float | None"], "tuple[int, str, str]"]
FACT = re.compile(r"^\s*(\w+)\s*=\s*(0x[0-9A-Fa-f]+)\s*;(.*)$")
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
    changed: dict[str, int] = {}
    stale: dict[str, list[str]] = {}
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
        if count:
            changed[rel] = count
            if write:
                path.write_text(out)
        left = sorted({t for t in TOKEN.findall(out) if t in gone})
        if left:
            stale[rel] = left
    return {"files": changed, "spellings": sum(changed.values()), "stale": stale}


def remap(
    run: Run, tree: Path, pin: dict[str, Any], old: str, new: str, write: bool = True
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Result and findings of carrying the source's renames from `old` to `new` into `tree` (pin config `remap`)."""
    config = pin.get("remap")
    if not config:
        raise RemapError("remap.config", "the tree's land.trees pin has no `remap` (facts, paths)")
    source = pin["source"]
    found = renames(facts(run, source, old, config["facts"]), facts(run, source, new, config["facts"]))
    done = apply(tree, config["paths"], found, write)
    result = {
        "tree": str(tree), "from": old, "to": new, "renamed": len(found["renames"]), "files": done["files"],
        "spellings": done["spellings"], "ambiguous": len(found["ambiguous"]), "written": write,
    }  # fmt: skip
    findings = [
        {"key": "remap.stale", "blocking": True,
         "message": f"{rel} spells {', '.join(names[:5])}{' ...' if len(names) > 5 else ''}: the source dropped "
         f"{'it' if len(names) == 1 else 'them'} with no successor name, so the tree cannot link or load",
         "next": f"git -C {source} log -S{names[0]} --oneline {old[:12]}..{new[:12]}"}
        for rel, names in sorted(done["stale"].items())
    ]  # fmt: skip
    if found["ambiguous"]:
        sample = found["ambiguous"][0]
        findings.append(
            {"key": "remap.ambiguous", "blocking": False,
             "message": f"{len(found['ambiguous'])} addresses changed several names (first {sample['address']}: "
             f"{','.join(sample['lost'])} -> {','.join(sample['gained'])}); those names are not remapped"}
        )  # fmt: skip
    return result, findings
