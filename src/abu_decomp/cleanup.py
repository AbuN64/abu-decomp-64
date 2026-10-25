"""`decomp cleanup count`: the placeholder names a cleanup pass removes, per folder, from one shared set of patterns.

Lanes and the orchestrator's WATCH report the same numbers when they all run this one verb: a placeholder is a
function (`func_<8 hex>`), a datum (`D_<8 hex>`), an unnamed member (`unk<hex>`), a padding member (`pad<hex>`) or a
stack slot (`sp<hex>`), counted per token occurrence in the tracked C sources and headers."""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

CLASSES: dict[str, re.Pattern[str]] = {
    "func": re.compile(r"\bfunc_[0-9A-F]{8}"),
    "data": re.compile(r"\bD_[0-9A-F]{8}"),
    "unk": re.compile(r"\bunk[0-9A-Fa-f_]+"),
    "pad": re.compile(r"\bpad[0-9A-Fa-f]+"),
    "sp": re.compile(r"\bsp[0-9A-F]{2,}\b"),
}
SUFFIXES = (".c", ".cpp", ".h")
ROOTS = ("src/", "include/")


def folder_of(path: str) -> str:
    """The folder a file is counted under: its directory without the leading `src/` (include/ keeps its own name)."""
    parent = PurePosixPath(path).parent.as_posix()
    return parent[4:] if parent.startswith("src/") else parent


def count_text(text: str) -> Counter[str]:
    return Counter({name: len(rx.findall(text)) for name, rx in CLASSES.items()})


def tracked(root: Path) -> list[str]:
    done = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", *ROOTS], capture_output=True, check=False)
    paths = done.stdout.decode(errors="replace").split("\0")
    return sorted(p for p in paths if p.lower().endswith(SUFFIXES))


def count(root: Path, only: str | None = None) -> dict[str, Any]:
    """{"total", "classes", "folders": {folder: {"count", classes..., "files"}}} over the tracked tree; `only` keeps the
    folders equal to it or under it (a leaf name such as MENU_2 also matches)."""
    folders: dict[str, Counter[str]] = {}
    files: Counter[str] = Counter()
    want = (only or "").strip("/").removeprefix("src/")
    for path in tracked(root):
        folder = folder_of(path)
        if want and not (folder == want or folder.startswith(want + "/") or PurePosixPath(folder).name == want):
            continue
        try:
            text = (root / path).read_text(errors="replace")
        except OSError:
            continue
        folders.setdefault(folder, Counter()).update(count_text(text))
        files[folder] += 1
    total: Counter[str] = Counter()
    rows = {}
    for folder, got in sorted(folders.items()):
        total.update(got)
        rows[folder] = {"count": sum(got.values()), **{k: got[k] for k in CLASSES}, "files": files[folder]}
    return {"total": sum(total.values()), "classes": {k: total[k] for k in CLASSES}, "folders": rows}
