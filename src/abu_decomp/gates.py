"""`decomp cleanup count --rev R`: every exit gate of the cleanup story, counted on commit R in one run.

The project names its gates in the profile's `cleanup` section (`gates`: id, phase, name, `measure` + that measure's
settings; `scratch`: where the revision is unpacked); the measures below are generic. Every gate answers a number or
the reason it could not: nothing is estimated, and a gate that failed makes the run exit 1.

  placeholders  func_/D_/unk/... tokens (`classes`) in files matching `paths`, minus `exclude` (regexes on the path)
  regex         matches of `pattern` in the same files
  files         tracked files whose path matches `paths` and not `exclude`; `folders` lists the count per folder
  tags          struct/union/enum tags defined in more than one place (`records`: tags defined at all)
  externs       unit-local externs: `of`="header-declared" (a name some header declares) or "all"
  dead-decls    declarations nothing reads under `dir`, in `passes` layers
  unmapped      units without a row in the module map `map` (a row is found by the unit's path, else by its file name:
                a rename moves a file and the map keeps the old path)
  misfiled      units whose module folder (the module's path without its extension) is not the folder they sit in;
                `folders` lists the count per folder the unit sits in
  probe         commands in `argv` lists that fail (a tool the rig must have deployed)
  lines         lines of the JSON-lines `file` whose fields equal `where`
  length        length of the list at `key` of the JSON `file`
  marked        functions of the text `file` with a line matching `open` and no later line of theirs matching `closed`
  refused       percent of rows refused in the JSON-lines name-book submits `file`
  command       matches of `pattern` in the output of `argv` run in the unpacked revision
  style         `recipe style --full` findings on the unpacked revision, those of `rules` (all when none are named)
  roms          versions whose ROM, linked on a stamp-pool tree synced to the revision (`pool`: the shared tree the pool
                is seeded from, `config`: its data config, `wait`: seconds to wait for a free tree), is not the
                Makefile's SHA1; the revision is built at any commit and the train's own tree is never touched"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

from abu_decomp import cleanup
from abu_decomp.config import ConfigError

RECORD = re.compile(r"\b(?:struct|union|enum)\s+(\w+)\s*\{")


def unpack(root: Path, rev: str, scratch: Path, link: Mapping[str, str] | None = None) -> Path:
    """Commit REV of ROOT as a tree on disk under SCRATCH (git-initialised, LINK names symlinked in: what the project
    keeps out of git, the base ROMs); the caller removes it."""
    scratch.mkdir(parents=True, exist_ok=True)
    tree = Path(tempfile.mkdtemp(prefix="gates-", dir=scratch))
    packed = subprocess.run(["git", "-C", str(root), "archive", rev], capture_output=True, check=True)
    with tarfile.open(fileobj=BytesIO(packed.stdout)) as tar:
        tar.extractall(tree, filter="data")
    for name, target in (link or {}).items():
        (tree / name).symlink_to(Path(target).expanduser())
    subprocess.run(["git", "-C", str(tree), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tree), "add", "-A"], check=True, capture_output=True)
    return tree


@contextmanager
def at_rev(root: Path, rev: str, profile: Any = None) -> Iterator[Path]:
    """A tree holding exactly commit REV of ROOT, for a verb that reads files: the profile's cleanup scratch and links,
    removed after. An unknown REV raises ValueError."""
    if subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "-q", f"{rev}^{{commit}}"], capture_output=True
    ).returncode:
        raise ValueError(f"{rev} is no commit of {root}")
    section: Mapping[str, Any] = {}
    if profile is not None:
        with contextlib.suppress(ConfigError):
            section = profile.section("cleanup")
    tree = unpack(root, rev, Path(section.get("scratch", "~/rig-state/tmp")).expanduser(), section.get("link"))
    try:
        yield tree
    finally:
        shutil.rmtree(tree, ignore_errors=True)


class Revision:
    """One commit, read once: the source texts, and an unpacked tree for the measures that need files."""

    def __init__(self, root: Path, rev: str, scratch: Path, link: Mapping[str, str] | None = None, profile: Any = None):
        self.root, self.rev, self.scratch, self.link, self.profile = root, rev, scratch, link or {}, profile
        self._style: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._linked: dict[str, dict[str, Any]] | None = None
        self._linked_lock = threading.Lock()
        self._link_seconds: dict[str, float] = {}
        self.paths = cleanup.tracked(root, rev)
        self.texts = cleanup._texts(root, rev, self.paths)
        self._tree: Path | None = None

    @property
    def tree(self) -> Path:
        if self._tree is None:
            self._tree = unpack(self.root, self.rev, self.scratch, self.link)
        return self._tree

    def close(self) -> None:
        if self._tree is not None:
            shutil.rmtree(self._tree, ignore_errors=True)

    def select(self, spec: Mapping[str, Any]) -> dict[str, str]:
        keep = re.compile(spec.get("paths", ""), re.I)
        drop = re.compile(spec["exclude"], re.I) if spec.get("exclude") else None
        return {p: t for p, t in self.texts.items() if keep.search(p) and not (drop and drop.search(p))}


def _placeholders(rev: Revision, spec: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    texts, classes = rev.select(spec), spec.get("classes") or list(cleanup.CLASSES)
    names = {c: cleanup.distinct_text("", "")[c] for c in classes}
    total: Counter[str] = Counter()
    for path, text in texts.items():
        total.update({c: n for c, n in cleanup.count_text(text).items() if c in classes})
        for c, found in cleanup.distinct_text(path, text).items():
            if c in classes:
                names[c] |= found
    detail = {c: total[c] for c in classes}
    detail["distinct"] = sum(len(v) for v in names.values())
    return (detail["distinct"] if spec.get("distinct") else sum(total.values())), detail


def _regex(rev: Revision, spec: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    rx = re.compile(spec["pattern"], re.M)
    found = [(p, m) for p, t in rev.select(spec).items() for m in rx.findall(t)]
    return len(found), {"files": len({p for p, _ in found}), "distinct": len(set(found))}


def _files(rev: Revision, spec: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    keep = re.compile(spec.get("paths", ""), re.I)
    drop = re.compile(spec["exclude"], re.I) if spec.get("exclude") else None
    every = _all_files(rev.root, rev.rev)
    mine = [p for p in every if keep.search(p) and not (drop and drop.search(p))]
    return len(mine), {"examples": mine[:3], "folders": _by_folder(mine)}


def _by_folder(paths: Iterable[str]) -> dict[str, int]:
    """{folder: files} for PATHS, the largest folder first (the folder as `cleanup count` names it)."""
    got = Counter(cleanup.folder_of(p) for p in paths)
    return dict(sorted(got.items(), key=lambda kv: (-kv[1], kv[0])))


def _all_files(root: Path, rev: str) -> list[str]:
    done = subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "-z", "--name-only", rev], capture_output=True)
    return sorted(p for p in done.stdout.decode(errors="replace").split("\0") if p)


def _tags(rev: Revision, spec: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    where: dict[str, int] = Counter()
    for text in rev.select(spec).values():
        where.update(RECORD.findall(text))
    return sum(1 for n in where.values() if n > 1), {"records": len(where), "definitions": sum(where.values())}


def _externs(rev: Revision, spec: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    from abu_decomp import externs

    found = externs.scan(rev.tree, externs.tracked(rev.tree))
    declared = {i for i in found.externs if i in found.in_headers}
    mine = declared if spec.get("of") == "header-declared" else set(found.externs)
    return sum(len(found.externs[i]) for i in mine), {
        "names": len(mine),
        "all_lines": sum(map(len, found.externs.values())),
    }


def _dead(rev: Revision, spec: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    from abu_decomp import deaddecls

    got = deaddecls.run(rev.tree, spec["dir"])
    return int(got["dead"]), {"headers": got["headers"], "units": got["units"], "passes": got["passes"]}  # type: ignore[call-overload]


def _units(rev: Revision, spec: Mapping[str, Any]) -> tuple[list[str], dict[str, list[str]]]:
    """The units of the revision (C and C++ sources under src/) and, for each, the modules the map `map` gives it: its
    own row by path, else the rows of its file name (a rename moves a file; the map keeps the old path). A unit
    nothing in the map names gets none."""
    done = subprocess.run(
        ["git", "-C", str(rev.root), "show", f"{rev.rev}:{spec['map']}"], capture_output=True, text=True
    )
    by_path: dict[str, str] = {}
    by_name: dict[str, list[str]] = defaultdict(list)
    for line in done.stdout.splitlines() if done.returncode == 0 else ():  # no map yet: every unit lacks its row
        unit, _, rest = line.partition("\t")
        unit = unit.strip()
        if not unit or unit.startswith("#") or unit == "unit":
            continue
        module = rest.partition("\t")[0].strip()
        by_path[unit] = module
        by_name[PurePosixPath(unit).name].append(module)
    units = [p for p in rev.paths if p.startswith("src/") and p.lower().endswith((".c", ".cpp"))]
    return units, {u: [by_path[u]] if u in by_path else by_name.get(PurePosixPath(u).name, []) for u in units}


def _unmapped(rev: Revision, spec: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    units, modules = _units(rev, spec)
    return sum(1 for u in units if not modules[u]), {"units": len(units), "rows": sum(1 for u in units if modules[u])}


def _misfiled(rev: Revision, spec: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    units, modules = _units(rev, spec)
    mapped = [u for u in units if modules[u]]
    bad = [
        u
        for u in mapped
        if PurePosixPath(u).parent.as_posix() not in {PurePosixPath(m).with_suffix("").as_posix() for m in modules[u]}
    ]
    return len(bad), {"units": len(mapped), "unmapped": len(units) - len(mapped), "folders": _by_folder(bad)}


def _probe(rev: Revision, spec: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    bad = [a for a in spec["argv"] if subprocess.run(a, capture_output=True, check=False).returncode]
    return len(bad), {"probed": len(spec["argv"]), "failing": [a[0] + " " + " ".join(a[1:3]) for a in bad]}


def _jsonl(path: str) -> list[dict[str, Any]]:
    return cleanup._lines(Path(path).expanduser())


def _lines(rev: Revision, spec: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    where = spec.get("where", {})
    return sum(1 for r in _jsonl(spec["file"]) if all(r.get(k) == v for k, v in where.items())), {}


def _length(rev: Revision, spec: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    doc = json.loads(Path(spec["file"]).expanduser().read_text(encoding="utf-8"))
    return len(doc[spec["key"]]), {"ts": doc.get("ts", "")}


def _marked(rev: Revision, spec: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    opened, closed = re.compile(spec["open"]), re.compile(spec["closed"]) if spec.get("closed") else None
    state: dict[str, bool] = {}
    for line in Path(spec["file"]).expanduser().read_text(encoding="utf-8").splitlines():
        name = line.split(None, 1)[0] if line.strip() else ""
        if name and opened.search(line):
            state[name] = True
        elif name and closed and closed.search(line) and name in state:
            state[name] = False
    return sum(state.values()), {"named": len(state)}


def _refused(rev: Revision, spec: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    rows = _jsonl(spec["file"])
    sent, refused = sum(r["rows"] for r in rows), sum(r["refused"] for r in rows)
    return round(100 * refused / sent) if sent else 0, {"rows": sent, "refused": refused}


def _command(rev: Revision, spec: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    done = subprocess.run(spec["argv"], cwd=rev.tree, capture_output=True, text=True, check=False)
    return len(re.findall(spec["pattern"], done.stdout + done.stderr, re.M)), {"exit": done.returncode}


def _styled(rev: Revision, spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    with rev._lock:  # the gates that read the style findings share one run
        if rev._style is None:
            argv = [*spec.get("argv", ["recipe"]), "--project", str(rev.tree), "style", "--full", "--json"]
            done = subprocess.run(argv, capture_output=True, text=True, check=False)
            try:
                rev._style = json.loads(done.stdout)
            except ValueError:
                raise RuntimeError(
                    f"recipe style answered no JSON (exit {done.returncode}): {done.stderr[-200:]}"
                ) from None
        return rev._style["findings"]


def _style(rev: Revision, spec: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    found = _styled(rev, spec)
    mine = [f for f in found if not spec.get("rules") or f.get("rule") in spec["rules"]]
    return len(mine), {"rules": dict(Counter(f.get("rule") for f in mine).most_common(5)), "all": len(found)}


def _link(rev: Revision, spec: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The revision's ROMs, once for all `roms` gates: a pool tree leased, synced to the revision, every version
    linked."""
    from abu_decomp import shipproof
    from abu_decomp.data import config as config_mod
    from abu_decomp.data import stampbuild, trial

    with rev._linked_lock:
        if rev._linked is None:
            pool = Path(spec["pool"]).expanduser()
            sha = subprocess.run(
                ["git", "-C", str(rev.root), "rev-parse", rev.rev], capture_output=True, text=True, check=True
            ).stdout.strip()
            if subprocess.run(["git", "-C", str(pool), "cat-file", "-e", f"{sha}^{{commit}}"]).returncode:
                subprocess.run(["git", "-C", str(pool), "fetch", "-q", str(rev.root), sha], check=True)
            config = Path(spec["config"]).expanduser() if spec.get("config") else None
            cfg = config_mod.load(config, pool)
            began = time.monotonic()
            with stampbuild.at_rev(cfg, rev.profile, pool, sha, float(spec.get("wait", 600))) as tree:
                leased = time.monotonic()
                trial.Builder(cfg, tree).rom_check()  # links every version; the report below judges the ROMs themselves
                rev._linked = shipproof.rom_report(tree)
                rev._link_seconds = {"tree": round(leased - began, 1), "link": round(time.monotonic() - leased, 1)}
        return rev._linked


