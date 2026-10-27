"""`decomp cleanup slices`: who owns each placeholder target (one `recipe name slices` run, see `slices_argv`).

`decomp cleanup count`: the placeholder names a cleanup pass removes, per folder, from one shared set of patterns.

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
LOCAL = frozenset({"sp", "var", "arg"})  # a slot is scoped by its function: distinct names are counted per file
SUFFIXES = (".c", ".cpp", ".h")
ROOTS = ("src/", "include/")


def slices_argv(lanes: int | None, module_map: str | None, assign: list[str], out: str | None) -> list[str]:
    """The `recipe name slices` arguments: the slicer lives with the evidence (Recipe Maker); this verb is its front, so
    the lane count, the pluggable module map (unit<TAB>module lines, MODULES.tsv when it exists) and the lane
    assignments are all it adds."""
    argv = ["name", "slices"]
    if lanes:
        argv += ["--lanes", str(lanes)]
    if module_map:
        argv += ["--map", module_map]
    for pair in assign:
        argv += ["--assign", pair]
    if out:
        argv += ["--out", out]
    return argv


def slices_lines(document: Mapping[str, Any]) -> list[str]:
    """The report of a slices run: the counts, then where the document and the per-lane ownership files went."""
    return [*document.get("summary", []), f"slices {document.get('path', '?')}", *document.get("lane_files", [])]


def folder_of(path: str) -> str:
    """The folder a file is counted under: its directory without the leading `src/` (include/ keeps its own name)."""
    parent = PurePosixPath(path).parent.as_posix()
    return parent[4:] if parent.startswith("src/") else parent


def count_text(text: str) -> Counter[str]:
    return Counter({name: len(rx.findall(text)) for name, rx in CLASSES.items()})


def distinct_text(path: str, text: str) -> dict[str, set[Any]]:
    """The distinct placeholder names of one file per class: a name, or (file, name) for the slot classes."""
    out: dict[str, set[Any]] = {}
    for name, rx in CLASSES.items():
        found = set(rx.findall(text))
        out[name] = {(path, t) for t in found} if name in LOCAL else found
    return out


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


def count(root: Path, only: str | None = None, rev: str | None = None, distinct: bool = False) -> dict[str, Any]:
    """{"total", "classes", "folders": {folder: {"count", classes..., "files"}}} over the tracked tree, or over commit
    REV without a checkout (CLEAN-PLAN-O1 counted commits with its own git grep); `only` keeps the folders equal to it
    or under it (a leaf name such as MENU_2 also matches). `distinct` adds the number of different names per class
    ("distinct": {"total", "classes"}, and per folder): a rename pass removes names, not occurrences."""
    folders: dict[str, Counter[str]] = {}
    files: Counter[str] = Counter()
    names: dict[str, dict[str, set[Any]]] = {}  # folder -> class -> distinct names (only with `distinct`)
    want = (only or "").strip("/").removeprefix("src/")
    if want in ("src", "include"):  # a whole root: every folder of it (`--dir src` matched no folder and counted 0)
        paths = [p for p in tracked(root, rev) if p.startswith(want + "/")]
    elif want.startswith("include/"):
        paths = [
            p for p in tracked(root, rev) if p.startswith(want + "/") or PurePosixPath(p).parent.as_posix() == want
        ]
    else:
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
        if distinct:
            mine = names.setdefault(folder, {k: set() for k in CLASSES})
            for k, found in distinct_text(path, text).items():
                mine[k] |= found
    total: Counter[str] = Counter()
    rows = {}
    for folder, got in sorted(folders.items()):
        total.update(got)
        rows[folder] = {"count": sum(got.values()), **{k: got[k] for k in CLASSES}, "files": files[folder]}
    out: dict[str, Any] = {"total": sum(total.values()), "classes": {k: total[k] for k in CLASSES}, "folders": rows}
    if distinct:
        every: dict[str, set[Any]] = {k: set() for k in CLASSES}
        for folder, mine in names.items():
            rows[folder]["distinct"] = sum(len(v) for v in mine.values())
            for k, found in mine.items():
                every[k] |= found
        out["distinct"] = {
            "total": sum(len(v) for v in every.values()),
            "classes": {k: len(v) for k, v in every.items()},
        }
    return out


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


def _stamp(ts: str) -> float:
    import time

    return time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))


def _lines(path: Path) -> list[dict[str, Any]]:
    import json

    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def show_slice(names: Path, lane: str) -> dict[str, Any]:
    """The ownership file of one lane (`<names>/slices/<lane>.json`): its slice ids and the function, datum and record
    keys it owns. A DecompError naming the lanes that have a file when `lane` has none."""
    import json

    from abu_decomp import DecompError

    path = names / "slices" / f"{lane}.json"
    if not path.is_file():
        have = sorted(p.stem for p in (names / "slices").glob("*.json")) if (names / "slices").is_dir() else []
        raise DecompError("cleanup.show", f"no slice for lane {lane} in {names / 'slices'}; lanes with one: {have}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {
        "lane": lane,
        "slice": doc.get("slice", ""),
        **{k: list(doc.get(k, ())) for k in ("fns", "data", "records")},
    }


def hourly(names: Path, day: str | None, now: float) -> dict[str, dict[str, dict[str, int]]]:
    """{lane: {hour: {landed, returned, refused}}} for the hours of `day` (YYYY-MM-DD) or the last 24 hours: rows of
    a landed or returned batch (the lane of each row from the ledger) and rows a submit refused."""
    lane_of = {r["seq"]: r["lane"] for r in _lines(names / "ledger.jsonl")}
    since = now - 24 * 3600

    def hour(ts: str) -> str | None:
        return ts[:13] if (ts.startswith(day) if day else _stamp(ts) >= since) else None

    table: dict[str, dict[str, dict[str, int]]] = {}

    def add(lane: str, ts: str, field: str, n: int) -> None:
        if n and (h := hour(ts)):
            table.setdefault(lane, {}).setdefault(h, {"landed": 0, "returned": 0, "refused": 0})[field] += n

    for e in _lines(names / "events.jsonl"):
        if e.get("event") in ("landed", "returned") and e.get("ts"):
            for seq in e.get("seqs", ()):
                add(lane_of.get(seq, "?"), e["ts"], e["event"], 1)
    for sub in _lines(names / "submits.jsonl"):
        add(sub["lane"], sub["ts"], "refused", sub["refused"])
    return table


def train_rounds(history: Path, day: str | None, now: float) -> dict[str, Any]:
    """The train's rounds (`train/history.jsonl`, dry runs left out) of `day` (YYYY-MM-DD) or the last 24 hours:
    inputs per round, landed and dropped, the gap between rounds in minutes, and rounds per hour. N is tuned from
    inputs per round."""
    import statistics

    def when(ts: str) -> float:
        import time

        return time.mktime(time.strptime(ts, "%Y%m%d-%H%M%S"))

    rounds = [
        r
        for r in _lines(history)
        if not r.get("dry") and (r["ts"].startswith(day.replace("-", "")) if day else when(r["ts"]) >= now - 24 * 3600)
    ]
    per = [len(r["results"]) for r in rounds]
    status = Counter(x["status"] for r in rounds for x in r["results"])
    gaps = sorted((when(b["ts"]) - when(a["ts"])) / 60 for a, b in itertools.pairwise(rounds))
    hours = Counter(f"{r['ts'][:4]}-{r['ts'][4:6]}-{r['ts'][6:8]}T{r['ts'][9:11]}" for r in rounds)
    return {
        "rounds": len(rounds),
        "status": dict(status),
        "inputs_per_round": {"median": statistics.median(per) if per else 0, "max": max(per, default=0)},
        "gap_minutes": {"median": round(statistics.median(gaps), 1), "p90": round(gaps[int(len(gaps) * 0.9)], 1)}
        if gaps
        else {},
        "rounds_per_hour": dict(sorted(hours.items())),
    }


def ledger(
    names: Path, hours: float = 1.0, now: float | None = None, day: str | None = None, history: Path | None = None
) -> dict[str, Any]:
    """WATCH's view of the name ledger (`names` = ~/rig-state/names): queue and batch counts, per lane the rows
    submitted, accepted and refused in the last `hours` (with the refusing rules), rows accepted per hour, and per slice
    how many of its functions have an accepted row. Read from the ledger, submits, events and slices files; nothing
    is written."""
    import json
    import time

    now = now or time.time()
    rows = _lines(names / "ledger.jsonl")
    state = {r["seq"]: "queued" for r in rows}
    batches: dict[int, str] = {}
    for e in _lines(names / "events.jsonl"):
        batch = e.get("batch", 0)
        word = {"made": "inflight", "landed": "landed", "returned": "returned", "requeued": "queued"}.get(
            e.get("event", "")
        )
        for seq in e.get("seqs", ()):
            if word and seq in state:
                state[seq] = word
        if e.get("event") in ("made", "landed"):
            batches[batch] = e["event"]
    window: dict[str, dict[str, Any]] = {}
    for sub in _lines(names / "submits.jsonl"):
        if now - _stamp(sub["ts"]) > hours * 3600:
            continue
        lane = window.setdefault(sub["lane"], {"submitted": 0, "accepted": 0, "refused": 0, "by_rule": {}})
        lane["submitted"] += sub["rows"]
        lane["accepted"] += sub["accepted"]
        lane["refused"] += sub["refused"]
        for rule, n in sub.get("by_rule", {}).items():
            lane["by_rule"][rule] = lane["by_rule"].get(rule, 0) + n
    for lane in window.values():
        lane["rows_per_hour"] = round(lane["accepted"] / hours, 1)
    slices = {}
    for path in sorted((names / "slices").glob("*.json")) if (names / "slices").is_dir() else []:
        doc = json.loads(path.read_text(encoding="utf-8"))
        owned = set(doc.get("fns", ()))
        named = {r["key"] for r in rows if r["key"] in owned and state.get(r["seq"]) != "returned"}
        slices[path.stem] = {"slice": doc.get("slice", ""), "owned_fns": len(owned), "named_fns": len(named)}
    tally = Counter(state.values())
    return {
        "hours": hours,
        "rows": {k: tally.get(k, 0) for k in ("queued", "inflight", "landed", "returned")},
        "batches": {
            "made": sum(1 for v in batches.values() if v == "made"),
            "landed": sum(1 for v in batches.values() if v == "landed"),
        },
        "lanes": window,
        "slices": slices,
        "hourly": hourly(names, day, now),
        "train": train_rounds(history or names.parent / "train" / "history.jsonl", day, now),
    }
