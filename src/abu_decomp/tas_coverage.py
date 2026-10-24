"""Which game code the recorded movies reach: gcov line/branch/function coverage per run and unioned, plus the oracle.

Port side: the page's port is a gcov build (cmake RAGE_DECOMP_COVERAGE: the original units compiled with GCC's
`--coverage`, libgcov linked, counters dumped on every exit path by decomp/coverage.c). Each `tas play` sets
`GCOV_PREFIX` to RUN/gcov, so a run keeps its own .gcda; `run_report` turns them into RUN/coverage.json with
`gcovr --json`, and `build` unions every movie's newest run with `gcovr --add-tracefile` into one `--html-details`
report (runs/coverage/index.html, served by `tas serve` at /coverage/): per file and line, line/branch/function
percentages, source annotated. Nothing here renders HTML by hand.

Oracle side: the recompiled original (cake-taste host) replays the same movie with `N64R_GOLDEN_DIR`; its
`trace-final.tsv` (symbol, first VI, thread, count) is the per-function first-entry record the host already writes.
The join with the port is by function name (the versions share names). Not a gcov build of the recomp: see RESULT.
Behaviour: the host also writes cumulative entry counts every shot_every polls (`trace-poll-N.tsv`); `behaviour`
judges each function by the port's gcov count against the original's counts around the port's last poll, so the
table says "executed and behaves like the original", not only "executed" (granularity: TAS-CORPUS-OPUS-1 DESIGN.md).

The universe for percentages by bytes is every function of the version's ELF (`readelf -sW`, MIPS sizes), keyed by
name with the versions that define it. A row is scope `sdk` when its file is under `src/sdk/` (the port replaces
those with its shim), other when no source defines it (compiler runtime, asm-only), else game.
Section `tas.coverage`: {"elf": ".../build/{version}/RageWars.elf", "version":
"us-rev1", "versions": [...], "source": folder with src/ (default: tas.serve.pin), "oracle": {"argv": [...],
"env": {...}} with {movie}, {dir}, {trace} in values}.
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
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, tas_oracle, tas_symbolize
from abu_decomp.config import Profile, check_keys

COVERAGE, GCOV, ORACLE_DIR, TABLE, HTML = "coverage.json", "gcov", "oracle-golden", "coverage-table.json", "coverage"
_KEYS = frozenset({"elf", "version", "versions", "source"})
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
        "source": profile.path("tas.coverage.source", pin) if pin else None,
    }


# --- the two sides' hit files ---


def read_gcovr(path: Path) -> dict[str, int] | None:
    """name -> execution count of every function in a gcovr JSON report (same-named statics add up); None when
    the file is missing or unreadable. The counts are kept beside it (`<report>-fns.json`, ~100 KB against the
    report's ~13 MB) while newer than it: a union over every movie otherwise parses gigabytes each time."""
    cache = path.with_name(path.stem + "-fns.json")
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
        for fn in entry.get("functions", []):
            name = str(fn.get("name", ""))
            if name:
                counts[name] = counts.get(name, 0) + int(fn.get("execution_count", 0))
    try:
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps(counts))
        tmp.replace(cache)
    except OSError:
        pass
    return counts


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


def merge_build(group: str, paths: list[Path], cache: Path, build: Path) -> Path:
    """One gcovr JSON for the runs of one port build (same source, so gcovr's strict merge holds), kept in `cache`
    under the run set's digest: an old build's runs are merged once, not on every union. A lone run is its own."""
    if len(paths) == 1:
        return paths[0]
    key = hashlib.md5("\n".join(sorted(str(p) for p in paths)).encode()).hexdigest()[:12]
    out = cache / f"{group[:12]}-{key}.json"
    if not out.is_file():
        tmp = out.with_suffix(".tmp")
        args = [*_gcovr_base(build), "--json", str(tmp)]
        for path in paths:
            args += ["--add-tracefile", str(path)]
        gcovr(args, build)
        tmp.replace(out)
    return out


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
    """The oracle's cumulative entry counts at the snapshots around `poll` (RUN/oracle-golden/trace-poll-N.tsv, one
    every shot_every polls): (counts at the last snapshot <= poll, counts at the first snapshot >= poll, None when the
    oracle ended before `poll`). None when the run holds no snapshots."""
    folder = run / ORACLE_DIR
    polls = sorted(int(m[1]) for p in folder.glob("trace-poll-*.tsv") if (m := _SNAPSHOT.fullmatch(p.name)))
    if not polls:
        return None
    below = [p for p in polls if p <= poll]
    above = [p for p in polls if p >= poll]
    low = _entry_counts(folder / f"trace-poll-{below[-1]}.tsv") if below else {}
    high = _entry_counts(folder / f"trace-poll-{above[0]}.tsv") if above else None
    return low, high


