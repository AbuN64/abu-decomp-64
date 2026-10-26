"""Which game code the recorded movies reach: gcov line/branch/function coverage per run and unioned, plus the oracle.

Port side: the page's port is a gcov build (cmake RAGE_DECOMP_COVERAGE: the original units compiled with GCC's
`--coverage`, libgcov linked, counters dumped on every exit path by decomp/coverage.c). Each `tas play` sets
`GCOV_PREFIX` to RUN/gcov, so a run keeps its own .gcda; `run_report` turns them into RUN/coverage.json with
`gcovr --json`, and `build` unions every movie's newest run with `gcovr --add-tracefile` into one `--html-details`
report (runs/coverage/index.html, served by `tas serve` at /coverage/): per file and line, line/branch/function
percentages, source annotated. Nothing here renders HTML by hand.

Oracle side: the recompiled original (cake-taste host) replays the same movie with `N64R_GOLDEN_DIR`; its
`trace-final.tsv` (symbol, first VI, thread, count) is the per-function first-entry record the host already writes.
The join with the port is by ROM address: a function's identity is the address the reference version's ELF gives it, and
every name a side reports (the port's gcov names, the original host's names) resolves to it through the source's address
tables (`symbols`), so a rename never loses coverage; a row carries the current name and the
`address`. Not a gcov build of the recomp: see RESULT.
Behaviour: the host also writes cumulative entry counts every shot_every polls (`trace-poll-N.tsv`); `behaviour`
judges each function by the port's gcov count against the original's counts around the port's last poll, so the
table says "executed and behaves like the original", not only "executed" (granularity: TAS-CORPUS-OPUS-1 DESIGN.md).

The universe for percentages by bytes is every function of the version's ELF (`readelf -sW`, MIPS sizes), keyed by
name with the versions that define it. A row is scope `sdk` when its file is under `src/sdk/` (the port replaces
those with its shim), other when no source defines it (compiler runtime, asm-only), else game.
Section `tas.coverage`: {"elf": ".../build/{version}/RageWars.elf", "version":
"us-rev1", "versions": [...], "source": folder with src/ (default: tas.serve.pin), "symbols": [address tables in
source, first file names win; e.g. "versions/us-rev1/symbol_addrs.txt", ".../aliases.txt"], "since": epoch seconds
(default 0): the union counts only runs that started at or after it, so a new epoch restarts coverage from nothing,
"oracle": {"argv": [...], "env": {...}} with {movie}, {dir}, {trace} in values}.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, remap, slots, tas_oracle, tas_symbolize
from abu_decomp.config import Profile, check_keys

COVERAGE, GCOV, ORACLE_DIR, TABLE, HTML = "coverage.json", "gcov", "oracle-golden", "coverage-table.json", "coverage"
_KEYS = frozenset({"elf", "version", "versions", "source", "symbols", "since"})
_SYMBOL = re.compile(r"^\s*\d+:\s+([0-9a-fA-F]+)\s+(\d+)\s+FUNC\s+\w+\s+\w+\s+\S+\s+(\S+)$")
_DEFINITION = re.compile(r"^(?:[A-Za-z_][\w]*[\s\*]+)+\**([A-Za-z_]\w*)\s*\([^;]*$")
_INCLUDE_ASM = re.compile(r"INCLUDE_ASM\s*\([^,]*,\s*([A-Za-z_]\w*)")
_SOURCE_SUFFIXES = {".c", ".cpp", ".h"}
LISTED = 200  # rows each list of the page carries


def settings(profile: Profile, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """The `tas.coverage` section, or None when the profile has none (coverage is then simply not collected)."""
    table = profile.section("tas").get("coverage")
    if not table:
        return None
    check_keys(profile.source_file, "tas.coverage", table, frozenset({"elf"}), _KEYS)
    version = str(table.get("version", "us-rev1"))
    pin = table.get("source") or profile.section("tas").get("serve", {}).get("pin")
    return {
        "elf": str(table["elf"]),
        "version": version,
        "versions": [str(v) for v in table.get("versions", [version])],
        "since": float(table.get("since", 0)),
        "source": profile.path("tas.coverage.source", pin) if pin else None,
        "symbols": [str(f) for f in table.get("symbols", [])],
    }


# --- the two sides' hit files ---


def read_gcovr(path: Path) -> dict[str, int] | None:
    """name -> execution count of every function in a gcovr JSON report (same-named statics add up); None when
    the file is missing or unreadable. A function's count is the larger of gcovr's entry count and the count of its
    first body line: executions inlined into callers never pass its entry, but they run its body lines. The counts
    are kept beside it (`<report>-entries.json`, ~100 KB against the report's ~13 MB) while newer than it: a union
    over every movie otherwise parses gigabytes each time."""
    cache = path.with_name(path.stem + "-entries.json")
    try:
        if cache.stat().st_mtime >= path.stat().st_mtime:
            return json.loads(cache.read_text())
    except (OSError, ValueError):
        pass
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    counts: dict[str, int] = {}
    for entry in data.get("files", []):
        lines = {
            int(ln["line_number"]): int(ln.get("count", 0)) for ln in entry.get("lines", []) if "line_number" in ln
        }
        for fn in entry.get("functions", []):
            name = str(fn.get("name", ""))
            if name:
                counts[name] = counts.get(name, 0) + max(int(fn.get("execution_count", 0)), _first_body_line(fn, lines))
    try:
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps(counts))
        tmp.replace(cache)
    except OSError:
        pass
    return counts


def _first_body_line(fn: dict[str, Any], lines: dict[int, int]) -> int:
    """The count of the first line after a function's signature line (0 when gcovr gave no span or the body has none)."""
    try:
        first, last = (int(p.split(":")[0]) for p in fn["pos"])
    except (KeyError, ValueError, TypeError):
        return 0
    return next((lines[n] for n in sorted(lines) if first < n <= last), 0)


def spans(path: Path) -> dict[str, str]:
    """ "file:function" -> "first-last" source lines of every function in a gcovr JSON report ({} when unreadable)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    for entry in data.get("files", []):
        for fn in entry.get("functions", []):
            pos = fn.get("pos") or [str(fn.get("lineno", "?"))] * 2
            out[f"{entry.get('file')}:{fn.get('name')}"] = "-".join(str(p).split(":")[0] for p in pos[:2])
    return out


def file_signatures(path: Path) -> dict[str, str]:
    """file -> digest of its (line, gcovr/md5) pairs in a gcovr JSON report: equal digests mean the same source text."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for entry in data.get("files", []):
        text = "|".join(f"{line.get('line_number')}:{line.get('gcovr/md5')}" for line in entry.get("lines", []))
        out[str(entry.get("file"))] = hashlib.md5(text.encode()).hexdigest()
    return out


def comparable(report: Path, changed: set[str], out: Path) -> Path:
    """`report` without the files whose source differs from the build the union is drawn on (`changed`), written to
    `out` (kept while it is newer than `report`): an older port build counts only for code it shares with the current
    one (gcovr refuses to merge lines whose text differs, and merging them would credit other code). The report
    itself when nothing changed."""
    if not changed:
        return report
    if out.is_file() and out.stat().st_mtime >= report.stat().st_mtime:
        return out
    data = json.loads(report.read_text())
    data["files"] = [entry for entry in data.get("files", []) if entry.get("file") not in changed]
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(out)
    return out


BUCKETS = 32  # a build's runs merge in this many stable groups (by movie): a new run re-merges one group of them
SINGLE = 24  # a build with at most this many runs merges in one gcovr call


def _merge_set(label: str, paths: list[Path], cache: Path, build: Path, keep: set[str] | None) -> Path:
    """One gcovr `--add-tracefile` merge of `paths`, kept in `cache` under the digest of the set: merged once."""
    key = hashlib.md5("\n".join(sorted(str(p) for p in paths)).encode()).hexdigest()[:12]
    out = cache / f"{label[:12]}-{key}.json"
    if keep is not None:
        keep.add(out.name)
    if not out.is_file():
        tmp = out.with_suffix(".tmp")
        args = [*_gcovr_base(build), "--json", str(tmp)]
        for path in paths:
            args += ["--add-tracefile", str(path)]
        gcovr(args, build)
        tmp.replace(out)
    return out


def merge_build(group: str, paths: list[Path], cache: Path, build: Path, keep: set[str] | None = None) -> Path:
    """One gcovr JSON for the runs of one port build (same source, so gcovr's strict merge holds), kept in `cache`
    under the run set's digest: an old build's runs are merged once, not on every union. A lone run is its own.
    A big build merges as a tree: its runs fall into BUCKETS groups by movie name, each group merges (and is kept)
    on its own, then the group files merge. A run that replaces its movie's older one changes one group, so the
    next union re-merges ~1/BUCKETS of the runs (the whole set cost ~500 CPU s, and each new run repeated it).
    `keep` collects the cache files this merge reads or writes."""
    if len(paths) == 1:
        return paths[0]
    if len(paths) <= SINGLE:
        return _merge_set(group, paths, cache, build, keep)
    buckets: dict[int, list[Path]] = {}
    for path in paths:
        movie = path.parent.name.partition("Z-")[2] or path.parent.name
        buckets.setdefault(int(hashlib.md5(movie.encode()).hexdigest(), 16) % BUCKETS, []).append(path)
    parts = [_merge_set(f"{group[:8]}-b{i:02d}", ps, cache, build, keep) for i, ps in sorted(buckets.items())]
    return _merge_set(group, parts, cache, build, keep)


def mixed_builds(by_build: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """Functions whose source span differs between the port builds a union merges (or that some build lacks): their
    counts come from different code, so the merged line coverage is not one program's. by_build: sha -> spans()."""
    if len(by_build) < 2:
        return []
    keys = sorted({k for spans_ in by_build.values() for k in spans_})
    out = []
    for key in keys:
        seen = {sha[:12]: spans_.get(key, "absent") for sha, spans_ in sorted(by_build.items())}
        if len(set(seen.values())) > 1:
            file, _, name = key.partition(":")
            out.append({"file": file, "function": name, "spans": seen})
    return out


@slots.admitted("background")
def gcovr(args: list[str], cwd: Path, timeout: float = 600) -> None:
    """Run gcovr (the decomp venv's own pinned copy); a failure is a DecompError carrying its last lines.

    gcovr's gcov calls drop one `<source>##<hash>.gcov.json.gz` per unit into the process cwd (thousands per run; they
    filled the build and TAS folders), so it runs in a scratch folder under `cwd` that is removed afterwards. Every path
    in `args` is absolute."""
    scratch = Path(tempfile.mkdtemp(prefix=".gcovr-", dir=cwd))
    try:
        done = subprocess.run(
            [sys.executable, "-m", "gcovr", *args],
            cwd=scratch,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DecompError("tas.coverage", f"gcovr did not run: {error}") from error
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if done.returncode:
        raise DecompError("tas.coverage", "gcovr failed: " + failure(done.stderr))


def failure(stderr: str) -> str:
    """The cause of a gcovr failure: its first error line (a merge conflict ends in a long list of tracefile paths,
    so the last lines alone name files, not the cause) plus the last line."""
    lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
    cause = next((line for line in lines if "(ERROR)" in line or "Error" in line or line.startswith("Got ")), "")
    tail = lines[-1] if lines else "no output"
    return " | ".join(dict.fromkeys(x for x in (cause, tail) if x))


def read_oracle(run: Path) -> tuple[dict[str, tuple[int, int]], int] | None:
    """(name -> (first poll, count), polls the oracle ran) from the oracle's RUN/oracle-golden/trace-final.tsv and
    its poll trace (tas_oracle.ORACLE_TRACE); None when it left no function trace."""
    try:
        rows = (run / ORACLE_DIR / "trace-final.tsv").read_text(errors="replace").splitlines()[1:]
    except OSError:
        return None
    vis: list[int] = []
    try:
        for line in (run / tas_oracle.ORACLE_TRACE).read_text().splitlines():
            parts = line.split()
            if len(parts) == 2:
                vis.append(int(parts[1]))
    except (OSError, ValueError):
        pass
    hits: dict[str, tuple[int, int]] = {}
    for line in rows:
        parts = line.split("\t")
        if len(parts) < 5 or not (parts[2].isdigit() and parts[4].isdigit()):
            continue
        hits[parts[1]] = (bisect.bisect_right(vis, int(parts[2])), int(parts[4]))
    return hits, len(vis)


_SNAPSHOT = re.compile(r"trace-poll-(\d+)\.tsv")


def _entry_counts(path: Path) -> dict[str, int]:
    """name -> entry count of one oracle trace table (symbol in column 2, count in column 5)."""
    counts: dict[str, int] = {}
    try:
        rows = path.read_text(errors="replace").splitlines()[1:]
    except OSError:
        return counts
    for line in rows:
        parts = line.split("\t")
        if len(parts) >= 5 and parts[4].isdigit():
            counts[parts[1]] = int(parts[4])
    return counts


def oracle_bracket(run: Path, poll: int) -> tuple[dict[str, int], dict[str, int] | None] | None:
    """The oracle's cumulative entry counts around `poll` (RUN/oracle-golden/trace-poll-N.tsv, one every shot_every
    polls): (counts one snapshot before the last snapshot <= poll, counts at the first snapshot >= poll, None when the
    oracle ended before `poll`). The port's last poll and the oracle's snapshots are not in phase (the port lags the
    oracle by up to one snapshot interval), so the lower bound reaches one interval back. None when the run holds no
    snapshots."""
    folder = run / ORACLE_DIR
    polls = sorted(int(m[1]) for p in folder.glob("trace-poll-*.tsv") if (m := _SNAPSHOT.fullmatch(p.name)))
    if not polls:
        return None
    below = [p for p in polls if p <= poll]
    above = [p for p in polls if p >= poll]
    low = _entry_counts(folder / f"trace-poll-{below[-2]}.tsv") if len(below) > 1 else {}
    high = _entry_counts(folder / f"trace-poll-{above[0]}.tsv") if above else None
    return low, high


def behaviour(port: dict[str, int], bracket: tuple[dict[str, int], dict[str, int] | None]) -> dict[str, str]:
    """Per function the port's build defines and either side entered: `behaves` when the port's entry count at its last
    poll lies inside the oracle_bracket (at least the lower bound when the oracle ended earlier), `missing` when the port never entered what the original had by then, `port-only` when the
    original never entered it by the next snapshot, else `differs`. Count granularity: lanes/TAS-CORPUS-OPUS-1."""
    low, high = bracket
    out: dict[str, str] = {}
    for name, count in port.items():
        lo = low.get(name, 0)
        hi = high.get(name, 0) if high is not None else None
        if count == 0 and lo == 0 and not hi:
            continue
        if lo <= count and (hi is None or count <= hi):
            out[name] = "behaves"
        elif count == 0:
            out[name] = "missing"
        elif hi == 0:
            out[name] = "port-only"
        else:
            out[name] = "differs"
    return out


@slots.admitted("background")
def run_oracle(profile: Profile, cfg: dict[str, Any], run: Path, record: dict[str, Any]) -> bool:
    """Replay the run's movie in the original game (the `tas.oracle` host, as `tas play --oracle` does) with the host's
    golden dir on, so it writes RUN/oracle-golden/trace-final.tsv: every function it entered, first VI and count.
    True when it left that trace; False without a `tas.oracle` section or host."""
    oracle = tas_oracle.settings(profile)
    if oracle is None or not oracle["host"].is_file() or not (run / "movie.raw").is_file():
        return False
    (run / ORACLE_DIR).mkdir(exist_ok=True)
    card = None
    if (run / "cpak-start.mpk").is_file():  # the run had a Controller Pak: the oracle starts from its own copy of it
        card = run / "oracle-cpak.mpk"
        card.write_bytes((run / "cpak-start.mpk").read_bytes())
    process = tas_oracle.start(
        oracle,
        run,
        cfg["exit_after"],
        str(record.get("pak") or "on"),
        extra={"N64R_GOLDEN_DIR": str(run / ORACLE_DIR)},
        card=card,
        pads=int(record.get("pads", 1)),
    )
    try:
        process.wait(oracle["timeout_seconds"])
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    return (run / ORACLE_DIR / "trace-final.tsv").is_file()


# --- the universe ---


def elf_functions(elf: Path) -> dict[str, tuple[int, int]]:
    """name -> (address, size) of every sized function symbol in an ELF."""
    try:
        out = subprocess.run(
            ["readelf", "-sW", str(elf)], capture_output=True, text=True, timeout=120, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DecompError("tas.coverage", f"cannot read {elf}: {error}") from error
    found: dict[str, tuple[int, int]] = {}
    for line in out.splitlines():
        match = _SYMBOL.match(line)
        if match and int(match[2]):
            found.setdefault(match[3], (int(match[1], 16), int(match[2])))
    return found


def source_files(source: Path | None, names: set[str]) -> dict[str, str]:
    """name -> `src/...` file defining it (a C body, or the INCLUDE_ASM that stands in for it); the first wins."""
    where: dict[str, str] = {}
    if source is None or not (source / "src").is_dir():
        return where
    for path in sorted((source / "src").rglob("*")):
        if path.suffix.lower() not in _SOURCE_SUFFIXES or not path.is_file():
            continue
        rel = str(path.relative_to(source))
        for line in path.read_text(errors="replace").splitlines():
            match = _DEFINITION.match(line) if line[:1].isalpha() or line[:1] == "_" else None
            name = match[1] if match else None
            if name is None and "INCLUDE_ASM" in line:
                match = _INCLUDE_ASM.search(line)
                name = match[1] if match else None
            if name in names:
                where.setdefault(name, rel)
    return where


def symbol_tables(cov: dict[str, Any]) -> tuple[dict[str, int], dict[int, str]]:
    """(name -> address, address -> the name the first table gives it) of the functions in the source's address
    tables (`tas.coverage.symbols`); both empty without them or without a source."""
    by_name: dict[str, int] = {}
    first: dict[int, str] = {}
    source = cov.get("source")
    for rel in cov.get("symbols") or []:
        try:
            text = (source / rel).read_text(errors="replace") if source else ""
        except OSError:
            continue
        for line in text.splitlines():
            found = remap.FACT.match(line)
            if found and "type:func" in found.group(3):
                address = int(found.group(2), 16)
                by_name.setdefault(found.group(1), address)
                first.setdefault(address, found.group(1))
    return by_name, first


def resolver(rows: list[dict[str, Any]], version: str, by_name: dict[str, int]) -> Callable[[str], str]:
    """name -> the row name it stands for: itself when a row has it, else the row at the address the source's tables
    give the name (the row of a function the source renamed), else itself (a helper no ROM address names)."""
    names = {row["name"] for row in rows}
    at = {row["address"]: row["name"] for row in rows if version in row["versions"] and row["address"]}
    old = {row["elf_name"]: row["name"] for row in rows if row.get("elf_name")}

    def resolve(name: str) -> str:
        if name in names:
            return name
        return old.get(name) or at.get(by_name.get(name, 0), name)

    return resolve


def fold(counts: dict[str, Any], resolve: Callable[[str], str], merge: Callable[[Any, Any], Any]) -> dict[str, Any]:
    """`counts` keyed by row name: the entries of names that resolve to one row merge into it."""
    out: dict[str, Any] = {}
    for name, value in counts.items():
        key = resolve(name)
        out[key] = merge(out[key], value) if key in out else value
    return out


def _add(a: int, b: int) -> int:
    return a + b


def _first_seen(a: tuple[int, int], b: tuple[int, int]) -> tuple[int, int]:
    return (min(a[0], b[0]), a[1] + b[1])


def universe(
    cov: dict[str, Any], symbols: tuple[dict[str, int], dict[int, str]] | None = None
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """One row per function over all versions' ELFs, and each version's function count. The reference version's
    functions are keyed by ROM address and carry the name the source's tables give that address now (`elf_name` when
    the ELF still says another); the other versions' functions join them by their ELF name, renamed the same way."""
    rows: dict[str, dict[str, Any]] = {}
    totals: dict[str, int] = {}
    current = symbols[1] if symbols else {}
    renamed: dict[str, str] = {}
    for version in sorted(cov["versions"], key=lambda v: v != cov["version"]):
        elf = Path(cov["elf"].format(version=version))
        funcs = elf_functions(elf)
        totals[version] = len(funcs)
        for name, (address, size) in funcs.items():
            if version == cov["version"]:
                renamed[name] = current.get(address, name)
            key = renamed.get(name, name)
            row = rows.setdefault(key, {"name": key, "versions": [], "address": 0, "size": 0})
            row["versions"].append(version)
            if version == cov["version"] or not row["size"]:
                row["address"], row["size"] = address, size
            if version == cov["version"] and key != name:
                row["elf_name"] = name
    files = source_files(cov["source"], set(rows))
    for name, row in rows.items():
        row["file"] = files.get(name, "")
        row["scope"] = "sdk" if row["file"].startswith("src/sdk/") else "game" if row["file"] else "other"
    return sorted(rows.values(), key=lambda r: r["address"]), totals


# --- the union ---


def _share(hit: int, total: int) -> float:
    return round(100.0 * hit / total, 2) if total else 0.0


def union(
    rows: list[dict[str, Any]],
    totals: dict[str, int],
    version: str,
    runs: list[dict[str, Any]],
    linked: set[str] | None,
) -> dict[str, Any]:
    """The function table over the movies' runs. `runs` is one dict per movie: {movie, port, oracle, port_polls,
    oracle_polls, behaviour?}: port maps name -> execution count (gcovr), oracle maps name -> (first poll, count) or is
    None when it never ran, behaviour maps name -> `behaviour()` state. `linked` is the set of names the port binary
    defines (None: unknown). A function's `behaviour` over the runs: differs or missing when any run says so, else
    behaves, else port-only (None when no run judged it): coverage that says "executed and behaves like the original".

    A run's two sides are compared over the polls both reached: the port stops `exit_after` polls after the movie (or
    where it crashed) and the oracle runs its own tail, so a function the oracle enters after the port's run ended is
    `oracle-late`, not a defect. `port-only` has no such guard (gcov has no poll); the page does not list it."""
    for run in runs:
        run["common"] = min(run.get("port_polls") or 0, run.get("oracle_polls") or 0)
    table = []
    for row in rows:
        name = row["name"]
        out = {k: row[k] for k in ("name", "file", "scope", "size", "versions", "address") if k in row}
        if row.get("elf_name"):
            out["elf_name"] = row["elf_name"]
        out["in_version"] = version in row["versions"]
        port = [(r["movie"], r["port"][name]) for r in runs if r["port"].get(name)]
        oracle = [(r["movie"], r["oracle"][name]) for r in runs if r["oracle"] and name in r["oracle"]]
        first = min(oracle, key=lambda m: (m[1][0], m[0]), default=None)
        out["port"] = {
            "hit": bool(port),
            "count": sum(c for _, c in port),
            "movie": port[0][0] if port else None,
            "poll": None,
        }
        out["oracle"] = {
            "hit": bool(oracle),
            "count": sum(c[1] for _, c in oracle),
            "movie": first[0] if first else None,
            "poll": first[1][0] if first else None,
        }
        out["port_linked"] = None if linked is None else name in linked
        reached = any(r["oracle"] and name in r["oracle"] and r["oracle"][name][0] <= r["common"] for r in runs)
        if reached and not port:
            out["state"] = "oracle-only"
        elif port and runs and any(r["oracle"] is not None for r in runs) and not oracle:
            out["state"] = "port-only"
        elif port and oracle:
            out["state"] = "both"
        elif oracle:
            out["state"] = "oracle-late"
        elif port:
            out["state"] = "port"
        else:
            out["state"] = "neither"
        judged = [r["behaviour"][name] for r in runs if name in (r.get("behaviour") or {})]
        out["behaviour"] = next((s for s in ("differs", "missing", "behaves", "port-only") if s in judged), None)
        table.append(out)
    scoped = [r for r in table if r["in_version"]]
    summary: dict[str, Any] = {
        "version": version,
        "movies": len(runs),
        "oracle_movies": sum(1 for r in runs if r["oracle"] is not None),
    }
    for scope in ("game", "sdk", "other"):
        mine = [r for r in scoped if r["scope"] == scope]
        entry: dict[str, Any] = {"functions": len(mine), "bytes": sum(r["size"] for r in mine)}
        for label, test in (
            ("port", lambda r: r["port"]["hit"]),
            ("oracle", lambda r: r["oracle"]["hit"]),
            ("either", lambda r: r["port"]["hit"] or r["oracle"]["hit"]),
        ):
            hit = [r for r in mine if test(r)]
            entry[label] = {
                "functions": len(hit),
                "functions_pct": _share(len(hit), len(mine)),
                "bytes": sum(r["size"] for r in hit),
                "bytes_pct": _share(sum(r["size"] for r in hit), entry["bytes"]),
            }
        summary[scope] = entry
    game = [r for r in scoped if r["scope"] == "game"]
    summary["states"] = dict(Counter(r["state"] for r in game))
    summary["behaviour"] = dict(Counter(r["behaviour"] for r in game if r.get("behaviour")))
    by_version = {}
    seen = {r["name"] for r in table if r["port"]["hit"] or r["oracle"]["hit"]}
    for v in totals:
        names = [r for r in table if v in r["versions"]]
        by_version[v] = {"functions": totals[v], "reached_by_name": sum(1 for r in names if r["name"] in seen)}
    summary["versions"] = by_version
    covered = [
        [
            i
            for i, row in enumerate(table)
            if run["port"].get(row["name"]) or (run["oracle"] and row["name"] in run["oracle"])
        ]
        for run in runs
    ]
    return {"generated": time.time(), "summary": summary, "functions": table, "covered": covered}


def lists(report: dict[str, Any], limit: int = LISTED) -> dict[str, Any]:
    """The page's view: the summary plus the two lists that drive work, biggest functions first.
    `oracle_only`: the original reaches it and the port never does (a defect ticket for a cycle).
    `neither`: no movie reaches it (the owner records one); asm stand-ins and sdk rows are left out of that list."""
    game = [r for r in report.get("functions", []) if r["in_version"] and r["scope"] == "game"]

    def pick(state: str) -> list[dict[str, Any]]:
        mine = sorted((r for r in game if r["state"] == state), key=lambda r: (-r["size"], r["name"]))
        return mine[:limit]

    return {
        "generated": report.get("generated"),
        "since": report.get("since"),
        "summary": report.get("summary"),
        "gcovr": report.get("gcovr"),
        "builds": report.get("builds"),
        "changed_files": report.get("changed_files"),
        "dropped_functions": report.get("dropped_functions") or [],
        "mixed_build_count": len(report.get("mixed_build") or []),
        "mixed_build": (report.get("mixed_build") or [])[:limit],
        **(
            {"refused": report["refused"], "stale_runs": report.get("stale_runs") or [], "next": report.get("next")}
            if report.get("refused")
            else {}
        ),
        "oracle_only": pick("oracle-only"),
        "neither": pick("neither"),
        "port_only": pick("port-only"),
    }


# --- driving it ---

LINK = "ragewars-decomp"  # a coverage build tree's link to the source it compiled
FILTER = ".*/src/"  # the original game's sources (not the headers, not the generated stubs)


def build_dir(cfg: dict[str, Any]) -> Path:
    """The CMake build tree of the page's port: where the .gcno files and the `ragewars-decomp` source link are. The
    port's own folder when it is that tree; else the tree `tas.build.units` lies in (its nearest folder holding a
    CMakeCache.txt): a page port is published away from its build tree (live/port), and coverage refused it as "not
    a coverage build" (PORT-FIX-O1, 10-25)."""
    own = Path(cfg["port"]).parent
    units = (cfg.get("build") or {}).get("units")
    if (own / LINK).exists() or not units:
        return own
    return next((p for p in (Path(units), *Path(units).parents) if (p / "CMakeCache.txt").is_file()), own)


def _gcovr_base(build: Path) -> list[str]:
    if not (build / LINK).exists():
        raise DecompError(
            "tas.coverage",
            f"{build} is not a coverage build (no ragewars-decomp link): "
            "configure the port with -DRAGE_DECOMP_COVERAGE=ON",
        )
    return ["--root", str(build), "--gcov-ignore-errors=all", "--filter", FILTER]


def run_report(profile: Profile, cfg: dict[str, Any], run: Path) -> Path | None:
    """RUN/coverage.json from the run's counters (GCOV_PREFIX = RUN/gcov mirrors the build tree's absolute path), or
    None when the profile has no coverage section or the run left no counters (not a coverage build, or killed hard)."""
    if settings(profile, cfg) is None:
        return None
    build = build_dir(cfg)
    counters = run / GCOV / str(build).lstrip("/")
    if not counters.is_dir():
        return None
    # gcov pairs a .gcda with the .gcno beside it; the counters live in the run folder, the notes in the build tree.
    for gcda in counters.rglob("*.gcda"):
        note = gcda.with_suffix(".gcno")
        source = build / note.relative_to(counters)
        if source.is_file() and not note.exists():
            note.symlink_to(source)
    out = run / COVERAGE
    gcovr([*_gcovr_base(build), "--json", str(out), str(counters)], run)
    return out


def drop_orphan_notes(build: Path) -> int:
    """Delete every .gcno (and its .gcda) under build/decomp whose unit source is gone from the build's source link
    (units a pin bump renamed or merged): gcovr dies reading a missing source. Returns how many were dropped."""
    link, dropped = build / LINK, 0
    if not link.exists():  # not a coverage build: nothing says which notes are orphans
        return 0
    for note in (build / "decomp").rglob("*.gcno"):
        unit = note.relative_to(build / "decomp").with_suffix("")
        if unit.suffix == ".stubs":
            unit = unit.with_suffix("")
        if not (link / unit).exists():
            note.unlink(missing_ok=True)
            note.with_suffix(".gcda").unlink(missing_ok=True)
            dropped += 1
    return dropped


def _mirror_notes(tree: Path, into: Path) -> Path:
    """INTO: symlinks to every .gcno under TREE, same relative paths and nothing else, so gcov finds no .gcda beside a
    note and a count stray in the build tree can never enter a zero baseline."""
    into.mkdir(parents=True, exist_ok=True)
    for note in tree.rglob("*.gcno"):
        link = into / note.relative_to(tree)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(note)
    return into


def baseline(cfg: dict[str, Any]) -> Path:
    """runs/coverage-baseline-<build>.json: the build's every unit with zero counts (gcovr reads the .gcno notes
    alone), so files no movie loads still show in the union. One per port build, made once."""
    from abu_decomp import tas

    build = build_dir(cfg)
    path = cfg["runs"] / f"coverage-baseline-{tas.build_id(Path(cfg['port']))[:12] or 'unbuilt'}.json"
    if not path.is_file():
        for old in cfg["runs"].glob("coverage-baseline-*.json"):
            old.unlink()
        cfg["runs"].mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".notes-", dir=build) as mirror:
            roots = [_mirror_notes(build / name, Path(mirror) / name) for name in ("decomp", "decomp-image")]
            gcovr([*_gcovr_base(build), "--json", str(path), *map(str, roots)], build)
    return path


def published(cfg: dict[str, Any], port: Any) -> bool:
    """True when PORT (a run's recorded executable) is a page-port build: it sits in the same publication folder as
    the configured port's build (live/port -> live/snaps/port-<pin>-<time>/ragewars). A lane's private build, run
    with `--port`, is built elsewhere from a tree nobody pinned, so its counts belong to no main pin."""
    if not port or not cfg.get("port"):
        return False
    return Path(os.path.realpath(port)).parent.parent == Path(os.path.realpath(cfg["port"])).parent.parent


def latest_runs(cfg: dict[str, Any], since: float = 0) -> dict[str, Path]:
    """movie name -> its newest movie-driven finished run folder that left a coverage report or counters, started
    at or after SINCE (a run with no start time never counts once SINCE is set) and played a page-port build: a
    binary published beside the configured one (see `published`)."""
    from abu_decomp import tas

    newest: dict[str, Path] = {}
    for run, row in tas.run_rows(cfg):
        if row.get("status") == "running" or row.get("input") == "human" or (run / tas.TAKEN).exists():
            continue
        if not ((run / COVERAGE).is_file() or (run / GCOV).is_dir()):
            continue
        if since and float(row.get("started") or 0) < since:
            continue
        if not published(cfg, row.get("port")):
            continue
        name = str(row.get("movie") or "")
        if name and (name not in newest or run.name > newest[name].name):
            newest[name] = run
    return newest


def build(
    profile: Profile,
    cfg: dict[str, Any],
    *,
    oracle: bool = True,
    jobs: int = 0,
    table_only: bool = False,
    rebaseline: bool = False,
    oracle_seconds: float | None = None,
) -> dict[str, Any] | None:
    """Union the newest run of every movie: runs/coverage/ (gcovr `--html-details` over the baseline and every
    run's JSON), runs/coverage-table.json (the function table with the oracle). None without a `tas.coverage`
    section. With `oracle`, a run with no oracle trace yet is replayed in the oracle first, for at most
    `oracle_seconds` (no new replay starts after that; the rest wait for the next call, so a catch-up over hundreds of
    runs cannot eat the whole call and lose the union; None: no limit). With `table_only` the
    function table is rebuilt from the per-run function counts alone: no gcovr merge, no HTML page, no line summary
    (that final `--add-tracefile` over every report cost ~358 CPU s, past the 5-minute cap); the page, the line
    totals and the per-build fields stay those of the last full build."""
    from concurrent.futures import ThreadPoolExecutor

    from abu_decomp import tas

    cov = settings(profile, cfg)
    if cov is None:
        return None
    runs = latest_runs(cfg, cov["since"])
    drop_orphan_notes(build_dir(cfg))
    symbols = symbol_tables(cov)
    rows, totals = universe(cov, symbols)
    resolve = resolver(rows, cov["version"], symbols[0])

    deadline = None if oracle_seconds is None else time.monotonic() + oracle_seconds

    def one(item: tuple[str, Path]) -> dict[str, Any]:
        name, run = item
        if not (run / COVERAGE).is_file():
            run_report(profile, cfg, run)
        record = tas.read_record(run) or {}
        got = read_oracle(run)
        if (
            got is None
            and oracle
            and (deadline is None or time.monotonic() < deadline)
            and run_oracle(profile, cfg, run, record)
        ):
            got = read_oracle(run)
        port = fold(read_gcovr(run / COVERAGE) or {}, resolve, _add)
        bracket = oracle_bracket(run, int(record.get("polls_reached") or 0))
        if bracket:
            bracket = (
                fold(bracket[0], resolve, _add),
                fold(bracket[1], resolve, _add) if bracket[1] is not None else None,
            )
        return {
            "movie": name,
            "run": run.name,
            "port": port,
            "behaviour": behaviour(port, bracket) if bracket else None,
            "oracle": fold(got[0], resolve, _first_seen) if got else None,
            "port_polls": int(record.get("polls_reached") or 0),
            "oracle_polls": got[1] if got else 0,
            "build": str(record.get("build") or "unknown"),
        }

    with ThreadPoolExecutor(max_workers=max(1, jobs or slots.capacity())) as pool:
        collected = list(pool.map(one, sorted(runs.items())))
    build_tree = build_dir(cfg)
    changed: dict[str, set[str]] = {}
    groups: dict[str, list[Path]] = {}
    page = summary_file = final_page = final_summary = None
    base: Path | None = None
    tag = ""
    if not table_only:
        # One build per movie (its newest run). The runs of each port build merge once (cached); a build other than the
        # baseline's (the current port) keeps only the files whose source text the current build shares, the dropped
        # files counted per build.
        base = baseline(cfg)
        current = file_signatures(base)
        groups: dict[str, list[Path]] = {}
        for r in collected:
            path = runs[r["movie"]] / COVERAGE
            if path.is_file():  # a run of an unknown build is its own group: its source cannot be assumed shared
                groups.setdefault(r["build"] if r["build"] != "unknown" else f"unknown-{r['run']}", []).append(path)
        cache = cfg["runs"] / "coverage-builds"
        cache.mkdir(parents=True, exist_ok=True)
        tag = base.stem.rsplit("-", 1)[-1]
        tracefiles, changed, keep = [base], {}, set()
        for group, paths in sorted(groups.items()):
            merged = merge_build(group, paths, cache, build_tree, keep)
            changed[group] = {f for f, sig in file_signatures(merged).items() if current.get(f) != sig}
            key = merged.stem if merged.parent == cache else merged.parent.name
            out = comparable(merged, changed[group], cache / f"{key}.on-{tag}.json")
            keep.update({merged.name, out.name})
            tracefiles.append(out)
        for old in cache.iterdir():
            if old.name not in keep:
                old.unlink()
        final_page, final_summary = cfg["runs"] / HTML, cfg["runs"] / "coverage-summary.json"
        page, summary_file = cfg["runs"] / (HTML + ".new"), cfg["runs"] / "coverage-summary.new.json"
        shutil.rmtree(page, ignore_errors=True)
        merged_key = merge_key(tracefiles, build_tree)
        if (
            (final_page / MERGE_KEY).is_file()
            and (final_page / MERGE_KEY).read_text() == merged_key
            and (final_page / UNION_JSON).is_file()
            and final_summary.is_file()
        ):
            # the same tracefiles as the page in place: its HTML and summary are this merge (the --add-tracefile
            # merge over every report cost ~358 CPU s; a re-run with no new run, a --rebaseline, paid it again)
            shutil.copytree(final_page, page)
            shutil.copy2(final_summary, summary_file)
        else:
            page.mkdir(parents=True)
            _merge_page(build_tree, page, summary_file, tracefiles)
            (page / MERGE_KEY).write_text(merged_key)
    _, names = tas_symbolize.symbols(cfg["port"])
    report = union(rows, totals, cov["version"], collected, set(names) if names else None)
    report["runs"] = [
        {"movie": r["movie"], "run": r["run"], "polls": r["port_polls"], "covered": cov, "build": r["build"]}
        for r, cov in zip(collected, report.pop("covered"), strict=True)
    ]
    # One build per movie (its newest run): which port builds the union merges, and the functions they disagree on.
    builds: dict[str, int] = {}
    for r in collected:
        builds[r["build"]] = builds.get(r["build"], 0) + 1
    report["builds"] = dict(sorted(builds.items(), key=lambda kv: -kv[1]))
    report["since"] = cov["since"]
    report["oracle_pending"] = sum(1 for r in collected if r["oracle"] is None) if oracle else 0
    old = read_table(cfg) or {}
    if table_only or base is None:
        # the page, the line totals and the per-build comparison are the last full build's
        for key in ("changed_files", "baseline", "dropped_functions", "changed_functions", "mixed_build", "gcovr"):
            report[key] = old.get(key, [] if key.endswith(("functions", "build")) else None)
        report["table_only"] = True
        mixed = {m["function"] for m in report.get("mixed_build") or []}
        for row in report.get("functions", []):
            row["mixed_build"] = row.get("name") in mixed
    else:
        report["changed_files"] = {group: len(files) for group, files in changed.items() if files}
        report["baseline"] = tag
        dropped = set().union(*changed.values()) if changed else set()
        report["dropped_functions"] = [
            {"file": file, "function": name}
            for file, _, name in sorted(key.partition(":") for key in spans(base))
            if file in dropped
        ]
        report["changed_functions"] = len(report["dropped_functions"])
    if not table_only:
        # spans per known build (one run each; a run of an unknown build is not compared)
        report["mixed_build"] = mixed_builds(
            {group: spans(paths[0]) for group, paths in groups.items() if not group.startswith("unknown-")}
        )
        mixed = {m["function"] for m in report["mixed_build"]}
        for row in report.get("functions", []):
            row["mixed_build"] = row.get("name") in mixed
        try:
            gsum = json.loads(summary_file.read_text())
            report["gcovr"] = {
                k: gsum.get(k)
                for k in (
                    "line_percent",
                    "line_covered",
                    "line_total",
                    "function_percent",
                    "function_covered",
                    "function_total",
                    "branch_percent",
                    "branch_covered",
                    "branch_total",
                )
            }
        except (OSError, ValueError):
            report["gcovr"] = None
    path = cfg["runs"] / TABLE
    old = read_table(cfg)
    why = None if table_only or rebaseline else refusal(old, report)
    if rebaseline and old is not None:  # the report in place stays readable beside the new one
        path.with_name("coverage-table.prev.json").write_text(json.dumps(old, sort_keys=True) + "\n")
        report["rebaselined"] = covered_functions(old)
    if why:  # never replace a report with one covering fewer functions: the new one lands beside it
        report["refused"] = why
        report["stale_runs"] = stale_runs(report["runs"], tag)
        report["next"] = refused_next(report["stale_runs"])
        side = path.with_name("coverage-table.refused.json")
        side.write_text(json.dumps(report, sort_keys=True) + "\n")
        shutil.rmtree(page, ignore_errors=True)
        summary_file.unlink(missing_ok=True)
        return report
    if not table_only:
        shutil.rmtree(final_page, ignore_errors=True)
        page.replace(final_page)
        summary_file.replace(final_summary)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, sort_keys=True) + "\n")
    tmp.replace(path)
    bake_coverage(profile)
    return report


def bake_coverage(profile: Profile) -> None:
    """Bake the Runner page's coverage data (gutters from union.json, function rows) from the report just written.
    Takes its own batch slot, so it runs after every gcovr hold has ended. A bake that cannot run never fails the
    union; it says why on stderr and `decomp tas bake --what coverage` repeats it."""
    from abu_decomp import tas_bake

    try:
        tas_bake.bake(profile, {"coverage"})
    except Exception as error:
        print(f"tas bake --what coverage: {error}", file=sys.stderr)


UNION_JSON = "union.json"  # in the page folder: the gcovr union as JSON (files, lines, functions), for readers
MERGE_KEY = ".merge-key"  # in the page folder: the tracefiles (path, size, date) the page was merged from


def merge_key(tracefiles: list[Path], build: Path) -> str:
    """What one HTML merge of these tracefiles reads: each path, size and date, the build tree, the filter."""
    rows = [(str(p), *((p.stat().st_size, p.stat().st_mtime_ns) if p.is_file() else (-1, -1))) for p in tracefiles]
    return hashlib.sha256(json.dumps([rows, str(build), FILTER]).encode()).hexdigest()


def _merge_page(build: Path, page: Path, summary: Path, tracefiles: list[Path]) -> None:
    args = [
        *_gcovr_base(build),
        "--html-details",
        str(page / "index.html"),
        "--html-title",
        "Rage Wars game code: TAS movies",
        "--json-summary",
        str(summary),
        "--json",
        str(page / UNION_JSON),
    ]
    for path in tracefiles:
        args += ["--add-tracefile", str(path)]
    gcovr(args, build)


def stale_runs(runs: list[dict[str, Any]], tag: str) -> list[dict[str, Any]]:
    """The union's runs from a port build other than the baseline's (TAG, the current build's id prefix; "" for a
    table-only union, which knows no baseline): {movie, run, build}, the movies a replay on the current build renews."""
    if not tag:
        return []
    return [
        {"movie": r["movie"], "run": r["run"], "build": r["build"]}
        for r in runs
        if not str(r.get("build") or "").startswith(tag)
    ]


def refused_next(stale: list[dict[str, Any]]) -> str:
    """What to run after a refused union: replay the stale movies on the current build, or take the smaller union."""
    if stale:
        return (
            f"decomp tas play --all --resume (replays the {len(stale)} movies whose newest run is another port build: "
            f"{', '.join(r['movie'] for r in stale[:5])}{' ...' if len(stale) > 5 else ''}), then decomp tas coverage; "
            "or decomp tas coverage --rebaseline to accept the smaller union"
        )
    return "decomp tas coverage --rebaseline to accept the smaller union (no run is from another port build)"


def covered_functions(report: dict[str, Any] | None) -> int:
    """Functions gcovr counted as covered in a coverage table (0 for none or an unreadable one)."""
    return int(((report or {}).get("gcovr") or {}).get("function_covered") or 0)


def refusal(old: dict[str, Any] | None, new: dict[str, Any]) -> str | None:
    """Why `new` must not replace `old`: it covers fewer functions. On the same baseline (port build) any loss is lost
    counters. Drawn on another build, the union drops the source files that changed since the older runs, so it may
    lose at most the functions those files define (`changed_functions`); a bigger loss is still refused. A report
    of another epoch (`since`) is a deliberate restart: nothing of the old one is kept, so nothing is refused."""
    if float((old or {}).get("since") or 0) != float(new.get("since") or 0):
        return None
    before, after = covered_functions(old), covered_functions(new)
    same = (old or {}).get("baseline") == new.get("baseline")
    allowed = 0 if same else int(new.get("changed_functions") or 0)
    if after >= before - allowed:
        return None
    return f"covers {after} functions, the report in place {before}" + (
        "" if same else f" (another port build: up to {allowed} functions of changed files may go)"
    )


def read_table(cfg: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return json.loads((cfg["runs"] / TABLE).read_text())
    except (OSError, ValueError):
        return None


def line(report: dict[str, Any]) -> str:
    """One line for the console: gcov line/branch/function percentages and the two work lists' sizes."""
    s = report["summary"]
    g = report.get("gcovr") or {}
    states = s["states"]
    if report.get("refused"):
        return (
            f"coverage report NOT replaced: new union {report['refused']} (beside it: coverage-table.refused.json); "
            f"{len(report.get('stale_runs') or [])} runs from another port build; next: {report.get('next')}"
        )
    pct = lambda key: f"{g[key]:.1f}%" if g.get(key) is not None else "?"  # noqa: E731
    return (
        f"coverage {s['version']} gcov: lines {pct('line_percent')}, branches {pct('branch_percent')}, functions "
        f"{pct('function_percent')}; port {s['game']['port']['bytes_pct']}% of game bytes; oracle-only "
        f"{states.get('oracle-only', 0)}, neither {states.get('neither', 0)}; behaves "
        f"{s.get('behaviour', {}).get('behaves', 0)}, differs {s.get('behaviour', {}).get('differs', 0)} "
        f"({s['movies']} movies, {s['oracle_movies']} with oracle, {report.get('oracle_pending', 0)} still to replay; "
        f"{len(report.get('builds') or {})} port builds, "
        f"{len(report.get('mixed_build') or [])} mixed-build functions); report runs/{HTML}/index.html"
    )
