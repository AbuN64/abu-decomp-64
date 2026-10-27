"""`decomp cleanup slices` file sets: every source and header file of the tree has one owner lane, or is GLOBALS.

The placeholder slices say who may name a function; the file sets say who may edit a file, so lanes working at once
never touch the same file. The rule is one for every project (project facts come in through the module map):
- a source file belongs to its module (the map's `unit<TAB>module`; a unit the map lacks, or no map at all, makes its
  folder the module), a module's header belongs to the module, whoever includes it;
- any other header belongs to the one module that reads it (a source or a module header includes it, directly or
  through unowned headers); read by two or more modules it is GLOBALS, which only tool batches write;
- a header nobody reads goes with the lane that holds most of its folder;
- a module lives in one lane: kept from the last run while its lane exists, else the lightest lane takes it.

The sets are `<names>/files/<LANE>.json` (and `GLOBALS.json`), beside the slice files. A lane with no file set is a
tool batch. The train's `unbake lanecheck` reads these files and refuses a hand lane's input that touches a file of
another lane or of GLOBALS, naming the owner."""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from abu_decomp import files

GLOBALS = "GLOBALS"
INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*[<"]([^>"\n]+)[>"]', re.MULTILINE)
INCLUDE_ROOTS = ("include/", "")


def store() -> Path:
    """The names directory (the name book's store: NAMES_STATE, else ~/rig-state/names)."""
    return Path(os.environ.get("NAMES_STATE") or "~/rig-state/names").expanduser()


def directory(names: Path) -> Path:
    return names / "files"


def _resolver(paths: Collection[str]) -> Any:
    exact = set(paths)
    folded: dict[str, str | None] = {}
    for p in sorted(paths):
        folded[p.lower()] = None if p.lower() in folded else p  # a folded name two files share resolves to neither

    def find(name: str) -> str | None:
        if name in exact:
            return name
        return folded.get(name.lower())

    return find


def includes(text: str) -> list[str]:
    return INCLUDE.findall(text)


def _included(path: str, text: str, find: Any) -> list[str]:
    """The tracked files `text` of `path` includes: next to it, under include/, or from the root."""
    here = PurePosixPath(path).parent
    out = []
    for name in includes(text):
        candidates = [PurePosixPath(here, name).as_posix(), *(root + name for root in INCLUDE_ROOTS)]
        found = next((hit for hit in map(find, candidates) if hit), None)
        if found and found != path:
            out.append(found)
    return out


def partition(
    sizes: Mapping[str, int],
    texts: Mapping[str, str],
    unit_module: Mapping[str, str],
    module_header: Mapping[str, str],
    lanes: Sequence[str],
    kept: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The file sets of `lanes` over the files `sizes` (path -> bytes): {lanes: {lane: {modules, files}}, globals:
    {file: [reading modules]}, modules: {module: lane}}. `kept` is the module -> lane of the last run."""
    if not lanes:
        raise ValueError("a partition needs at least one lane")
    find = _resolver(sizes)
    module_of: dict[str, str] = {}
    for path in sorted(sizes):
        if not path.lower().endswith(".h"):
            hit = next((u for u in (find(path),) if u), path)
            module_of[hit] = "dir:" + PurePosixPath(hit).parent.as_posix()
    for unit, module in unit_module.items():
        if (hit := find(unit)) and hit in module_of:
            module_of[hit] = module
    owned: dict[str, str] = {}
    for module, header in sorted(module_header.items()):
        if (hit := find(header)) and hit not in owned:
            owned[hit] = module
    readers: dict[str, set[str]] = defaultdict(set)
    chain: dict[str, list[str]] = {}  # an unowned header -> the headers it includes: its readers read them too
    for path in sorted(sizes):
        found = _included(path, texts.get(path, ""), find)
        if path in module_of:
            for header in found:
                readers[header].add(module_of[path])
        elif path in owned:
            for header in found:
                readers[header].add(owned[path])
        else:
            chain[path] = found
    changed = True
    while changed:  # readers pass down the unowned headers until nothing grows
        changed = False
        for header, below in chain.items():
            for lower in below:
                if lower not in owned and not readers[lower] >= readers[header]:
                    readers[lower] |= readers[header]
                    changed = True
    owner: dict[str, str] = {**module_of, **owned}
    shared: dict[str, list[str]] = {}
    unread: list[str] = []
    for path in sorted(sizes):
        if path in owner:
            continue
        reading = readers.get(path, set())
        if len(reading) == 1:
            owner[path] = next(iter(reading))
        elif reading:
            shared[path] = sorted(reading)
        else:
            unread.append(path)
    weight: Counter[str] = Counter()
    for path, module in owner.items():
        weight[module] += max(sizes[path], 1)
    load: Counter[str] = Counter({lane: 0 for lane in lanes})
    lane_of: dict[str, str] = {}
    for module in sorted(weight):
        if (previous := (kept or {}).get(module)) in load:
            lane_of[module] = previous
            load[previous] += weight[module]
    for module in sorted(set(weight) - set(lane_of), key=lambda m: (-weight[m], m)):
        lane = min(lanes, key=lambda x: (load[x], x))
        lane_of[module] = lane
        load[lane] += weight[module]
    where: dict[str, str] = {path: lane_of[module] for path, module in owner.items()}
    near: dict[str, Counter[str]] = defaultdict(Counter)
    for path, lane in where.items():
        near[PurePosixPath(path).parent.as_posix()][lane] += 1
    for path in unread:
        folder = PurePosixPath(path).parent.as_posix()
        lane = near[folder].most_common(1)[0][0] if near[folder] else min(lanes, key=lambda x: (load[x], x))
        where[path] = lane
        load[lane] += max(sizes[path], 1)
    sets: dict[str, dict[str, Any]] = {lane: {"modules": [], "files": []} for lane in lanes}
    for module, lane in sorted(lane_of.items()):
        sets[lane]["modules"].append(module)
    for path, lane in sorted(where.items()):
        sets[lane]["files"].append(path)
    return {"lanes": sets, "globals": shared, "modules": dict(sorted(lane_of.items()))}


def check(doc: Mapping[str, Any], sizes: Mapping[str, int]) -> list[str]:
    """What is wrong with a partition: a file in two sets (or in a set and GLOBALS), a file in none."""
    seen: Counter[str] = Counter()
    for got in doc["lanes"].values():
        seen.update(got["files"])
    seen.update(list(doc["globals"]))
    return [f"{p} is in {seen[p]} sets" for p in sorted(seen) if seen[p] > 1] + [
        f"{p} is in no set" for p in sorted(set(sizes) - set(seen))
    ]


def slice_lanes(names: Path) -> list[str]:
    """The lanes holding placeholder slices in NAMES."""
    return sorted(p.stem for p in (names / "slices").glob("*.json")) if (names / "slices").is_dir() else []


def lane_names(names: Path, count: int, earlier: Sequence[str] = ()) -> list[str]:
    """`count` lane names: the lanes the planner assigned slices to, now or before the slicer ran (sorted, as many as
    fit), then L01, L02, ... for the lanes still missing. The file sets and the placeholder slices use the same
    names, and a slicer run that regroups the slices never drops a lane that already had a file set."""
    out = sorted({*slice_lanes(names), *earlier})[:count]
    extra = iter(range(1, count + 1))
    while len(out) < count:
        name = f"L{next(extra):02d}"
        if name not in out:
            out.append(name)
    return out


def write(names: Path, doc: Mapping[str, Any], readers: Mapping[str, Sequence[str]] | None = None) -> list[Path]:
    """`<names>/files/<LANE>.json` for every lane, `GLOBALS.json`, and `<names>/files.json` (the module -> lane map the
    next run keeps); a lane file of a lane that is gone is removed."""
    folder = directory(names)
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for lane, got in doc["lanes"].items():
        target = folder / f"{lane}.json"
        files.atomic_json(target, {"lane": lane, **got})
        written.append(target)
    target = folder / f"{GLOBALS}.json"
    files.atomic_json(target, {"lane": GLOBALS, "files": sorted(doc["globals"]), "readers": doc["globals"]})
    written.append(target)
    for old in folder.glob("*.json"):
        if old not in written:
            old.unlink()
    files.atomic_json(names / "files.json", {"lanes": list(doc["lanes"]), "modules": doc["modules"]})
    return written


def load(names: Path, lane: str) -> dict[str, Any] | None:
    try:
        return dict(json.loads((directory(names) / f"{lane}.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def kept(names: Path) -> dict[str, str]:
    try:
        return dict(json.loads((names / "files.json").read_text(encoding="utf-8")).get("modules", {}))
    except (OSError, ValueError):
        return {}


def build(
    root: Path, names: Path, count: int | None, module_map: str | None, earlier: Sequence[str] = ()
) -> dict[str, Any]:
    """The partition of the tracked source and header files of ROOT over `count` lanes (default: the lanes holding
    slices, now or in EARLIER). The module map is MODULE_MAP read from ROOT when it names a file there; without one,
    every folder is a module. Raises ValueError when the partition is not exclusive."""
    from abu_decomp import cleanup, owners

    paths = cleanup.tracked(root)
    sizes: dict[str, int] = {}
    texts: dict[str, str] = {}
    for path in paths:
        try:
            text = (root / path).read_text(errors="replace")
        except OSError:
            continue
        sizes[path] = len(text)
        texts[path] = text
    target = (root / module_map) if module_map else None
    unit_module, module_header = owners.read_map(target) if target and target.is_file() else ({}, {})
    held = lane_names(names, count or max(1, len({*slice_lanes(names), *earlier})), earlier)
    doc = partition(sizes, texts, unit_module, module_header, held, kept(names))
    if wrong := check(doc, sizes):
        raise ValueError("; ".join(wrong[:5]))
    doc["map"] = str(target) if unit_module else None
    doc["files"] = len(sizes)
    return doc


def lines(doc: Mapping[str, Any]) -> list[str]:
    """The report of a partition: per lane its modules and files, then GLOBALS."""
    out = [
        f"file sets: {doc['files']} files over {len(doc['lanes'])} lanes (map {doc['map'] or 'none: folder tree'}),"
        f" {len(doc['globals'])} in GLOBALS"
    ]
    out += [f"  {lane}: {len(got['modules'])} modules, {len(got['files'])} files" for lane, got in doc["lanes"].items()]
    return out