def behaviour(port: dict[str, int], bracket: tuple[dict[str, int], dict[str, int] | None]) -> dict[str, str]:
    """Per function the port's build defines and either side entered: `behaves` when the port's entry count at its last
    poll lies between the oracle's counts at the snapshots around that poll (at least the lower one when the oracle
    ended earlier), `missing` when the port never entered what the original had by then, `port-only` when the
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


def universe(cov: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """One row per function name over all versions' ELFs, and each version's function count."""
    rows: dict[str, dict[str, Any]] = {}
    totals: dict[str, int] = {}
    for version in cov["versions"]:
        elf = Path(cov["elf"].format(version=version))
        funcs = elf_functions(elf)
        totals[version] = len(funcs)
        for name, (address, size) in funcs.items():
            row = rows.setdefault(name, {"name": name, "versions": [], "address": 0, "size": 0})
            row["versions"].append(version)
            if version == cov["version"] or not row["size"]:
                row["address"], row["size"] = address, size
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
        out = {k: row[k] for k in ("name", "file", "scope", "size", "versions")}
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
        "summary": report.get("summary"),
        "gcovr": report.get("gcovr"),
        "builds": report.get("builds"),
        "changed_files": report.get("changed_files"),
        "dropped_functions": report.get("dropped_functions") or [],
        "mixed_build_count": len(report.get("mixed_build") or []),
        "mixed_build": (report.get("mixed_build") or [])[:limit],
        "oracle_only": pick("oracle-only"),
        "neither": pick("neither"),
        "port_only": pick("port-only"),
    }


# --- driving it ---

FILTER = ".*/src/"  # the original game's sources (not the headers, not the generated stubs)


def build_dir(cfg: dict[str, Any]) -> Path:
    """The CMake build tree of the page's port: where the .gcno files and the `ragewars-decomp` source link are."""
    return Path(cfg["port"]).parent


def _gcovr_base(build: Path) -> list[str]:
    if not (build / "ragewars-decomp").exists():
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
    link, dropped = build / "ragewars-decomp", 0
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
        gcovr([*_gcovr_base(build), "--json", str(path), str(build / "decomp"), str(build / "decomp-image")], build)
    return path


def latest_runs(cfg: dict[str, Any]) -> dict[str, Path]:
    """movie name -> its newest movie-driven finished run folder that left a coverage report or counters."""
    from abu_decomp import tas

    newest: dict[str, Path] = {}
    for run, row in tas.run_rows(cfg):
        if row.get("status") == "running" or row.get("input") == "human" or (run / tas.TAKEN).exists():
            continue
        if not ((run / COVERAGE).is_file() or (run / GCOV).is_dir()):
            continue
        name = str(row.get("movie") or "")
        if name and (name not in newest or run.name > newest[name].name):
            newest[name] = run
    return newest


