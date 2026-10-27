"""`decomp tas corpus`: the regression corpus of movies, problem spots, and the speedrun gate over both.

Design note: RageWars100 lanes/TAS-CORPUS-OPUS-1/DESIGN.md (prior art: coverage-guided fuzzing corpora and cmin,
snapshot tests, known-failure tracking, TAS sync checks, differential testing).

Corpus (`<tas.movies>/corpus.json`, beside the movies it names): `entries` {movie: {why, added, baseline, oracle,
spots}} and `spots` {id: spot}. An entry's `baseline` is a run folder (runs/plays) of that movie: its record, RDRAM
hash per poll and gcovr report are the golden the gate compares against; `oracle` names a run whose oracle-golden
holds the original game's per-poll entry counts for that movie (the recomp is deterministic and never changes with
our code, so its side is taken once per movie); `spots` the status of each spot at the baseline. The archive keeps
every run the corpus names (`kept_runs`).
- Auto-keep (`offer`, called by `tas.play` for every finished movie run on the profile's port): a movie not in the
  corpus whose run reaches features no baseline reaches is kept with that run as its baseline. Features: functions
  entered (`f:`), branches taken (`b:file:line:i`) from the run's gcovr JSON, and a non-ok end's subject (`o:`, one
  per crash).
- Hand keep (`add`, why `hand`) and spot keep (why `spot:ID`) are never minimised away; `minimise` keeps a greedy
  set cover of the other entries' features, fewest polls first, then by name, so the same inputs give the same
  corpus.
- Problem spots: checks that describe correct behaviour, recorded with the movie that shows them. `ram`: a typed
  value at a guest address compared at poll P, read from the port's RDRAM dump at P (`N64C_TAS_DUMP_AT`; host byte
  order, the port's RDRAM holds what the game's C stored); `function`: reached, unreached or reached at least N
  times (gcov count). Status pass / fail / unreached (the run ended before P); a spot written for an open bug fails
  until it is fixed.
- Gate (`gate`): every entry replayed at turbo (`tas.play`, label `corpus-gate`, so every gate run is on the page),
  in parallel: any non-ok outcome fails, even with an identical or missing baseline. Compared with its baseline,
  a worse outcome, a spot that passed and no longer does, or a function
  that behaved like the original (tas_coverage.call_behaviour) and no longer does is `fail`; better outcome, a spot or
  function that now passes is `improved`; another RDRAM hash at some poll or other functions entered is `changed`
  (shown, not judged); else `pass`. Exit 1 on a fail. A gate on the profile's port moves every non-failed entry's
  baseline to its new run and minimises; `accept` moves failed ones on purpose; a `--port` gate (a lane's own build)
  moves nothing. The last gate is `<tas.runs>/corpus-gate.json`, which `tas serve` shows; serve runs the gate itself
  when the port binary changes.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import resource
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, slots, tas, tas_coverage, tas_health, tas_oracle
from abu_decomp.behave import prototype
from abu_decomp.config import Profile

CORPUS, GATE = "corpus.json", "corpus-gate.json"
GATE_LABEL, SPOT_LABEL = "corpus-gate", "corpus"
HAND = "hand"
PINNED_WHY = (HAND, "spot:")
_SPOT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
_RAM = re.compile(
    r"\s*([us])(8|16|32)@(0x[0-9a-fA-F]{1,8}|D_[0-9a-fA-F]{8}\w*)\s*(==|!=|<=|>=|<|>|&)\s*(-?0x[0-9a-fA-F]+|-?\d+)\s*"
)
EXPECTS = ("reached", "unreached")
LISTED = 40  # behaviour names a gate row lists per direction


# --- the corpus file ---


def corpus_path(cfg: dict[str, Any]) -> Path:
    return cfg["movies"] / CORPUS


def gate_path(cfg: dict[str, Any]) -> Path:
    return cfg["runs"] / GATE


@contextlib.contextmanager
def locked(cfg: dict[str, Any]) -> Iterator[None]:
    """flock on the movies folder: offers from parallel replays (play --all) and gates write the corpus in turn."""
    cfg["movies"].mkdir(parents=True, exist_ok=True)
    fd = os.open(cfg["movies"], os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def load(cfg: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(corpus_path(cfg).read_text())
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as error:
        raise DecompError("tas.corpus", f"{corpus_path(cfg)} is unreadable: {error}") from error
    return {"entries": dict(data.get("entries") or {}), "spots": dict(data.get("spots") or {})}


def store(cfg: dict[str, Any], data: dict[str, Any]) -> None:
    path = corpus_path(cfg)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"schema": 1, **data}, indent=1, sort_keys=True) + "\n")
    tmp.replace(path)


def read_gate(cfg: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return json.loads(gate_path(cfg).read_text())
    except (OSError, ValueError):
        return None


def kept_runs(cfg: dict[str, Any]) -> set[str]:
    """Run ids the corpus needs (baselines, oracle references, the last gate's runs): never archived."""
    try:
        data = load(cfg)
    except DecompError:
        return set()
    kept = {str(v) for e in data["entries"].values() for v in (e.get("baseline"), e.get("oracle")) if v}
    gate = read_gate(cfg) or {}
    return kept | {str(r["run"]) for r in gate.get("rows", []) if r.get("run")}


def pinned(entry: dict[str, Any]) -> bool:
    return any(w == HAND or w.startswith("spot:") for w in entry.get("why", []))


# --- features ---

_features: dict[tuple[str, int], frozenset[str]] = {}


def features(run: Path) -> frozenset[str]:
    """What the run reached: functions entered, branches taken (its gcovr JSON) and a non-ok end's subject."""
    report = run / tas_coverage.COVERAGE
    try:
        key = (str(report), report.stat().st_mtime_ns)
    except OSError:
        key = (str(report), 0)
    if key not in _features:
        found: set[str] = set()
        with contextlib.suppress(OSError, ValueError):
            for entry in json.loads(report.read_text()).get("files", []):
                name = entry.get("file", "")
                found.update(f"f:{f['name']}" for f in entry.get("functions", []) if f.get("execution_count"))
                for line in entry.get("lines", []):
                    for i, branch in enumerate(line.get("branches", [])):
                        if branch.get("count"):
                            found.add(f"b:{name}:{line.get('line_number')}:{i}")
        record = tas.read_record(run) or {}
        if record.get("outcome") not in (None, "ok"):
            found.add(f"o:{record['outcome']}:{record.get('subject') or ''}")
        _features[key] = frozenset(found)
    return _features[key]


def _run(cfg: dict[str, Any], run_id: Any) -> Path | None:
    return tas.run_folder(cfg, str(run_id)) if run_id else None


def entry_features(cfg: dict[str, Any], entry: dict[str, Any]) -> frozenset[str]:
    run = _run(cfg, entry.get("baseline"))
    return features(run) if run else frozenset()


def union(cfg: dict[str, Any], entries: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for entry in entries.values():
        out |= entry_features(cfg, entry)
    return out


def summarise(new: set[str] | frozenset[str], head: str = "new coverage") -> str:
    fns = sum(1 for f in new if f.startswith("f:"))
    branches = sum(1 for f in new if f.startswith("b:"))
    ends = sum(1 for f in new if f.startswith("o:"))
    return f"{head} +{fns} fn +{branches} br" + (f" +{ends} end" if ends else "")


def offer(profile: Profile, cfg: dict[str, Any], run: Path, record: dict[str, Any]) -> dict[str, Any] | None:
    """Keep the run's movie when the run reaches features the corpus does not (its run becomes the baseline). Only a
    finished, unstopped movie run on the profile's own port that left a coverage report counts; a gate's or a spot's
    replay never does (their movies are already in)."""
    movie = str(record.get("movie") or "")
    if (
        not movie
        or record.get("outcome") is None
        or record.get("stopped")
        or record.get("taken")
        or record.get("label") in (GATE_LABEL, SPOT_LABEL)
        or record.get("input") != "movie"
        or Path(str(record.get("port") or "")) != Path(cfg["port"])
        or not (run / tas_coverage.COVERAGE).is_file()
    ):
        return None
    with locked(cfg):
        data = load(cfg)
        if movie in data["entries"]:
            return None
        new = features(run) - union(cfg, data["entries"])
        if not new:
            return None
        data["entries"][movie] = {"why": [summarise(new)], "added": time.time(), "baseline": run.name}
        store(cfg, data)
    return {"kept": movie, "why": summarise(new)}


def baseline_polls(cfg: dict[str, Any], entry: dict[str, Any]) -> int:
    run = _run(cfg, entry.get("baseline"))
    return int(((tas.read_record(run) if run else None) or {}).get("polls") or 0)


def minimise(cfg: dict[str, Any], entries: dict[str, Any]) -> list[str]:
    """Drop automatic entries whose features the others already cover (greedy set cover: pinned entries first, then the
    entry adding the most uncovered features, fewest polls then name on ties). Returns the dropped movies."""
    feats = {m: entry_features(cfg, e) for m, e in entries.items()}
    polls = {m: baseline_polls(cfg, e) for m, e in entries.items()}
    covered: set[str] = set()
    for movie, entry in entries.items():
        if pinned(entry):
            covered |= feats[movie]
    left = sorted((m for m, e in entries.items() if not pinned(e)), key=lambda m: (polls[m], m))
    chosen: set[str] = set()
    while left:
        best = max(left, key=lambda m: (len(feats[m] - covered), -polls[m], [-ord(c) for c in m]))
        if not feats[best] - covered:
            break
        chosen.add(best)
        covered |= feats[best]
        left.remove(best)
    dropped = sorted(left)
    for movie in dropped:
        del entries[movie]
    return dropped


# --- problem spots ---


def parse_ram(expr: str) -> dict[str, Any]:
    """`u32@0x80111D24 == 0xE3F635E3` (u8/u16/u32/s8/s16/s32; an address or a D_XXXXXXXX name; == != < <= > >= or &,
    which passes when the masked bits are not all zero)."""
    match = _RAM.fullmatch(expr or "")
    if not match:
        raise DecompError("tas.corpus", f"a RAM check is like 'u32@0x80111D24 == 0xE3F635E3', not {expr!r}")
    sign, bits, where, op, value = match.groups()
    address = int(where[2:10], 16) if where.startswith("D_") else int(where, 16)
    if not 0x80000000 <= address < 0xC0000000:
        raise DecompError("tas.corpus", f"{where} is not a guest RDRAM address (0x80000000.., KSEG0/KSEG1)")
    return {"type": f"{sign}{bits}", "address": address, "op": op, "value": int(value, 0)}


def ram_value(dump: Path, kind: str, address: int) -> int | None:
    """The typed value at a guest address in an RDRAM dump (host byte order), None when the dump lacks it."""
    size = int(kind[1:]) // 8
    offset = address & 0x1FFFFFFF
    try:
        with dump.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(size)
    except OSError:
        return None
    if len(data) != size:
        return None
    return int.from_bytes(data, "little", signed=kind[0] == "s")


_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "&": lambda a, b: bool(a & b),
}


def make_spot(
    movie: str, ram: str | None, at: int | None, function: str | None, expect: str | None, note: str
) -> dict[str, Any]:
    if bool(ram) == bool(function):
        raise DecompError("tas.corpus", "a spot is one check: --ram EXPR --at POLL, or --function NAME --expect ...")
    spot: dict[str, Any] = {"movie": movie, "note": " ".join(note.split())}
    if ram:
        if at is None or at < 1:
            raise DecompError("tas.corpus", "a RAM spot needs --at POLL (>= 1): the poll its value is read at")
        spot.update(kind="ram", at=at, expr=" ".join(ram.split()), **parse_ram(ram))
        return spot
    expect = expect or "reached"
    if expect not in EXPECTS and not expect.isdigit():
        raise DecompError("tas.corpus", f"--expect is reached, unreached or a minimum count, not {expect!r}")
    spot.update(kind="function", function=function, expect=expect)
    return spot


def evaluate(spot: dict[str, Any], run: Path, record: dict[str, Any]) -> dict[str, Any]:
    """{status: pass|fail|unreached, value?} of one spot over one finished run."""
    if spot["kind"] == "ram":
        if int(record.get("polls_reached") or 0) < spot["at"]:
            return {"status": "unreached"}
        value = ram_value(run / tas.RAM_DUMP.format(spot["at"]), spot["type"], spot["address"])
        if value is None:
            return {"status": "unreached"}
        return {"status": "pass" if _OPS[spot["op"]](value, spot["value"]) else "fail", "value": value}
    counts = tas_coverage.read_gcovr(run / tas_coverage.COVERAGE)
    if counts is None:
        return {"status": "unreached"}
    count = counts.get(spot["function"], 0)
    expect = spot["expect"]
    ok = count > 0 if expect == "reached" else count == 0 if expect == "unreached" else count >= int(expect)
    return {"status": "pass" if ok else "fail", "value": count}


def spots_for(data: dict[str, Any], movie: str) -> dict[str, dict[str, Any]]:
    """The spots checked on a movie's replays: its own, plus every function spot written for no movie."""
    return {
        sid: s
        for sid, s in sorted(data["spots"].items())
        if s.get("movie") == movie or (not s.get("movie") and s["kind"] == "function")
    }


def dump_polls(spots: dict[str, dict[str, Any]]) -> list[int | None]:
    """The RDRAM dump polls a movie's replays need: one replay per distinct RAM-spot poll (the port dumps once per run),
    a single plain replay without RAM spots."""
    polls = sorted({s["at"] for s in spots.values() if s["kind"] == "ram"})
    return list(polls) or [None]


def _replays(
    profile: Profile, movie: str, polls: list[int | None], label: str, health: Any, port: Path | str | None
) -> list[dict[str, Any]]:
    """The movie's replays for these dump polls (the first one is the entry's run), each a tas.play result."""
    out = []
    for poll in polls:
        out.append(tas.play(profile, movie, label=label, dump_ram_at=poll, health=health, port=port))
    return out


def _judge(spots: dict[str, dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each spot's verdict from the replay that dumped at its poll (function spots: the first replay)."""
    judged = {}
    for sid, spot in spots.items():
        result = next((r for r in results if spot["kind"] != "ram" or r.get("dump_at") == spot["at"]), results[0])
        run = Path(result["run"])
        judged[sid] = evaluate(spot, run, tas.read_record(run) or {})
    return judged


def _drop_dumps(results: list[dict[str, Any]]) -> None:
    """RDRAM dumps (8 MB each) go once their values are read; the verdicts keep the values."""
    for result in results:
        for path in Path(result["run"]).glob("ram-*.bin"):
            path.unlink(missing_ok=True)


def add_spot(
    profile: Profile,
    spot_id: str,
    movie: str,
    *,
    ram: str | None = None,
    at: int | None = None,
    function: str | None = None,
    expect: str | None = None,
    note: str = "",
    replace: bool = False,
) -> dict[str, Any]:
    """Define spot `spot_id` on `movie`, replay the movie once to take its first status, and keep the movie in the
    corpus (why `spot:ID`; the replay is its baseline when it had none)."""
    cfg = tas.settings(profile)
    if not _SPOT_ID.fullmatch(spot_id):
        raise DecompError("tas.corpus", f"spot ids are lower-case words joined by '-', not {spot_id!r}")
    tas.movie_polls(cfg, movie)
    spot = make_spot(movie, ram, at, function, expect, note)
    with locked(cfg):
        if spot_id in load(cfg)["spots"] and not replace:
            raise DecompError("tas.corpus", f"spot {spot_id} exists; pass --replace to redefine it")
    results = _replays(profile, movie, dump_polls({spot_id: spot}), SPOT_LABEL, None, None)
    for r, poll in zip(results, dump_polls({spot_id: spot}), strict=True):
        r["dump_at"] = poll
    verdict = _judge({spot_id: spot}, results)[spot_id]
    _drop_dumps(results)
    with locked(cfg):
        data = load(cfg)
        data["spots"][spot_id] = {**spot, "added": time.time(), "first": verdict["status"]}
        entry = data["entries"].setdefault(movie, {"why": [], "added": time.time(), "baseline": results[0]["id"]})
        entry["why"] = sorted(set(entry.get("why", [])) | {f"spot:{spot_id}"})
        entry.setdefault("spots", {})[spot_id] = verdict["status"]
        store(cfg, data)
    return {"spot": spot_id, **data["spots"][spot_id], "verdict": verdict, "run": results[0]["id"]}


def drop_spot(profile: Profile, spot_id: str) -> dict[str, Any]:
    cfg = tas.settings(profile)
    with locked(cfg):
        data = load(cfg)
        if spot_id not in data["spots"]:
            raise DecompError("tas.corpus", f"no spot {spot_id}")
        del data["spots"][spot_id]
        for entry in data["entries"].values():
            entry.get("spots", {}).pop(spot_id, None)
            entry["why"] = [w for w in entry.get("why", []) if w != f"spot:{spot_id}"] or ["unpinned spot"]
        store(cfg, data)
    return {"dropped": spot_id}


# --- hand keep, seed, list ---


def newest_run(cfg: dict[str, Any], movie: str, build: str) -> Path | None:
    """The newest finished, unstopped, untaken movie run of `movie` on this port build with a coverage report."""
    for run, row in reversed(tas.run_rows(cfg)):
        if row.get("movie") != movie or row.get("build") != build or row.get("input") != "movie":
            continue
        finished = row.get("status") == "done" and row.get("outcome") and not row.get("stopped")
        if finished and not row.get("taken") and (run / tas_coverage.COVERAGE).is_file():
            return run
    return None


def oracle_reference(cfg: dict[str, Any], movie: str) -> str | None:
    """The newest run of `movie` (any port build: the original does not change with ours) whose oracle-golden holds the
    per-poll entry counts, with the same Expansion Pak and card as the movie."""
    try:
        pak = tas.movie_pak(cfg, movie)
    except (DecompError, OSError):
        return None
    for run, row in reversed(tas.run_rows(cfg)):
        if row.get("movie") != movie or row.get("taken") or row.get("pak", "on") != pak:
            continue
        if any((run / tas_coverage.ORACLE_DIR).glob("trace-poll-*.tsv")):
            return run.name
    return None


def add(profile: Profile, movie: str, why: str = "") -> dict[str, Any]:
    """Keep `movie` by hand (never minimised away); its newest run on this build is the baseline, else one replay."""
    cfg = tas.settings(profile)
    tas.movie_polls(cfg, movie)
    run = newest_run(cfg, movie, tas.build_id(cfg["port"]))
    if run is None:
        run = Path(tas.play(profile, movie, label=SPOT_LABEL)["run"])
    with locked(cfg):
        data = load(cfg)
        entry = data["entries"].setdefault(movie, {"why": [], "added": time.time(), "baseline": run.name})
        entry["why"] = sorted(set(entry.get("why", [])) | {HAND} | ({f"hand: {why}"} if why else set()))
        entry.setdefault("oracle", oracle_reference(cfg, movie))
        store(cfg, data)
    return {"movie": movie, **entry}


def drop(profile: Profile, movie: str) -> dict[str, Any]:
    cfg = tas.settings(profile)
    with locked(cfg):
        data = load(cfg)
        if movie not in data["entries"]:
            raise DecompError("tas.corpus", f"{movie} is not in the corpus")
        if any(s.get("movie") == movie for s in data["spots"].values()):
            raise DecompError("tas.corpus", f"{movie} carries problem spots; drop those first (corpus spot drop ID)")
        del data["entries"][movie]
        store(cfg, data)
    return {"dropped": movie}


def seed(profile: Profile, jobs: int = 0) -> dict[str, Any]:
    """Offer every movie: its newest run on this build (replayed when it has none) joins as a candidate, then the
    corpus is minimised over all of them. Existing entries stay candidates; pinned ones always stay."""
    cfg = tas.settings(profile)
    build = tas.build_id(cfg["port"])
    names = sorted(p.stem for p in cfg["movies"].glob("*.tas")) if cfg["movies"].is_dir() else []
    health = tas_health.read(cfg, profile)
    missing = [n for n in names if newest_run(cfg, n, build) is None]

    def replay(name: str) -> None:
        with contextlib.suppress(DecompError):
            tas.play(profile, name, label=SPOT_LABEL, health=health)

    with ThreadPoolExecutor(max_workers=max(1, jobs or slots.capacity())) as pool:
        list(pool.map(replay, missing))
    with locked(cfg):
        data = load(cfg)
        before = set(data["entries"])
        for name in names:
            run = newest_run(cfg, name, build)
            if run is None or name in data["entries"]:
                continue
            data["entries"][name] = {"why": ["seed"], "added": time.time(), "baseline": run.name}
        for name, entry in data["entries"].items():
            entry["oracle"] = entry.get("oracle") or oracle_reference(cfg, name)
        dropped = minimise(cfg, data["entries"])
        for name in set(data["entries"]) - before:
            entry = data["entries"][name]
            entry["why"] = [summarise(entry_features(cfg, entry), "seed")]
        store(cfg, data)
    kept = sorted(data["entries"])
    return {"replayed": missing, "offered": len(names), "kept": kept, "dropped": dropped, **view(cfg)}


def view(cfg: dict[str, Any]) -> dict[str, Any]:
    """The page's and `corpus list`'s view: entries, spots, the union's size and the last gate."""
    data = load(cfg)
    gate = read_gate(cfg)
    rows = {r["movie"]: r for r in (gate or {}).get("rows", [])}
    feats = union(cfg, data["entries"])
    entries = []
    for movie, entry in sorted(data["entries"].items()):
        record = tas.read_record(_run(cfg, entry.get("baseline")) or Path("/")) or {}
        row = rows.get(movie, {})
        entries.append(
            {
                "movie": movie,
                "why": entry.get("why", []),
                "polls": int(record.get("polls") or 0),
                "outcome": record.get("outcome"),
                "baseline": entry.get("baseline"),
                "oracle": entry.get("oracle"),
                "spots": entry.get("spots", {}),
                "verdict": row.get("verdict"),
                "reasons": row.get("reasons", []),
                "first_change": row.get("first_change"),
            }
        )
    spots = [{"id": sid, **s} for sid, s in sorted(data["spots"].items())]
    summary = {k: v for k, v in (gate or {}).items() if k != "rows"} if gate else None
    if gate:
        summary["failed"] = [r for r in gate.get("rows", []) if r.get("verdict") == "fail"]
    return {
        "entries": entries,
        "spots": spots,
        "features": {
            "functions": sum(1 for f in feats if f.startswith("f:")),
            "branches": sum(1 for f in feats if f.startswith("b:")),
            "ends": sum(1 for f in feats if f.startswith("o:")),
        },
        "gate": summary,
    }


# --- the gate ---


def first_change(base: Path, now: Path) -> int | None:
    """The first poll whose RDRAM hash differs between two runs (or that only one reached); None when they agree."""
    try:
        a = (base / tas.HASH).read_text().splitlines()
        b = (now / tas.HASH).read_text().splitlines()
    except OSError:
        return None
    for i, (x, y) in enumerate(zip(a, b, strict=False)):
        if x != y:
            return int(x.split()[0]) if x.split() and x.split()[0].isdigit() else i + 1
    return None if len(a) == len(b) else min(len(a), len(b)) + 1


def behaviour_of(cfg: dict[str, Any], run: Path, oracle: str | None) -> dict[str, str] | None:
    """Per function the per-call verdict of this run's port calls against the reference run's original calls."""
    reference = _run(cfg, oracle)
    if reference is None:
        return None
    judged = tas_coverage.call_behaviour(run, str, prototype.inferred, reference)
    return None if judged is None else {name: v["verdict"] for name, v in judged.items()}


def outcome_failure(record: dict[str, Any]) -> str | None:
    """A gate requires a clean end; a baseline cannot excuse a crash, hang or missing result."""
    if record.get("outcome") != "ok":
        return f"outcome must be ok: {record.get('outcome')} at poll {record.get('polls_reached') or 0}"
    return None


def compare(
    cfg: dict[str, Any],
    entry: dict[str, Any],
    base: Path | None,
    now: Path,
    judged: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """One gate row: the entry's verdict against its baseline and the reasons."""
    record = tas.read_record(now) or {}
    row: dict[str, Any] = {
        "run": now.name,
        "outcome": record.get("outcome"),
        "subject": record.get("subject"),
        "polls_reached": int(record.get("polls_reached") or 0),
        "spots": {sid: v["status"] for sid, v in judged.items()},
        "spot_values": {sid: v.get("value") for sid, v in judged.items() if "value" in v},
    }
    fail, better, changed = [], [], []
    failure = outcome_failure(record)
    before = tas.read_record(base) if base else None
    if before is None:
        row.update(
            verdict="fail" if failure else "new",
            reasons=[failure] if failure else ["no baseline run: this run becomes it"],
            baseline=None,
            first_change=None,
        )
        return row
    row["baseline"] = base.name
    standing = tas.compare(before, record)
    was = f"{before.get('outcome')} {before.get('polls_reached')}"
    if standing == "worse":
        fail.append(f"outcome worse: {was} -> {record.get('outcome')} {row['polls_reached']}")
    elif standing == "better":
        better.append(f"outcome better: {was} -> {record.get('outcome')} {row['polls_reached']}")
    if failure:
        fail.append(failure)
    for sid, verdict in judged.items():
        old = entry.get("spots", {}).get(sid)
        if old == "pass" and verdict["status"] != "pass":
            fail.append(f"spot {sid}: pass -> {verdict['status']} (value {verdict.get('value')})")
        elif old in ("fail", "unreached") and verdict["status"] == "pass":
            better.append(f"spot {sid}: {old} -> pass")
    old_b, new_b = behaviour_of(cfg, base, entry.get("oracle")), behaviour_of(cfg, now, entry.get("oracle"))
    if old_b is not None and new_b is not None:
        lost = sorted(n for n, s in old_b.items() if s == "behaves" and new_b.get(n, "behaves") != "behaves")
        won = sorted(n for n, s in new_b.items() if s == "behaves" and old_b.get(n) not in (None, "behaves"))
        row["behaviour"] = {
            "behaves": sum(1 for s in new_b.values() if s == "behaves"),
            "differs": sum(1 for s in new_b.values() if s != "behaves"),
            "lost": lost[:LISTED],
            "won": won[:LISTED],
        }
        if lost:
            fail.append(f"{len(lost)} functions no longer behave like the original: {', '.join(lost[:5])}")
        if won:
            better.append(f"{len(won)} functions now behave like the original")
    row["first_change"] = first_change(base, now)
    if row["first_change"] is not None:
        changed.append(f"RDRAM differs from poll {row['first_change']}")
    old_f = {f for f in features(base) if f.startswith("f:")}
    new_f = {f for f in features(now) if f.startswith("f:")}
    if old_f != new_f:
        changed.append(f"functions entered +{len(new_f - old_f)} -{len(old_f - new_f)}")
    row["verdict"] = "fail" if fail else "improved" if better else "changed" if changed else "pass"
    row["reasons"] = fail + better + changed
    return row


def pins_stamp(cfg: dict[str, Any]) -> dict[str, Any] | None:
    """The page port's built pins (tas.build.pins: rage-ports, shim and RW shas) the gate ran on."""
    path = cfg.get("build", {}).get("pins")
    try:
        return json.loads(Path(path).read_text()) if path else None
    except (OSError, ValueError):
        return None


def _cpu() -> float:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def made_reference(profile: Profile, cfg: dict[str, Any], run: Path) -> str | None:
    """Replay the run's movie in the original (tas_coverage.run_oracle, golden dir on) so the run holds the original's
    per-poll entry counts: the entry's oracle reference from now on. None without an oracle host."""
    if tas_oracle.settings(profile) is None:
        return None
    with contextlib.suppress(DecompError, OSError):
        tas_coverage.run_oracle(profile, cfg, run, tas.read_record(run) or {})
    return run.name if any((run / tas_coverage.ORACLE_DIR).glob("trace-poll-*.tsv")) else None


def gate(
    profile: Profile, *, port: Path | str | None = None, jobs: int = 0, oracle: bool = True
) -> tuple[dict[str, Any], int]:
    """Replay every corpus entry at turbo and judge it against its baseline; exit status 1 on any fail. On the
    profile's own port the non-failed entries' baselines move to this gate's runs and the corpus is minimised. With
    `oracle`, an entry without an oracle reference gets one: the original replays its movie once beside this run."""
    cfg = tas.settings(profile)
    cfg["runs"].mkdir(parents=True, exist_ok=True)
    fd = os.open(cfg["runs"], os.O_RDONLY)  # one gate at a time (serve's own and a shell's)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise DecompError(
                "tas.corpus", "another corpus gate is running; its result lands in corpus-gate.json"
            ) from error
        return _gate(profile, cfg, port, jobs, oracle)
    finally:
        os.close(fd)


def _gate(
    profile: Profile, cfg: dict[str, Any], port: Path | str | None, jobs: int, oracle: bool
) -> tuple[dict[str, Any], int]:
    exe = tas.with_port(cfg, port)["port"]
    main = Path(exe) == Path(cfg["port"])
    data = load(cfg)
    if not data["entries"]:
        raise DecompError("tas.corpus", "the corpus is empty: decomp tas corpus seed")
    health = tas_health.read(cfg, profile)
    began, cpu0 = time.monotonic(), _cpu()

    def one(movie: str) -> dict[str, Any]:
        entry = data["entries"][movie]
        spots = spots_for(data, movie)
        polls = dump_polls(spots)
        try:
            results = _replays(profile, movie, polls, GATE_LABEL, health, port)
        except DecompError as error:
            return {"movie": movie, "verdict": "fail", "reasons": [f"refused: {error}"], "run": None}
        for result, poll in zip(results, polls, strict=True):
            result["dump_at"] = poll
        judged = _judge(spots, results)
        _drop_dumps(results)
        run = Path(results[0]["run"])
        reference = entry.get("oracle") or (made_reference(profile, cfg, run) if oracle else None)
        row = compare(cfg, {**entry, "oracle": reference}, _run(cfg, entry.get("baseline")), run, judged)
        return {"movie": movie, "oracle": reference, **row}

    with ThreadPoolExecutor(max_workers=max(1, jobs or slots.capacity())) as pool:
        rows = list(pool.map(one, sorted(data["entries"])))
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    failed = [r["movie"] for r in rows if r["verdict"] == "fail"]
    moved, dropped = [], []
    if main:
        with locked(cfg):
            data = load(cfg)
            for row in rows:
                entry = data["entries"].get(row["movie"])
                if entry is None or row["verdict"] == "fail" or not row.get("run"):
                    continue
                entry["baseline"] = row["run"]
                entry["spots"] = dict(row.get("spots", {}))
                entry["oracle"] = entry.get("oracle") or row.get("oracle") or oracle_reference(cfg, row["movie"])
                moved.append(row["movie"])
            dropped = minimise(cfg, data["entries"])
            store(cfg, data)
    result = {
        "at": time.time(),
        "port": str(exe),
        "main": main,
        "build": tas.build_id(Path(exe)),
        "pins": pins_stamp(cfg),
        "verdict": "fail" if failed else "pass",
        "counts": counts,
        "failed": failed,
        "moved": moved,
        "dropped": dropped,
        "wall_seconds": round(time.monotonic() - began, 1),
        "cpu_seconds": round(_cpu() - cpu0, 1),
        "rows": rows,
    }
    if main:
        cfg["runs"].mkdir(parents=True, exist_ok=True)
        tmp = gate_path(cfg).with_suffix(".tmp")
        tmp.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
        tmp.replace(gate_path(cfg))
    return result, int(bool(failed))


def accept(profile: Profile, movies: list[str]) -> dict[str, Any]:
    """Take the last gate's runs as the baselines of these failed entries (all failed ones when none are named): the
    change was meant."""
    cfg = tas.settings(profile)
    gate_result = read_gate(cfg)
    if not gate_result:
        raise DecompError("tas.corpus", "no gate has run yet: decomp tas corpus gate")
    rows = {r["movie"]: r for r in gate_result.get("rows", []) if r.get("verdict") == "fail" and r.get("run")}
    wanted = movies or sorted(rows)
    unknown = [m for m in wanted if m not in rows]
    if unknown:
        raise DecompError("tas.corpus", f"not failed in the last gate: {', '.join(unknown)}")
    unhealthy = [m for m in wanted if outcome_failure(rows[m])]
    if unhealthy:
        raise DecompError("tas.corpus", f"cannot accept non-ok outcomes: {', '.join(unhealthy)}")
    with locked(cfg):
        data = load(cfg)
        for movie in wanted:
            if movie in data["entries"]:
                data["entries"][movie]["baseline"] = rows[movie]["run"]
                data["entries"][movie]["spots"] = dict(rows[movie].get("spots", {}))
                rows[movie]["verdict"] = "accepted"
        store(cfg, data)
    gate_result["failed"] = [m for m in gate_result["failed"] if m not in wanted]
    gate_result["verdict"] = "fail" if gate_result["failed"] else "pass"
    gate_path(cfg).write_text(json.dumps(gate_result, indent=1, sort_keys=True) + "\n")
    return {"accepted": wanted}


def gate_due(cfg: dict[str, Any]) -> bool:
    """A gate is due when the corpus has entries and the last gate ran on another build of the profile's port."""
    try:
        if not load(cfg)["entries"]:
            return False
    except DecompError:
        return False
    last = read_gate(cfg) or {}
    build = tas.build_id(Path(cfg["port"]))
    return bool(build) and last.get("build") != build


def render(result: dict[str, Any]) -> str:
    """The gate as text: one line per entry, the verdict, and what to run for a meant change."""
    counts = ", ".join(f"{k} {v}" for k, v in sorted(result["counts"].items()))
    lines = [
        f"corpus gate {result['verdict']}: {counts}; {result['wall_seconds']}/{result['cpu_seconds']} s (wall/cpu)"
        f" on {result['port']}" + ("" if result["main"] else " (a lane build: baselines unchanged)")
    ]
    width = max((len(r["movie"]) for r in result["rows"]), default=0)
    for row in result["rows"]:
        end = f"{row.get('outcome')} {row.get('polls_reached', 0)}"
        lines.append(f"  {row['movie'].ljust(width)}  {row['verdict']:<8}  {end:<14}  {'; '.join(row['reasons'])}")
    acceptable = [r["movie"] for r in result["rows"] if r["verdict"] == "fail" and not outcome_failure(r)]
    if acceptable and result["main"]:
        lines.append("a meant change: decomp tas corpus accept " + " ".join(acceptable))
    return "\n".join(lines)
