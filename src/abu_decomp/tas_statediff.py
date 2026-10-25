"""Game state, port against the original, per region and symbol, at every poll: `tas play` leaves each region's first
divergence.

Both sides read the same typed elements of the game's global variables (module statetypes lays the declared C types
out for the port's i386 and for the original's MIPS), as logical values, so the port's little-endian words and the
original's big-endian ones agree when the game state agrees. A *region* is the source area (`src/game/<AREA>`,
`DATA/<sub>` for the data units) of the units that use a variable most: nothing is listed by hand. Per poll each side
writes the 32-bit hash of every symbol that changed since the previous poll (shim `N64C_TAS_STATE*`, oracle host
`N64R_TAS_STATE*`, both from `tas_state.h`); this module replays the two traces side by side, so one pass gives every
symbol's first differing poll, then reads the canonical bytes of those polls from both sides (a short second replay of
each, up to the last poll asked for) to name the field. Every region is compared independently, so one run reports
every divergent region, not just the first.

A region's *first* divergence is usually the same at poll 1 in every movie: initial data the port holds in another form
than the original (a header declares one type, the data unit another), plus whatever the two had run differently
before the first controller poll. It is reported as such (`static`), and when the region goes on to differ in
symbols that matched at the first poll, that is a second finding, `phase: "runtime"`: the first symbol that
started to differ after the static baseline.

Files (all under `<tas.runs>`):
- `state-spec/<key>/{port.spec,oracle.spec,map.json}`: the spec each side reads and what its elements are (see below).
  `<key>` = the port executable's sha1 + the newest unit in `tas.build.units`; built once per port build (CPU ~15 s).
- `oracle-state/<key>/`: the original's trace for one movie (`state-oracle.txt`, `oracle-trace.txt`, dumps
  `dump-<poll>.bin`), cached by movie, Expansion Pak, card, oracle host and spec, so the original runs once per movie.
- `plays/<run>/state-diff.json`: `{schema:2, key, movie, polls_compared, first_compared, port_polls, oracle_polls,
  symbols, regions: {name: {status: same|diverges|unreached, symbols, static_symbols, runtime_symbols,
  polls_differing, first: row|null, runtime: row|null}}, divergent, notes, wall_seconds}`, where a row is `{poll,
  phase, static, symbol, field, port, oracle, differing_symbols, fields:[{symbol, field, kind, address, index, port,
  oracle}...], vi:{port, oracle}, timing}`; `state-port.txt`, `port-trace.txt` (the port's symbol-hash and
  "poll vi" traces) and `state/` (the port's dumps).
- `findings.jsonl` (the shared feed, module tas_findings): a `clear` of the movie's earlier state-diverge lines, then
  one line per row: `{kind:"state-diverge", id, time, movie, run, batch, poll, region, symbol, datum (= symbol), field
  (symbol plus member path, the bucket root), port, oracle, signature, evidence:{phase: "first"|"runtime", static,
  differing_symbols, fields, polls_differing,
  static_symbols, vi, timing, key, file}, at}`. `port`/`oracle` are the canonical element values (integers; a pointer
  is 0 for null, 1 for a static or host object, else its heap address); `phase: "first"` is the region's earliest
  divergence (`static: true` when it is already there at the first poll both sides traced), `phase: "runtime"` the
  earliest among the symbols that matched until then; `timing` is true when the two sides had run a different number
  of VI retraces by that poll (the port then reached the poll at another game time, so state that follows the
  clock differs by design).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, procguard, statetypes, tas_findings, tas_oracle
from abu_decomp.config import Profile

SCHEMA = 2
SPEC_VERSION = 2  # bump when the way a spec is built changes: a cached spec of another version is not reused
SPECS, ORACLES = "state-spec", "oracle-state"
FINDINGS, KIND = tas_findings.FILE, "state-diverge"
DIFF, PORT_HASHES, PORT_TRACE, DUMPS = "state-diff.json", "state-port.txt", "port-trace.txt", "state"
ORACLE_HASHES = "state-oracle.txt"
RAM = 0x800000
BASE = 0x80000000
OPEN_ARRAY_CAP = 0x4000  # bytes taken for an `extern T x[]` whose size only the symbol gap gives
HEAP_GUARD = 0x1000
FIELDS_SHOWN = 8
SYMBOLS_NAMED = 4  # symbols whose differing fields a finding lists
_SYMBOL = re.compile(r"^([0-9a-f]{8}) A (\S+)$")
_UNDEFINED = re.compile(r"^(.*?):\s+U\s+(\S+)$")
KIND_BYTES = {"b": 1, "h": 2, "w": 4, "d": 8, "p": 4}


_HOST_TRACES: dict[tuple[str, int], bool] = {}


def host_traces(host: Path) -> bool:
    """Whether the original's host binary implements the state trace (it reads N64R_TAS_STATE): a host without it
    replays the whole movie and leaves nothing to compare (TAS-COVER-3: every play ended "the original left no state
    trace"). Read once per binary (path and date)."""
    try:
        key = (str(host), host.stat().st_mtime_ns)
    except OSError:
        return False
    if key not in _HOST_TRACES:
        try:
            _HOST_TRACES[key] = b"N64R_TAS_STATE" in host.read_bytes()
        except OSError:
            _HOST_TRACES[key] = False
    return _HOST_TRACES[key]


def available(profile: Profile) -> str | None:
    """None when the original and the unit sources are there; else why a run has no state diff."""
    settings = tas_oracle.settings(profile)
    if settings is None:
        return "no tas.oracle section"
    host = settings.get("host") if isinstance(settings, dict) else None
    if host and not host_traces(Path(host)):
        return f"the original's host {host} has no state trace (N64R_TAS_STATE): no state diff, no oracle replay for it"
    units = _units(profile)
    if units is None or not units.is_dir():
        return "no tas.build.units folder (the port's preprocessed units)"
    return None


def _units(profile: Profile) -> Path | None:
    build = profile.section("tas").get("build") or {}
    return profile.path("tas.build.units", build["units"]) if "units" in build else None


# --- the spec: what each side reads ---
def port_symbols(exe: Path) -> dict[str, int]:
    """Name -> address of the guest-address symbols a port binary defines (the game's variables and functions)."""
    out = subprocess.run(["nm", str(exe)], capture_output=True, text=True, check=False, timeout=120).stdout
    found = {}
    for line in out.splitlines():
        m = _SYMBOL.match(line)
        if m and BASE <= int(m[1], 16) < BASE + RAM:
            found[m[2]] = int(m[1], 16)
    return found


def area_of(relative: Path) -> str | None:
    """The source area of a unit by its place in the layout: game/ACTORS/x -> ACTORS, game/DATA/AMMO/x -> DATA/AMMO."""
    parts = relative.parts
    if len(parts) < 3:
        return None
    if parts[0] == "game":
        return "/".join(parts[1:3]) if parts[1] == "DATA" and len(parts) > 3 else parts[1]
    return "/".join(parts[:2])


def owners(units: Path) -> dict[str, str]:
    """Variable name -> the area whose compiled units reference it most (code areas outrank the data units)."""
    objects = [p for p in sorted(units.rglob("*.o")) if not p.name.endswith(".stubs.o")]
    uses: dict[str, Counter[str]] = {}
    for start in range(0, len(objects), 300):
        out = subprocess.run(
            ["nm", "-A", "-u", "--no-demangle", *map(str, objects[start : start + 300])],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        ).stdout
        for line in out.splitlines():
            m = _UNDEFINED.match(line)
            area = area_of(Path(m[1]).relative_to(units)) if m and m[1].startswith(str(units)) else None
            if area:
                uses.setdefault(m[2], Counter())[area] += 1
    chosen = {}
    for name, count in uses.items():
        code = Counter({a: n for a, n in count.items() if not a.startswith("DATA")})
        pick = code or count
        chosen[name] = sorted(pick.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return chosen


def read_types(units: Path) -> statetypes.TypeDb:
    db = statetypes.TypeDb()
    seen: set[tuple[str, int]] = set()
    seen_statements: set[str] = set()
    for path in sorted(units.rglob("*.i")):
        db.read_unit(path.read_text(errors="replace"), seen, seen_statements)
    return db


def _kinds(runs: list[statetypes.Run]) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for _o, kind, n in runs:
        if out and out[-1][0] == kind:
            out[-1] = (kind, out[-1][1] + n)
        else:
            out.append((kind, n))
    return out


@dataclass
class Entry:
    name: str
    address: int
    port: list[statetypes.Run]
    original: list[statetypes.Run]
    shape: statetypes.Shape
    elements: int


def build_entries(
    db: statetypes.TypeDb, symbols: dict[str, int], owner: dict[str, str], skip: re.Pattern[str] | None
) -> tuple[dict[str, list[Entry]], Counter[str]]:
    """Region -> entries, and why the rest was left out."""
    port, mips = statetypes.Layouts(db, "i386"), statetypes.Layouts(db, "mips")
    ordered = sorted(symbols.values())
    why: Counter[str] = Counter()
    regions: dict[str, list[Entry]] = {}
    for name, address in sorted(symbols.items(), key=lambda kv: kv[1]):
        decl = db.variables.get(name)
        if decl is None:
            why["no variable declaration"] += 1
            continue
        if statetypes.is_const(decl.type):
            why["const"] += 1
            continue
        if skip and skip.search(name):
            why["skipped by name"] += 1
            continue
        region = owner.get(name)
        if region is None:
            why["no unit uses it"] += 1
            continue
        try:
            try:
                a, b = port.of(decl.type), mips.of(decl.type)
            except statetypes.Unsized:
                if not statetypes._open_array(decl.type):
                    raise
                after = [x for x in ordered if x > address]
                span = min((after[0] if after else BASE + RAM) - address, OPEN_ARRAY_CAP)
                item = port.of(decl.type.type)
                count = span // item.size if item.size else 0
                a, b = port.open_array(decl.type, count), mips.open_array(decl.type, count)
        except (statetypes.Unsized, ZeroDivisionError, KeyError, IndexError, ValueError, TypeError):
            why["type not laid out"] += 1
            continue
        if not a.runs:
            why["no host-independent elements"] += 1
            continue
        if _kinds(a.runs) != _kinds(b.runs):
            why["layouts of the two ABIs disagree"] += 1
            continue
        if address + a.size > BASE + RAM:
            why["outside RDRAM"] += 1
            continue
        regions.setdefault(region, []).append(
            Entry(
                name,
                address,
                [(address + o, k, n) for o, k, n in a.runs],
                [(address + o, k, n) for o, k, n in b.runs],
                a.shape,
                statetypes.elements(a.shape),
            )
        )
    return regions, why


def spec_text(regions: dict[str, list[Entry]], side: str, dynamic: tuple[int, int]) -> str:
    lines = [
        "# state spec for tas_state.h (module tas_statediff)",
        f"ram {RAM:x}",
        f"dynamic {dynamic[0]:x} {dynamic[1]:x}",
    ]
    for region in sorted(regions):
        lines.append("region " + region.replace(" ", "_"))
        for entry in regions[region]:
            lines.append("symbol " + entry.name)
            merged: list[statetypes.Run] = []
            for o, k, n in entry.port if side == "port" else entry.original:
                if merged and merged[-1][1] == k and merged[-1][0] + KIND_BYTES[k] * merged[-1][2] == o:
                    merged[-1] = (merged[-1][0], k, merged[-1][2] + n)
                else:
                    merged.append((o, k, n))
            lines += [f"run {o:x} {k} {n}" for o, k, n in merged]
    return "\n".join(lines) + "\n"


def spec_key(exe: Path, units: Path) -> str:
    digest = hashlib.sha1()
    with exe.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    newest = max((p.stat().st_mtime_ns for p in units.rglob("*.i")), default=0)
    digest.update(f"{newest}:{RAM}:{SPEC_VERSION}".encode())
    return digest.hexdigest()[:20]


def build_spec(profile: Profile, cfg: dict[str, Any], exe: Path) -> Path:
    """The folder holding port.spec, oracle.spec and map.json for this executable (built once per key)."""
    units = _units(profile)
    assert units is not None
    folder = cfg["runs"] / SPECS / spec_key(exe, units)
    if (folder / "map.json").is_file():
        return folder
    began, cpu = time.monotonic(), time.process_time()
    symbols = port_symbols(exe)
    regions, why = build_entries(read_types(units), symbols, owners(units), None)
    static_end = max(
        (e.address + sum(KIND_BYTES[k] * n for _o, k, n in e.port) for es in regions.values() for e in es),
        default=BASE,
    )
    top = max(symbols.values(), default=BASE) + HEAP_GUARD
    dynamic = ((max(static_end, top) + 0xFFF) & ~0xFFF, BASE + RAM)
    tmp = folder.with_name(folder.name + f".tmp{os.getpid()}")
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "port.spec").write_text(spec_text(regions, "port", dynamic))
    (tmp / "oracle.spec").write_text(spec_text(regions, "oracle", dynamic))
    index = 0
    table_regions = {}
    for r in sorted(regions):
        symbols_out = []
        for e in regions[r]:
            symbols_out.append(
                {
                    "index": index,
                    "name": e.name,
                    "address": e.address,
                    "elements": e.elements,
                    "kinds": _kinds(e.port),
                    "shape": e.shape,
                }
            )
            index += 1
        table_regions[r] = {"elements": sum(e.elements for e in regions[r]), "symbols": symbols_out}
    table = {
        "schema": SCHEMA,
        "dynamic": list(dynamic),
        "left_out": dict(why),
        "symbols": index,
        "wall_seconds": round(time.monotonic() - began, 1),
        "cpu_seconds": round(time.process_time() - cpu, 1),
        "regions": table_regions,
    }
    (tmp / "map.json").write_text(json.dumps(table, separators=(",", ":")))
    folder.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(tmp, folder)
    except OSError:  # another run built it first
        shutil.rmtree(tmp, ignore_errors=True)
    return folder


def load_map(folder: Path) -> dict[str, Any]:
    return json.loads((folder / "map.json").read_text())


@dataclass
class Sym:
    """One symbol of the spec as the dumps and traces see it."""

    index: int
    name: str
    region: str
    address: int
    offset: int  # byte offset in a whole-state dump
    size: int
    kinds: list[Any]
    shape: Any


def symbol_table(table: dict[str, Any]) -> list[Sym]:
    """Every symbol in spec order (regions by name, symbols by address), with its place in a whole-state dump."""
    out: list[Sym] = []
    at = 0
    for region in sorted(table["regions"]):
        for s in table["regions"][region]["symbols"]:
            size = sum(KIND_BYTES[k] * n for k, n in s["kinds"])
            out.append(Sym(s["index"], s["name"], region, s["address"], at, size, s["kinds"], s["shape"]))
            at += size
    return out


# --- reading the traces ---
def read_trace(path: Path, symbols: int) -> dict[int, list[tuple[int, int]]]:
    """poll -> [(symbol index, folded hash)] of a "poll idx:hash ..." trace (a half-written last line is dropped)."""
    rows: dict[int, list[tuple[int, int]]] = {}
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return rows
    for line in text[: text.rfind("\n") + 1].splitlines():
        parts = line.split()
        if not parts or not parts[0].isdigit():
            continue
        try:
            changes = [(int(i), int(h, 16)) for i, h in (p.split(":") for p in parts[1:])]
        except ValueError:
            continue
        if all(0 <= i < symbols for i, _h in changes):
            rows[int(parts[0])] = changes
    return rows


def diverged(
    port: dict[int, list[tuple[int, int]]], original: dict[int, list[tuple[int, int]]], regions: list[int]
) -> dict[str, Any]:
    """Walk both traces poll by poll. `regions[i]` is the region number of symbol i. Returns the polls both traced
    (`compared`, `first_compared`, `last_poll`), each symbol's first differing poll (`first[i]`, None when it never
    differed) and, per region number, how many compared polls had a symbol differing (`polls_differing`)."""
    n = len(regions)
    now_p: list[int | None] = [None] * n
    now_o: list[int | None] = [None] * n
    first: list[int | None] = [None] * n
    bad: set[int] = set()
    live: Counter[int] = Counter()
    polls_differing: Counter[int] = Counter()
    touched: set[int] = set()
    common = compared = 0
    first_compared = None
    last = 0
    for poll in sorted(set(port) | set(original)):
        for i, h in port.get(poll, ()):
            now_p[i] = h
            touched.add(i)
        for i, h in original.get(poll, ()):
            now_o[i] = h
            touched.add(i)
        if poll not in port or poll not in original:
            continue
        compared += 1
        common = poll
        last = poll
        if first_compared is None:
            first_compared = poll
        for i in touched:
            same = now_p[i] == now_o[i] and now_p[i] is not None
            if not same and i not in bad:
                bad.add(i)
                live[regions[i]] += 1
                if first[i] is None:
                    first[i] = poll
            elif same and i in bad:
                bad.discard(i)
                live[regions[i]] -= 1
        touched.clear()
        for r, count in live.items():
            if count:
                polls_differing[r] += 1
    return {
        "compared": compared,
        "first_compared": first_compared,
        "last_poll": last or common,
        "first": first,
        "polls_differing": polls_differing,
    }


def compare_symbol(sym: Sym, a: bytes, b: bytes, limit: int = FIELDS_SHOWN) -> dict[str, Any]:
    """The elements of one symbol's canonical bytes (`a` the port's, `b` the original's) that differ:
    {differing, fields:[{symbol, field, kind, address, index, port, oracle}]} in element order."""
    fields: list[dict[str, Any]] = []
    differing = 0
    shape = tuple_shape(sym.shape)
    byte = 0
    index = 0
    for kind, count in sym.kinds:
        width = KIND_BYTES[kind]
        for _ in range(count):
            if a[byte : byte + width] != b[byte : byte + width]:
                differing += 1
                if len(fields) < limit:
                    path, _kind = statetypes.field_at(shape, index)
                    fields.append(
                        {
                            "symbol": sym.name,
                            "field": path,
                            "kind": kind,
                            "address": sym.address,
                            "index": index,
                            "port": int.from_bytes(a[byte : byte + width], "big"),
                            "oracle": int.from_bytes(b[byte : byte + width], "big"),
                        }
                    )
            byte += width
            index += 1
    return {"differing": differing, "fields": fields}


def tuple_shape(value: Any) -> statetypes.Shape:
    """A shape read back from JSON (lists) as the tuples statetypes uses."""
    if isinstance(value, list):
        if value and value[0] == "s":
            return ("s", tuple((n, tuple_shape(s)) for n, s in value[1]))
        if value and value[0] == "a":
            return ("a", value[1], tuple_shape(value[2]))
        return tuple(value)
    return value


# --- running the two sides ---
@dataclass
class Session:
    """One play's state diff: the spec, the port's environment, the original (running or cached)."""

    folder: Path
    run: Path
    cfg: dict[str, Any]
    key: str
    table: dict[str, Any]
    oracle_dir: Path
    cached: bool
    process: subprocess.Popen | None = None
    oracle_cfg: dict[str, Any] = field(default_factory=dict)
    pak: str = "on"
    card: Path | None = None
    began: float = field(default_factory=time.monotonic)
    notes: list[str] = field(default_factory=list)
    _end: dict[str, Any] = field(default_factory=dict)
    _waiter: threading.Thread | None = None

    def port_env(self) -> dict[str, str]:
        return {
            "N64C_TAS_STATE": str(self.folder / "port.spec"),
            "N64C_TAS_STATE_HASH": str(self.run / PORT_HASHES),
            "N64C_TAS_TRACE": str(self.run / PORT_TRACE),
        }


def _oracle_extra(spec: Path, out: Path, extra: dict[str, str]) -> dict[str, str]:
    return {
        "N64R_TAS_SHOT_EVERY": "0",
        "N64R_AUDIO_OUT": os.devnull,
        "N64R_TAS_STATE": str(spec),
        "N64R_TAS_STATE_HASH": str(out / ORACLE_HASHES),
        **extra,
    }


def begin(
    profile: Profile, cfg: dict[str, Any], run: Path, pak: str, card: Path | None, polls: list[Any]
) -> Session | None:
    """Start a run's state diff: the spec for the port in use, and the original's trace of this movie (started now,
    beside the port, unless one is cached). None when this seat cannot (no original or no units)."""
    if available(profile) is not None:
        return None
    from abu_decomp import tas

    oracle = tas_oracle.required(profile)
    exe = Path(cfg["port"])
    folder = build_spec(profile, cfg, exe)
    table = load_map(folder)
    movie = (run / "movie.raw").read_bytes()
    host = oracle["host"].stat()
    cardhash = hashlib.sha1(card.read_bytes()).hexdigest() if card and card.is_file() else "none"
    digest = hashlib.sha1()
    for part in (
        movie,
        pak.encode(),
        cardhash.encode(),
        f"{host.st_size}:{host.st_mtime_ns}".encode(),
        (folder / "oracle.spec").read_bytes(),
        str(cfg["exit_after"]).encode(),
    ):
        digest.update(hashlib.sha1(part).digest())
    key = digest.hexdigest()[:20]
    cache = cfg["runs"] / ORACLES / key
    session = Session(
        folder, run, cfg, key, table, cache, (cache / ORACLE_HASHES).is_file(), oracle_cfg=oracle, pak=pak, card=card
    )
    if session.cached:
        return session
    work = cache.with_name(cache.name + f".tmp{os.getpid()}-{run.name}")
    work.mkdir(parents=True, exist_ok=True)
    (work / "movie.raw").write_bytes(movie)
    own = None
    if card is not None and card.is_file():
        own = work / "oracle-cpak.mpk"
        start = run / "cpak-start.mpk"
        own.write_bytes(start.read_bytes() if start.is_file() else card.read_bytes())
    session.process = tas_oracle.start(
        oracle,
        work,
        cfg["exit_after"],
        pak,
        extra=_oracle_extra(folder / "oracle.spec", work, {"N64R_TAS_TRACE": str(work / tas_oracle.ORACLE_TRACE)}),
        card=own,
    )
    session.oracle_dir = work

    def reap() -> None:
        code, cpu = tas._wait(session.process, oracle["timeout_seconds"])
        session._end.update(code=code, cpu=cpu)

    session._waiter = threading.Thread(target=reap, daemon=True)
    session._waiter.start()
    return session


def _oracle_done(session: Session) -> bool:
    """Wait for the original's trace and move it into the cache; False when it left none."""
    if session.cached:
        return True
    if session._waiter:
        session._waiter.join()
    work = session.oracle_dir
    final = session.cfg["runs"] / ORACLES / session.key
    if not (work / ORACLE_HASHES).is_file():
        session.notes.append(f"the original left no state trace (exit {session._end.get('code')})")
        shutil.rmtree(work, ignore_errors=True)
        return False
    for junk in ("oracle.bmp", "oracle-cpak.mpk", "oracle.txt"):
        with contextlib.suppress(OSError):
            (work / junk).unlink()
    shutil.rmtree(work / tas_oracle.SHOTS, ignore_errors=True)
    try:
        os.replace(work, final)
    except OSError:
        shutil.rmtree(work, ignore_errors=True)
    session.oracle_dir, session.cached = final, True
    return True


def _truncate_movie(source: Path, dest: Path, polls: int) -> None:
    lines = source.read_text().splitlines()
    dest.write_text("\n".join(lines[:polls]) + "\n")


def _dump_pass(session: Session, polls: list[int], profile: Profile) -> dict[int, Any]:
    """Replay both sides to the last poll asked for, dumping every symbol's canonical bytes at `polls`.
    Returns poll -> (port bytes, original bytes)."""
    from abu_decomp import tas

    cfg, run, last = session.cfg, session.run, max(polls)
    want = ",".join(str(p) for p in polls)
    port_dir = run / DUMPS
    port_dir.mkdir(exist_ok=True)
    _truncate_movie(run / "movie.raw", port_dir / "movie.raw", last)
    user = port_dir / "user"
    user.mkdir(exist_ok=True)
    card = None
    if (run / "cpak-start.mpk").is_file():
        card = port_dir / "cpak.mpk"
        card.write_bytes((run / "cpak-start.mpk").read_bytes())
    env = {
        **os.environ,
        **cfg["env"],
        "N64C_SPEED": "0",
        "N64C_TAS_PLAY": str(port_dir / "movie.raw"),
        "N64C_TAS_EXIT_AFTER": "0",
        "N64C_TAS_STATE": str(session.folder / "port.spec"),
        "N64C_TAS_STATE_AT": want,
        "N64C_TAS_STATE_DIR": str(port_dir),
    }
    argv = tas._argv(cfg, user, session.pak, card)
    # the original: only the polls its cache lacks
    cache = session.oracle_dir
    missing = [p for p in polls if not (cache / f"dump-{p}.bin").is_file()]
    original, work = None, None
    if missing:
        work = cache.with_name(cache.name + f".dump{os.getpid()}")
        work.mkdir(parents=True, exist_ok=True)
        _truncate_movie(run / "movie.raw", work / "movie.raw", max(missing))
        own = None
        if card is not None:
            own = work / "oracle-cpak.mpk"
            own.write_bytes(card.read_bytes())
        original = tas_oracle.start(
            session.oracle_cfg,
            work,
            0,
            session.pak,
            extra=_oracle_extra(
                session.folder / "oracle.spec",
                work,
                {
                    "N64R_TAS_STATE_HASH": os.devnull,
                    "N64R_TAS_STATE_AT": ",".join(map(str, missing)),
                    "N64R_TAS_STATE_DIR": str(work),
                    "N64R_TAS_TRACE": os.devnull,
                },
            ),
            card=own,
        )
    with (port_dir / "output.txt").open("w") as output:
        process = subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            **procguard.owned(),
            cwd=port_dir,
        )
        tas._wait(process, cfg["timeout_seconds"])
    if original is not None and work is not None:
        tas._wait(original, session.oracle_cfg["timeout_seconds"])
        for p in missing:
            produced = work / f"state-{p}.bin"
            if produced.is_file():
                os.replace(produced, cache / f"dump-{p}.bin")
        shutil.rmtree(work, ignore_errors=True)
    out = {}
    for p in polls:
        mine, theirs = port_dir / f"state-{p}.bin", cache / f"dump-{p}.bin"
        if mine.is_file() and theirs.is_file():
            out[p] = (mine.read_bytes(), theirs.read_bytes())
    return out


