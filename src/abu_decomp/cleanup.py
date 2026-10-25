"""`decomp cleanup count`: the placeholder names a cleanup pass removes, per folder, from one shared set of patterns.

Lanes and the orchestrator's WATCH report the same numbers when they all run this one verb: a placeholder is a
function (`func_<8 hex>`), a datum (`D_<8 hex>`), an unnamed member (`unk<hex>`), a padding member (`pad<hex>`) or a
stack slot (`sp<hex>`), a decompiler local (`var_s0`, `temp_f2`) or an unnamed parameter (`arg0`), counted per token
occurrence in the tracked C sources and headers. `evidence` adds, per folder, how many of those a Recipe Maker step can
name (a slot with source evidence) and how many it cannot (`no_evidence`), so a count shows what is namable."""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

CLASSES: dict[str, re.Pattern[str]] = {
    "func": re.compile(r"\bfunc_[0-9A-F]{8}"),
    "data": re.compile(r"\bD_[0-9A-F]{8}"),
    "unk": re.compile(r"\bunk[0-9A-Fa-f_]+"),
    "pad": re.compile(r"\bpad[0-9A-Fa-f]+"),
    "sp": re.compile(r"\bsp[0-9A-F]{2,}\b"),
    "var": re.compile(r"\b(?:var|temp)_[A-Za-z0-9]\w*"),
    "arg": re.compile(r"\barg\d+\b"),
}
# Recipe Maker step -> (what it names, the placeholder classes it covers): the evidence columns of `count --evidence`
EVIDENCE: dict[str, tuple[str, tuple[str, ...]]] = {
    "L": ("locals", ("sp", "var", "arg")),
    "N4": ("data", ("data",)),
    "N5": ("functions", ("func",)),
}
SUFFIXES = (".c", ".cpp", ".h")
ROOTS = ("src/", "include/")


def folder_of(path: str) -> str:
    """The folder a file is counted under: its directory without the leading `src/` (include/ keeps its own name)."""
    parent = PurePosixPath(path).parent.as_posix()
    return parent[4:] if parent.startswith("src/") else parent


def count_text(text: str) -> Counter[str]:
    return Counter({name: len(rx.findall(text)) for name, rx in CLASSES.items()})


def tracked(root: Path, rev: str | None = None) -> list[str]:
    """The source files under src/ and include/: tracked in the work tree, or held by commit REV."""
    argv = ["ls-tree", "-r", "-z", "--name-only", rev, "--", *ROOTS] if rev else ["ls-files", "-z", "--", *ROOTS]
    done = subprocess.run(["git", "-C", str(root), *argv], capture_output=True, check=False)
    if rev and done.returncode:
        raise ValueError(f"{rev} is no commit of {root}")
    paths = done.stdout.decode(errors="replace").split("\0")
    return sorted(p for p in paths if p.lower().endswith(SUFFIXES))


def _texts(root: Path, rev: str, paths: list[str]) -> dict[str, str]:
    """{path: text at REV}, read in one `git cat-file --batch`."""
    done = subprocess.run(
        ["git", "-C", str(root), "cat-file", "--batch"],
        input="".join(f"{rev}:{p}\n" for p in paths).encode(),
        capture_output=True,
        check=False,
    )
    out, at, data = {}, 0, done.stdout
    for path in paths:
        end = data.index(b"\n", at)
        head = data[at:end].split()
        at = end + 1
        if len(head) == 3 and head[1] == b"blob":
            size = int(head[2])
            out[path] = data[at : at + size].decode(errors="replace")
            at += size + 1
    return out


def count(root: Path, only: str | None = None, rev: str | None = None) -> dict[str, Any]:
    """{"total", "classes", "folders": {folder: {"count", classes..., "files"}}} over the tracked tree, or over commit
    REV without a checkout (CLEAN-PLAN-O1 counted commits with its own git grep); `only` keeps the folders equal to it
    or under it (a leaf name such as MENU_2 also matches)."""
    folders: dict[str, Counter[str]] = {}
    files: Counter[str] = Counter()
    want = (only or "").strip("/").removeprefix("src/")
    paths = [
        p
        for p in tracked(root, rev)
        if not want or (f := folder_of(p)) == want or f.startswith(want + "/") or PurePosixPath(f).name == want
    ]
    held = _texts(root, rev, paths) if rev else {}
    for path in paths:
        folder = folder_of(path)
        try:
            text = held[path] if rev else (root / path).read_text(errors="replace")
        except (OSError, KeyError):
            continue
        folders.setdefault(folder, Counter()).update(count_text(text))
        files[folder] += 1
    total: Counter[str] = Counter()
    rows = {}
    for folder, got in sorted(folders.items()):
        total.update(got)
        rows[folder] = {"count": sum(got.values()), **{k: got[k] for k in CLASSES}, "files": files[folder]}
    return {"total": sum(total.values()), "classes": {k: total[k] for k in CLASSES}, "folders": rows}


def evidence(
    folders: Mapping[str, Mapping[str, Any]], ask: Callable[[list[str], list[str]], Mapping[str, Any]]
) -> dict[str, Any]:
    """Per folder (a row of `count`) and per step of EVIDENCE: `namable` (slots or targets a step packs with evidence)
    and `no_evidence`. `ask(steps, target_folders)` runs `recipe name count --steps S,.. --targets F..` once (one
    evidence build for every folder; a `next` per folder and step was 45 runs, 231 s) and returns its result. A folder
    with none of a step's placeholder classes is not counted for it; an include/ folder holds no function."""
    wanted = {
        folder: [step for step, (_what, classes) in EVIDENCE.items() if any(row.get(c) for c in classes)]
        for folder, row in folders.items()
        if not folder.startswith("include")
    }
    steps = [step for step in EVIDENCE if any(step in got for got in wanted.values())]
    found = ask(steps, ["src/" + f for f in wanted]) if steps else {}
    rows = found.get("folders", {}) if isinstance(found, Mapping) else {}
    out: dict[str, Any] = {folder: {} for folder in folders}
    for folder, mine in wanted.items():
        for step in mine:
            result = rows.get("src/" + folder, {}).get(step) or {}
            if "namable" in result:
                what = EVIDENCE[step][0]
                out[folder][what] = {
                    "namable": int(result["namable"]),
                    "no_evidence": int(result.get("no_evidence", 0)),
                }
    return out