def _roms(rev: Revision, spec: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    report = _link(rev, spec)
    if not report:
        raise RuntimeError("the revision's Makefile names no ROM SHA1 to compare")
    return sum(1 for r in report.values() if not r["ok"]), {
        "versions": len(report),
        "exact": sorted(v for v, r in report.items() if r["ok"]),
        "seconds": rev._link_seconds,
    }


MEASURES: dict[str, Callable[[Revision, Mapping[str, Any]], tuple[int, dict[str, Any]]]] = {
    "placeholders": _placeholders,
    "regex": _regex,
    "files": _files,
    "tags": _tags,
    "externs": _externs,
    "dead-decls": _dead,
    "unmapped": _unmapped,
    "misfiled": _misfiled,
    "probe": _probe,
    "lines": _lines,
    "length": _length,
    "marked": _marked,
    "refused": _refused,
    "command": _command,
    "style": _style,
    "roms": _roms,
}


def measure(rev: Revision, gate: Mapping[str, Any]) -> dict[str, Any]:
    """One gate row: {id, phase, name, value, detail} or {id, ..., error}."""
    row = {k: gate.get(k, "") for k in ("id", "phase", "name")}
    spec = gate["measure"]
    started = time.monotonic()
    try:
        value, detail = MEASURES[spec["kind"]](rev, spec)
    except Exception as e:  # a gate that cannot be counted says why; its siblings still run
        return {**row, "error": f"{type(e).__name__}: {e}"[:300]}
    return {**row, "value": value, "detail": detail, "seconds": round(time.monotonic() - started, 1)}


def gate_lines(rows: list[dict[str, Any]], top: int = 0) -> list[str]:
    """The plain report of gate rows: one line each, then the largest TOP folders (0 = every one) of a gate that counts
    per folder (`detail.folders`)."""
    out = []
    for g in rows:
        got = g.get("value", "ERROR " + g.get("error", ""))
        detail = {k: v for k, v in g.get("detail", {}).items() if k != "folders"}
        out.append(f"  {g['phase']:<3} {got!s:>8}  {g['name']}  {detail or ''}")
        folders = list((g.get("detail") or {}).get("folders", {}).items())
        if folders:
            shown = folders if top == 0 else folders[:top]
            out.append(f"      {len(folders)} folders: " + " ".join(f"{k}={v}" for k, v in shown))
    return out


def run(root: Path, rev: str, section: Mapping[str, Any], profile: Any = None) -> dict[str, Any]:
    """Every gate of `section` on commit `rev`: {"rev", "seconds", "gates": [...], "failed": n}. The ROM gates start
    first: their link is the one long step and the others count while it runs."""
    started = time.monotonic()
    book = Revision(
        root, rev, Path(section.get("scratch", "~/rig-state/tmp")).expanduser(), section.get("link"), profile
    )
    gates = list(section["gates"])
    try:
        with ThreadPoolExecutor(max_workers=int(section.get("workers", 8))) as pool:
            futures = {i: pool.submit(measure, book, g) for i, g in enumerate(gates) if g["measure"]["kind"] == "roms"}
            if any(g["measure"]["kind"] in ("externs", "dead-decls", "command", "style") for g in gates):
                _ = book.tree  # unpack once, before the gates that read it share it
            futures |= {i: pool.submit(measure, book, g) for i, g in enumerate(gates) if i not in futures}
            rows = [futures[i].result() for i in range(len(gates))]
    finally:
        book.close()
    return {
        "rev": rev,
        "seconds": round(time.monotonic() - started, 1),
        "gates": rows,
        "failed": sum(1 for r in rows if "error" in r),
    }