def build(profile: Profile, cfg: dict[str, Any], *, oracle: bool = True, jobs: int = 4) -> dict[str, Any] | None:
    """Union the newest run of every movie: runs/coverage/ (gcovr `--html-details` over the baseline and every
    run's JSON), runs/coverage-table.json (the function table with the oracle). None without a `tas.coverage`
    section. With `oracle`, a run with no oracle trace yet is replayed in the oracle first."""
    from concurrent.futures import ThreadPoolExecutor

    from abu_decomp import tas

    cov = settings(profile, cfg)
    if cov is None:
        return None
    runs = latest_runs(cfg)
    drop_orphan_notes(build_dir(cfg))

    def one(item: tuple[str, Path]) -> dict[str, Any]:
        name, run = item
        if not (run / COVERAGE).is_file():
            run_report(profile, cfg, run)
        record = tas.read_record(run) or {}
        got = read_oracle(run)
        if got is None and oracle and run_oracle(profile, cfg, run, record):
            got = read_oracle(run)
        port = read_gcovr(run / COVERAGE) or {}
        bracket = oracle_bracket(run, int(record.get("polls_reached") or 0))
        return {
            "movie": name,
            "run": run.name,
            "port": port,
            "behaviour": behaviour(port, bracket) if bracket else None,
            "oracle": got[0] if got else None,
            "port_polls": int(record.get("polls_reached") or 0),
            "oracle_polls": got[1] if got else 0,
            "build": str(record.get("build") or "unknown"),
        }

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        collected = list(pool.map(one, sorted(runs.items())))
    build_tree = build_dir(cfg)
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
        merged = merge_build(group, paths, cache, build_tree)
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
    page.mkdir(parents=True)
    args = [
        *_gcovr_base(build_tree),
        "--html-details",
        str(page / "index.html"),
        "--html-title",
        "Rage Wars game code: TAS movies",
        "--json-summary",
        str(summary_file),
    ]
    for path in tracefiles:
        args += ["--add-tracefile", str(path)]
    gcovr(args, build_tree)
    rows, totals = universe(cov)
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
    report["changed_files"] = {group: len(files) for group, files in changed.items() if files}
    report["baseline"] = tag
    dropped = set().union(*changed.values()) if changed else set()
    # the functions whose older-build counts were dropped, by name: a smaller union shows what it lost
    report["dropped_functions"] = [
        {"file": file, "function": name}
        for file, _, name in sorted(key.partition(":") for key in spans(base))
        if file in dropped
    ]
    report["changed_functions"] = len(report["dropped_functions"])
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
    why = refusal(read_table(cfg), report)
    if why:  # never replace a report with one covering fewer functions: the new one lands beside it
        report["refused"] = why
        side = path.with_name("coverage-table.refused.json")
        side.write_text(json.dumps(report, sort_keys=True) + "\n")
        shutil.rmtree(page, ignore_errors=True)
        summary_file.unlink(missing_ok=True)
        return report
    shutil.rmtree(final_page, ignore_errors=True)
    page.replace(final_page)
    summary_file.replace(final_summary)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, sort_keys=True) + "\n")
    tmp.replace(path)
    return report


def covered_functions(report: dict[str, Any] | None) -> int:
    """Functions gcovr counted as covered in a coverage table (0 for none or an unreadable one)."""
    return int(((report or {}).get("gcovr") or {}).get("function_covered") or 0)


def refusal(old: dict[str, Any] | None, new: dict[str, Any]) -> str | None:
    """Why `new` must not replace `old`: it covers fewer functions. On the same baseline (port build) any loss is lost
    counters. Drawn on another build, the union drops the source files that changed since the older runs, so it may
    lose at most the functions those files define (`changed_functions`); a bigger loss is still refused."""
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
        return f"coverage report NOT replaced: new union {report['refused']} (beside it: coverage-table.refused.json)"
    pct = lambda key: f"{g[key]:.1f}%" if g.get(key) is not None else "?"  # noqa: E731
    return (
        f"coverage {s['version']} gcov: lines {pct('line_percent')}, branches {pct('branch_percent')}, functions "
        f"{pct('function_percent')}; port {s['game']['port']['bytes_pct']}% of game bytes; oracle-only "
        f"{states.get('oracle-only', 0)}, neither {states.get('neither', 0)}; behaves "
        f"{s.get('behaviour', {}).get('behaves', 0)}, differs {s.get('behaviour', {}).get('differs', 0)} "
        f"({s['movies']} movies, {s['oracle_movies']} with oracle; {len(report.get('builds') or {})} port builds, "
        f"{len(report.get('mixed_build') or [])} mixed-build functions); report runs/{HTML}/index.html"
    )