def vi_at(path: Path, poll: int) -> int | None:
    return tas_oracle.trace(path).get(poll)


def _rows(syms: list[Sym], found: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per region: its static symbols (differing at the first compared poll), the symbols that started to differ later
    (poll -> symbol indexes) and the rows to report: `first` (the earliest divergence) and `runtime` (the earliest
    one that is not static, when `first` is)."""
    base = found["first_compared"]
    out: dict[str, dict[str, Any]] = {}
    for sym in syms:
        poll = found["first"][sym.index]
        if poll is None:
            continue
        r = out.setdefault(sym.region, {"static": [], "later": {}})
        if poll == base:
            r["static"].append(sym.index)
        else:
            r["later"].setdefault(poll, []).append(sym.index)
    for r in out.values():
        later = sorted(r["later"].items())
        r["first"] = (base, r["static"], True) if r["static"] else (later[0][0], later[0][1], False)
        r["runtime"] = (later[0][0], later[0][1]) if r["static"] and later else None
    return out


def finish(session: Session, profile: Profile, movie: str, record: dict[str, Any]) -> dict[str, Any]:
    """Compare the two traces, name each divergent region's first differing field, write state-diff.json and the
    findings. Returns the summary stored in the run record."""
    run, table = session.run, session.table
    syms = symbol_table(table)
    names = sorted(table["regions"])
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "key": session.key,
        "movie": movie,
        "symbols": len(syms),
        "regions": {},
        "notes": session.notes,
    }
    if not _oracle_done(session):
        result.update(error="the original left no state trace", divergent=None)
        (run / DIFF).write_text(json.dumps(result, indent=1))
        return {"error": result["error"]}
    port = read_trace(run / PORT_HASHES, len(syms))
    original = read_trace(session.oracle_dir / ORACLE_HASHES, len(syms))
    if not port:
        session.notes.append(
            "the port wrote no state trace (a build without N64C_TAS_STATE, or it stopped before poll 1)"
        )
    number = {name: i for i, name in enumerate(names)}
    found = diverged(port, original, [number[s.region] for s in syms])
    result.update(
        polls_compared=found["compared"],
        first_compared=found["first_compared"],
        port_polls=max(port) if port else 0,
        oracle_polls=max(original) if original else 0,
    )
    per_region = _rows(syms, found) if found["compared"] else {}
    wanted = sorted(
        {p for r in per_region.values() for p in ([r["first"][0]] + ([r["runtime"][0]] if r["runtime"] else []))}
    )
    dumps: dict[int, Any] = {}
    if wanted:
        try:
            dumps = _dump_pass(session, wanted, profile)
        except (OSError, DecompError) as error:
            session.notes.append(f"dump pass failed: {error}")

    def row(region: str, poll: int, members: list[int], phase: str, static: bool) -> dict[str, Any]:
        info: dict[str, Any] = {
            "poll": poll,
            "phase": phase,
            "static": static,
            "differing_symbols": len(members),
            "symbol": syms[members[0]].name,
            "symbols": [syms[i].name for i in members[:SYMBOLS_NAMED]],
        }
        fields: list[dict[str, Any]] = []
        if poll in dumps:
            mine, theirs = dumps[poll]
            for i in members[:SYMBOLS_NAMED]:
                s = syms[i]
                part = compare_symbol(s, mine[s.offset : s.offset + s.size], theirs[s.offset : s.offset + s.size])
                fields += part["fields"]
        info["fields"] = fields[:FIELDS_SHOWN]
        if fields:
            info.update(field=fields[0]["field"], port=fields[0]["port"], oracle=fields[0]["oracle"])
        vi = {
            "port": vi_at(run / PORT_TRACE, poll),
            "oracle": vi_at(session.oracle_dir / tas_oracle.ORACLE_TRACE, poll),
        }
        info["vi"] = vi
        info["timing"] = vi["port"] is not None and vi["port"] != vi["oracle"]
        return info

    rows: list[tuple[str, dict[str, Any]]] = []
    for name in names:
        count = sum(1 for s in table["regions"][name]["symbols"])
        entry: dict[str, Any] = {
            "status": "unreached" if not found["compared"] else "same",
            "symbols": count,
            "static_symbols": 0,
            "runtime_symbols": 0,
            "polls_differing": found["polls_differing"][number[name]] if found["compared"] else 0,
            "first": None,
            "runtime": None,
        }
        r = per_region.get(name)
        if r:
            entry["status"] = "diverges"
            entry["static_symbols"] = len(r["static"])
            entry["runtime_symbols"] = sum(len(v) for v in r["later"].values())
            poll, members, static = r["first"]
            entry["first"] = row(name, poll, members, "first", static)
            rows.append((name, entry["first"]))
            if r["runtime"]:
                entry["runtime"] = row(name, r["runtime"][0], r["runtime"][1], "runtime", False)
                rows.append((name, entry["runtime"]))
        result["regions"][name] = entry
    result["divergent"] = sum(1 for e in result["regions"].values() if e["status"] == "diverges")
    result["wall_seconds"] = round(time.monotonic() - session.began, 1)
    (run / DIFF).write_text(json.dumps(result, indent=1))
    if found["compared"]:  # a run that compared nothing neither clears nor adds findings
        write_findings(session, movie, record, rows, result["regions"])
    return {
        "divergent": result["divergent"],
        "regions": len(names),
        "findings": len(rows),
        "polls_compared": found["compared"],
        "file": str(run / DIFF),
    }


def write_findings(
    session: Session,
    movie: str,
    record: dict[str, Any],
    rows: list[tuple[str, dict[str, Any]]],
    regions: dict[str, Any],
) -> None:
    """The run's rows into the shared feed (module tas_findings): first a `clear` of this movie's earlier
    state-diverge findings, so what stands is what this run found, then one finding per row."""
    lines: list[dict[str, Any]] = [
        {"kind": "clear", "movie": movie, "run": session.run.name, "clears": [KIND], "batch": record.get("batch")}
    ]
    for region, info in rows:
        where = f"{info['symbol']}{_field_suffix(info.get('field'))}"
        lines.append(
            {
                "kind": KIND,
                "movie": movie,
                "run": session.run.name,
                "batch": record.get("batch"),
                "poll": info["poll"],
                "region": region,
                "symbol": info["symbol"],
                "datum": info["symbol"],
                "field": where,
                "port": info.get("port"),
                "oracle": info.get("oracle"),
                "signature": f"state {info['phase']} {region} {where}",
                "evidence": {
                    "phase": info["phase"],
                    "static": info["static"],
                    "differing_symbols": info["differing_symbols"],
                    "symbols": info["symbols"],
                    "fields": info["fields"],
                    "polls_differing": regions[region]["polls_differing"],
                    "static_symbols": regions[region]["static_symbols"],
                    "vi": info["vi"],
                    "timing": info["timing"],
                    "key": session.key,
                    "file": str(session.run / DIFF),
                },
            }
        )
    tas_findings.append(session.cfg["runs"], [{k: v for k, v in row.items() if v is not None} for row in lines])


def _field_suffix(field: str | None) -> str:
    """`[1].value` and `.value` as they follow a symbol name; nothing for a scalar."""
    if not field:
        return ""
    return field if field.startswith("[") else "." + field
