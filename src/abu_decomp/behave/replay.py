"""`decomp behave --replay`: real calls from the original game, run again through the port's build of the function.

`behave` proves a candidate against the original in isolation on generated inputs. This is the same question asked of
the port, on inputs the game really made: the oracle host (the recompiled original, `tas.oracle`) replays the corpus
movies with its function-replay recorder on (cake-taste hosts/n64recomp/replay.cpp) and writes, for a few calls of every
function the movie reaches, the arguments, each guest memory location the call read before writing it (address, width,
value), what it stored and what it returned. Each sample is laid out in the native shape of the port (a little-endian
value of the width the original accessed; a function pointer becomes the port's address of that function; the arguments
follow the prototype's o32 slots), written into a plan, and the port executable runs it (`N64C_REPLAY`, shim
boot/replay.c: the loaded image, one forked child per case, no game thread). The return value and every location the
original stored are compared with what the port's function left.

Each difference is one line of `<tas.runs>/findings.jsonl` (`kind` fn-differs); a re-run of a function replaces its
earlier lines. Not a difference, and counted apart: a call the recorder wrote off (another thread ran, memory outside
RDRAM, one location read at two widths ...), a function the port build does not hold or stands in for with assembly, a
fault inside the shim (the shim is not started in a replay), a timeout.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, procguard, tas, tas_coverage, tas_oracle
from abu_decomp.behave import prototype
from abu_decomp.config import Profile

CALLS = 3  # samples kept per function
GAP = 25  # VI retraces between two samples of one function (recorder)
EVENTS = 200000  # accesses one sampled call may make (recorder)
CPU_BUDGET = 300.0  # CPU seconds one batch may spend (the repository's per-command cap)
CASE_SECONDS = 5  # wall clock one case may take in the port
FRAME_WINDOW = 0x4000  # stores below the entry stack pointer by less than this are the dead frames of the call
ULPS = 4  # float results this close are rounding (x87 against MIPS single precision), as lockstep.py judges them
MAX_PER_CASE = 8  # mismatches one sample reports
PHYSICAL_FLOOR = 0x100000  # the physical form of a function address is taken for one only from here up
KIND = "fn-differs"
ORACLE_KIND = "oracle-aborts"
FINDINGS = "findings.jsonl"
GUEST_ENTRY = re.compile(r"^\s*(\w+)\s*=\s*(0x[0-9A-Fa-f]+)\s*;")
INT_MASK = {"int8": 0xFF, "uint8": 0xFF, "int16": 0xFFFF, "uint16": 0xFFFF}
POINTERS = ("pointer", "funcptr", "word")


class ReplayError(DecompError):
    pass


class OracleAborted(ReplayError):
    """The original itself died replaying a movie (a bug-demo movie, a code pointer it cannot find): an oracle finding
    about that movie, not a failure of the batch."""

    def __init__(self, movie: str, code: int | None, tail: str):
        super().__init__("behave.replay.oracle-abort", f"the oracle aborted replaying {movie} (exit {code}): {tail}")
        self.movie, self.code, self.tail = movie, code, tail


@dataclass
class Options:
    movies: list[str] = field(default_factory=list)  # empty: every movie of the corpus
    functions: list[str] = field(default_factory=list)  # empty: every function the movies reach
    calls: int = CALLS
    oracle: Path | None = None  # an oracle host with the recorder (default tas.oracle.host)
    port: Path | None = None  # a port executable with replay.c (default tas.port)
    source: Path | None = None  # the game sources the port was built from (default tas.serve.pin, tools.project)
    version: str | None = None
    refresh: bool = False  # record again even when the movie's recording is cached
    cpu_budget: float = CPU_BUDGET
    findings: bool = True  # write <tas.runs>/findings.jsonl


@dataclass
class Sample:
    """One recorded call."""

    movie: str
    pak: str
    function: str
    index: int  # the function's call number in the movie
    frame: int
    sp: int
    args: list[int]
    f12: int
    f14: int
    stack: list[int]
    reads: list[list[int]]  # [address, width, value]
    stores: list[list[int]]
    ret: list[int]
    f0: int

    @property
    def key(self) -> str:
        return hashlib.sha1(json.dumps([self.args, self.reads]).encode()).hexdigest()


# --- recording --------------------------------------------------------------------------------------------------


def read_samples(path: Path, movie: str, pak: str) -> tuple[list[Sample], Counter[str], set[str]]:
    """The recorder's samples of one movie, the reasons it wrote calls off, and every function it saw a call of."""
    samples: list[Sample] = []
    skipped: Counter[str] = Counter()
    seen: set[str] = set()
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        seen.add(row["fn"])
        if "skip" in row:
            skipped[row["skip"]] += 1
            continue
        samples.append(
            Sample(
                movie,
                pak,
                row["fn"],
                row["i"],
                row["frame"],
                row["sp"],
                row["a"],
                row["f12"],
                row["f14"],
                row["stack"],
                row["in"],
                row["out"],
                row["ret"],
                row["f0"],
            )
        )
    return samples, skipped, seen


def oracle_host(profile: Profile, options: Options) -> dict[str, Any]:
    oracle = tas_oracle.required(profile, fresh=options.oracle is None)
    if options.oracle is not None:
        oracle = {**oracle, "host": options.oracle.resolve()}
        if not os.access(oracle["host"], os.X_OK):
            raise ReplayError("behave.replay.oracle", f"no oracle host at {oracle['host']}")
    return oracle


def record_movie(
    profile: Profile, cfg: dict[str, Any], oracle: dict[str, Any], name: str, options: Options
) -> dict[str, Any]:
    """Replay one movie in the oracle with the recorder on. The recording is cached beside the runs, keyed by the
    movie, the oracle host's build and the recorder settings: the original does not change when our code does."""
    movie = tas._movie(cfg, name)
    pak = tas.movie_pak(cfg, name)
    host = Path(oracle["host"])
    stat = host.stat()
    key = hashlib.sha1(
        json.dumps(
            [movie.read_bytes().hex(), str(host), stat.st_mtime_ns, stat.st_size, options.calls, GAP, EVENTS, pak]
        ).encode()
    ).hexdigest()[:16]
    home = cfg["runs"] / "replay" / "movies" / f"{name}-{key}"
    done = home / "replay.jsonl"
    if done.is_file() and not options.refresh:
        meta = json.loads((home / "meta.json").read_text()) if (home / "meta.json").is_file() else {}
        return {"movie": name, "pak": pak, "path": done, "cached": True, **meta}
    home.mkdir(parents=True, exist_ok=True)
    polls = tas.movie_polls(cfg, name)
    (home / "movie.raw").write_text(tas.format_raw(polls))
    card = None
    mode = tas.movie_cpak(cfg, name)
    start = tas.start_card(cfg, mode)
    if start is not None:
        card = home / "oracle-cpak.mpk"
        card.write_bytes(start)
    for stale in (done, home / "meta.json"):
        stale.unlink(missing_ok=True)
    extra = {
        "N64R_REPLAY_DIR": str(home / "rec"),
        "N64R_REPLAY_CALLS": str(options.calls),
        "N64R_REPLAY_GAP": str(GAP),
        "N64R_REPLAY_EVENTS": str(EVENTS),
    }
    began = time.monotonic()
    process = tas_oracle.start(oracle, home, cfg["exit_after"], pak, extra=extra, card=card)
    code, cpu = tas._wait(process, oracle["timeout_seconds"])
    wall = round(time.monotonic() - began, 1)
    made = home / "rec" / "replay.jsonl"
    if code != 0 or not made.is_file():
        tail = (home / tas_oracle.LOG).read_text(errors="replace").strip().splitlines()[-3:]
        if code != 0:
            raise OracleAborted(name, code, " | ".join(tail))
        raise ReplayError(
            "behave.replay.record",
            f"the oracle did not record {name} (exit {code}; the host may lack the recorder, cake-taste "
            f"hosts/n64recomp/replay.cpp): {' | '.join(tail)}",
        )
    made.replace(done)
    meta = {"wall_seconds": wall, "cpu_seconds": cpu, "oracle_exit": code}
    (home / "meta.json").write_text(json.dumps(meta))
    return {"movie": name, "pak": pak, "path": done, "cached": False, **meta}


def pick(samples: list[Sample], calls: int) -> dict[str, list[Sample]]:
    """Up to `calls` samples per function, different calls (arguments and inputs), movies taken in turn."""
    by_fn: dict[str, dict[str, list[Sample]]] = {}
    for s in samples:
        by_fn.setdefault(s.function, {}).setdefault(s.movie, []).append(s)
    chosen: dict[str, list[Sample]] = {}
    for fn, movies in by_fn.items():
        taken: list[Sample] = []
        keys: set[str] = set()
        queues = [list(v) for _, v in sorted(movies.items())]
        while queues and len(taken) < calls:
            for queue in list(queues):
                while queue and queue[0].key in keys:
                    queue.pop(0)
                if not queue:
                    queues.remove(queue)
                    continue
                s = queue.pop(0)
                keys.add(s.key)
                taken.append(s)
                if len(taken) >= calls:
                    break
        chosen[fn] = taken
    return chosen


# --- the game's facts -------------------------------------------------------------------------------------------


def guest_addresses(source: Path, version: str) -> dict[str, int]:
    """Function and data name -> its guest address in `version`, from the project's symbol facts."""
    path = source / "versions" / version / "symbol_addrs.txt"
    found: dict[str, int] = {}
    try:
        for line in path.read_text(errors="replace").splitlines():
            match = GUEST_ENTRY.match(line)
            if match:
                found[match[1]] = int(match[2], 16)
    except OSError as error:
        raise ReplayError("behave.replay.symbols", f"cannot read {path}: {error}") from error
    return found


def function_pointers(guest: dict[str, int], native: dict[str, int]) -> dict[int, int]:
    """Guest code pointer -> the port's address of the function: the KSEG0 address, and the physical form the game
    keeps in its tables (`D_00206018` is the function at 0x80206018)."""
    found: dict[int, int] = {}
    for name, address in native.items():
        at = guest.get(name)
        if at is not None and at >= 0x80000000:
            found[at] = address
            if at & 0x1FFFFFFF >= PHYSICAL_FLOOR:  # below it an integer is far likelier than a pointer
                found.setdefault(at & 0x1FFFFFFF, address)
    return found


class Prototypes:
    """The project's declarations, read once: typedef kinds and every source text, then a lookup per function."""

    def __init__(self, root: Path, globs: tuple[str, ...]) -> None:
        paths = sorted({p for pattern in globs for p in root.glob(pattern) if p.is_file()})
        self.root = root
        self.texts = {p: p.read_text(errors="replace") for p in paths}
        self.aliases = prototype.scan_aliases(list(self.texts.values()))
        self.cache: dict[str, prototype.Prototype] = {}

    def get(self, name: str) -> prototype.Prototype:
        """The declaration with the most parameters: an old-style `T f();` elsewhere in the sources says nothing."""
        if name not in self.cache:
            best = None
            for path, text in self.texts.items():
                if name not in text:
                    continue
                found = prototype.parse_declaration(text, name, self.aliases, str(path.relative_to(self.root)))
                if found and (best is None or len(found.params) > len(best.params)):
                    best = found
            self.cache[name] = best or prototype.inferred(name)
        return self.cache[name]


def ret_kind(proto: prototype.Prototype) -> str:
    """The replay harness' result kind: v void or not meaningful, i 32-bit, q 64-bit, f float, d double."""
    if not proto.known or proto.ret in ("void", "struct"):
        return "v"
    return {"int64": "q", "uint64": "q", "float": "f", "double": "d"}.get(proto.ret, "i")


def comparable_return(proto: prototype.Prototype) -> bool:
    """Whether the result register means something the comparison can judge: a declared, non-variadic function whose
    result type is known (an unresolved typedef may be narrow: its upper bits are not defined)."""
    return proto.known and not proto.variadic and proto.ret not in ("void", "struct", "word", "")


def native_words(proto: prototype.Prototype, sample: Sample, native_of: dict[int, int]) -> tuple[list[int], bool]:
    """The cdecl argument words for the sample, and whether the prototype is a guess (the comparison then leaves the
    return value out). o32 slots come from `argument_slots`; a 64-bit value is a big-endian register pair."""
    blind = proto.blind  # an unknown, variadic or by-value-struct prototype: the argument words are a guess
    registers = {f"a{i}": sample.args[i] for i in range(4)}
    words: list[int] = []
    for slot in prototype.argument_slots(proto):
        values: list[int] = []
        for where in slot.where:
            if where.startswith("stack:"):
                at = (int(where[6:]) - 16) // 4  # the recorded words start at sp+16, the 5th argument word
                values.append(sample.stack[at] if 0 <= at < len(sample.stack) else 0)
            elif where == "f12":
                values.append(sample.f12 & 0xFFFFFFFF if slot.param.kind == "float" else sample.f12)
            elif where == "f14":
                values.append(sample.f14 & 0xFFFFFFFF if slot.param.kind == "float" else sample.f14)
            else:
                values.append(registers[where])
        kind = slot.param.kind
        if slot.param.words == 2:
            # a double in an FPR is its 64 bits; a register pair or two stack words, high word first
            value = values[0] if len(values) == 1 else (values[0] << 32) | values[1]
            words += [value & 0xFFFFFFFF, value >> 32]
        else:
            word = values[0] & 0xFFFFFFFF
            if kind in POINTERS:
                word = native_of.get(word, word)
            words.append(word)
    return words[:16], blind


# --- the plan and its results -----------------------------------------------------------------------------------


@dataclass
class Case:
    id: str
    sample: Sample
    proto: prototype.Prototype
    kind: str
    entry: int
    words: list[int]
    blind: bool
    reads: list[tuple[int, int, int]]  # physical, width, native value
    wants: list[tuple[int, int, int]]  # physical, width, the original's value in the port's terms


def lay_out(value: int, width: int, native_of: dict[int, int]) -> int:
    """The original's value of a location, as the port holds it: a function's guest address is its native address."""
    if width == 4:
        return native_of.get(value, value)
    return value


def make_case(index: int, sample: Sample, proto: prototype.Prototype, entry: int, native_of: dict[int, int]) -> Case:
    words, blind = native_words(proto, sample, native_of)
    reads = [(a & 0x1FFFFFFF, w, lay_out(v, w, native_of)) for a, w, v in sample.reads]
    low, high = sample.sp - FRAME_WINDOW, sample.sp + 16  # the frames below and the four argument home words
    wants = [
        (a & 0x1FFFFFFF, w, lay_out(v, w, native_of))
        for a, w, v in sample.stores
        if not low
        <= a
        < high  # the dead frames of the call and its callees, and the home words the original spills into
    ]
    return Case(f"c{index}", sample, proto, ret_kind(proto), entry, words, blind, reads, wants)


def plan_text(cases: list[Case], results: Path) -> str:
    lines = [f"out {results}", f"timeout {CASE_SECONDS}"]
    for c in cases:
        lines += [f"case {c.id}", f"fn {c.entry:x} {c.kind}", "args " + " ".join(f"{w:x}" for w in c.words)]
        lines += [f"in {p:x} {w} {v:x}" for p, w, v in c.reads]
        lines += [f"want {p:x} {w}" for p, w, _ in c.wants]
        lines.append("go")
    return "\n".join(lines) + "\n"


@dataclass
class Outcome:
    status: str  # ok, crash, timeout, exit, nomem, nofn
    detail: list[str] = field(default_factory=list)
    eax: int = 0
    edx: int = 0
    bits: int = 0
    got: dict[tuple[int, int], int] = field(default_factory=dict)


def parse_results(text: str) -> dict[str, Outcome]:
    outcomes: dict[str, Outcome] = {}
    current: Outcome | None = None
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "res" and len(parts) >= 3:
            current = Outcome(parts[2], parts[3:])
            if parts[2] == "ok" and len(parts) >= 6:
                current.eax, current.edx, current.bits = int(parts[3], 16), int(parts[4], 16), int(parts[5], 16)
            outcomes[parts[1]] = current
        elif parts[0] == "got" and current is not None and len(parts) == 4:
            current.got[(int(parts[1], 16), int(parts[2]))] = int(parts[3], 16)
    return outcomes


def _float_close(a: int, b: int, bits: int) -> bool:
    """Two floats (as bits) within ULPS of each other and both normal numbers: rounding, not a different value."""
    exponent = 0xFF if bits == 32 else 0x7FF
    shift = 23 if bits == 32 else 52
    if (a ^ b) >> (bits - 1):
        return False
    for v in (a, b):
        e = (v >> shift) & exponent
        if e == 0 or e == exponent:
            return False
    return abs(a - b) <= ULPS


def _hex(value: int, width: int = 4) -> str:
    return f"0x{value & ((1 << (8 * width)) - 1):0{2 * width}X}"


@dataclass
class Mismatch:
    field: str  # return, memory or crash
    address: int | None
    width: int
    port: str
    oracle: str


def compare_case(case: Case, outcome: Outcome, native_of: dict[int, int]) -> list[Mismatch]:
    found: list[Mismatch] = []
    sample = case.sample
    if comparable_return(case.proto):
        kind = case.proto.ret
        v0, v1 = sample.ret
        if case.kind == "f":
            want, have = sample.f0 & 0xFFFFFFFF, outcome.bits & 0xFFFFFFFF
            if want != have and not _float_close(want, have, 32):
                found.append(Mismatch("return", None, 4, _hex(have), _hex(want)))
        elif case.kind == "d":
            want, have = sample.f0, outcome.bits
            if want != have and not _float_close(want, have, 64):
                found.append(Mismatch("return", None, 8, _hex(have, 8), _hex(want, 8)))
        elif case.kind == "q":
            want, have = (v0 << 32) | v1, (outcome.edx << 32) | outcome.eax
            if want != have:
                found.append(Mismatch("return", None, 8, _hex(have, 8), _hex(want, 8)))
        else:
            mask = INT_MASK.get(kind, 0xFFFFFFFF)
            want = native_of.get(v0, v0) if kind in POINTERS else v0
            if (want & mask) != (outcome.eax & mask):
                found.append(Mismatch("return", None, 4, _hex(outcome.eax & mask), _hex(want & mask)))
    for physical, width, want in case.wants:
        have = outcome.got.get((physical, width))
        if have is None or have == want:
            continue
        if width == 4 and _float_close(have, want, 32):
            continue
        found.append(Mismatch("memory", 0x80000000 | physical, width, _hex(have, width), _hex(want, width)))
    return found[:MAX_PER_CASE]


class Elf:
    """The port executable's functions: name -> address, and the function holding an address."""

    def __init__(self, path: Path) -> None:
        self.path = path
        functions = {n: row for n, row in tas_coverage.elf_functions(path).items() if ".cold" not in n}
        self.by_name = {n: address for n, (address, _) in functions.items()}
        self.rows = sorted((address, size, n) for n, (address, size) in functions.items())

    def holding(self, address: int) -> tuple[str, int] | None:
        low, high = 0, len(self.rows)
        while low < high:
            mid = (low + high) // 2
            if self.rows[mid][0] <= address:
                low = mid + 1
            else:
                high = mid
        if low:
            start, size, name = self.rows[low - 1]
            if address < start + max(size, 1):
                return name.removesuffix(".cold"), address - start
        return None


def judge(
    case: Case, outcome: Outcome | None, elf: Elf, game: set[str], native_of: dict[int, int]
) -> tuple[str, list[Mismatch], str]:
    """(verdict, mismatches, why): same, differs, or untestable with the reason."""
    if outcome is None:
        return "untestable", [], "no result (the port ended before the case)"
    if outcome.status == "ok":
        found = compare_case(case, outcome, native_of)
        return ("differs", found, "") if found else ("same", [], "")
    if outcome.status == "crash":
        signal_number = int(outcome.detail[0]) if outcome.detail else 0
        address = int(outcome.detail[2], 16) if len(outcome.detail) > 2 else 0
        where = elf.holding(address) if address else None
        name = where[0] if where else None
        if name and name in game:
            at = f"{name}+0x{where[1]:x}"
            sig = {11: "SIGSEGV", 7: "SIGBUS", 8: "SIGFPE", 4: "SIGILL", 6: "SIGABRT"}.get(
                signal_number, f"signal {signal_number}"
            )
            fault = int(outcome.detail[1], 16) if len(outcome.detail) > 1 else 0
            return "differs", [Mismatch("crash", fault or None, 4, f"{sig} at {at}", "returned")], ""
        return "untestable", [], f"fault in {name or 'unknown code'} (outside the game's functions)"
    if outcome.status == "exit":
        code = outcome.detail[0] if outcome.detail else "?"
        return (
            "untestable",
            [],
            "stand-in for assembly (boot-missing)" if code == "78" else f"the port ended (exit {code})",
        )
    if outcome.status == "timeout":
        return "untestable", [], "timeout"
    if outcome.status == "nomem":
        return "untestable", [], "input outside RDRAM"
    return "untestable", [], outcome.status


# --- findings ---------------------------------------------------------------------------------------------------


def write_findings(
    path: Path, rows: list[dict[str, Any]], functions: set[str], kind: str = KIND, key: str = "function"
) -> None:
    """Replace this tool's earlier lines of `kind` for `functions` (the `key` field) with `rows`; every other line
    stays as it is."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        kept: list[str] = []
        changed = False
        for line in handle.read().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                kept.append(line)
                continue
            if row.get("kind") == kind and row.get(key) in functions:
                changed = True
                continue
            kept.append(line)
        text = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
        if changed:
            handle.seek(0)
            handle.truncate()
            handle.write("".join(k + "\n" for k in kept) + text)
        else:
            handle.seek(0, os.SEEK_END)
            handle.write(text)
        handle.flush()


def oracle_rows(aborts: list[OracleAborted], batch: str, now: float) -> list[dict[str, Any]]:
    """One finding per movie whose original aborted: the movie cannot be compared, and says why."""
    return [
        {
            "kind": ORACLE_KIND,
            "time": round(now, 3),
            "movie": a.movie,
            "signature": f"{ORACLE_KIND}:{a.movie}",
            "oracle_exit": a.code,
            "evidence": a.tail,
            "batch": batch,
        }
        for a in aborts
    ]


def finding_rows(case: Case, found: list[Mismatch], evidence: str, batch: str, now: float) -> list[dict[str, Any]]:
    s = case.sample
    rows = []
    for m in found:
        field_name = m.field if m.address is None else f"{m.field}@{m.address:#010x}"
        rows.append(
            {
                "kind": KIND,
                "time": round(now, 3),
                "function": s.function,
                "movie": s.movie,
                "frame": s.frame,
                "signature": f"{KIND}:{s.function}:{field_name}",
                "sample": {"call": s.index, "args": [_hex(a) for a in s.args], "sp": _hex(s.sp), "pak": s.pak},
                "field": m.field,
                "addr": _hex(m.address) if m.address is not None else None,
                "width": m.width,
                "port": m.port,
                "oracle": m.oracle,
                "prototype": case.proto.source if case.proto.known else "inferred",
                "evidence": evidence,
                "batch": batch,
            }
        )
    return rows


# --- the batch --------------------------------------------------------------------------------------------------


def _source(profile: Profile, options: Options) -> Path:
    if options.source is not None:
        return options.source.resolve()
    table = profile.section("tas")
    pin = table.get("serve", {}).get("pin")
    if pin:
        return profile.path("tas.serve.pin", pin)
    return Path(profile.section("tools")["project"])


def run_port(
    cfg: dict[str, Any], port: Path, run: Path, pak: str, cases: list[Case]
) -> tuple[dict[str, Outcome], dict[str, Any]]:
    """The port executable on a plan: results by case id, and the wall and CPU seconds it took."""
    results = run / f"results-{pak}.txt"
    plan = run / f"plan-{pak}.txt"
    results.unlink(missing_ok=True)
    plan.write_text(plan_text(cases, results))
    user = cfg["runs"] / "replay" / "user"  # one user folder: the game data is extracted from the ROM once
    user.mkdir(parents=True, exist_ok=True)
    local = {**cfg, "port": port}
    argv = tas._argv(local, user, pak, None)
    env = {**os.environ, **cfg["env"], "N64C_REPLAY": str(plan)}
    began = time.monotonic()
    with (run / f"port-{pak}.txt").open("w") as output:
        process = subprocess.Popen(
            argv, env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, **procguard.owned()
        )
        code, cpu = tas._wait(process, max(60.0, 0.5 * len(cases)))
    timing = {"wall_seconds": round(time.monotonic() - began, 1), "cpu_seconds": cpu, "exit": code}
    if not results.is_file():
        tail = (run / f"port-{pak}.txt").read_text(errors="replace").strip().splitlines()[-3:]
        raise ReplayError(
            "behave.replay.port",
            f"the port ran no case (exit {code}): {' | '.join(tail)}; the build needs shim boot/replay.c",
        )
    return parse_results(results.read_text()), timing


def run(profile: Profile, options: Options) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    began, own_began = time.monotonic(), time.process_time()
    spent = 0.0  # CPU seconds of the oracle and port processes (wait4 of each process tree) so far
    cfg = tas.settings(profile)
    oracle = oracle_host(profile, options)
    port = (options.port or cfg["port"]).resolve()
    if not port.is_file():
        raise ReplayError("behave.replay.port", f"no port executable at {port}")
    movies = options.movies or [m for m in _corpus_movies(cfg)]
    if not movies:
        raise ReplayError("behave.replay.movies", "no movie to replay (name some with --movie, or fill the corpus)")
    stages: dict[str, float] = {}
    recorded, samples, skipped, seen = [], [], Counter(), set()
    aborts: list[OracleAborted] = []
    for name in movies:
        if spent + time.process_time() - own_began > options.cpu_budget:
            recorded.append(
                {"movie": name, "skipped": f"the batch spent {spent:.0f} CPU s of {options.cpu_budget:.0f}"}
            )
            continue
        try:
            info = record_movie(profile, cfg, oracle, name, options)
        except OracleAborted as aborted:
            recorded.append(
                {"movie": name, "skipped": f"oracle aborted (exit {aborted.code})", "oracle_abort": aborted.tail}
            )
            aborts.append(aborted)
            continue
        spent += 0.0 if info["cached"] else info.get("cpu_seconds", 0.0)
        found, off, reached = read_samples(info["path"], name, info["pak"])
        samples += found
        skipped.update(off)
        seen |= reached
        recorded.append(
            {k: v for k, v in info.items() if k != "path"} | {"samples": len(found), "functions": len(reached)}
        )
    stages["record"] = round(time.monotonic() - began, 1)
    if options.functions:
        wanted = set(options.functions)
        samples = [s for s in samples if s.function in wanted]
    chosen = pick(samples, options.calls)
    unsampled = {
        fn: "no replayable recording" if fn in seen else "not reached by the oracle"
        for fn in sorted(set(options.functions) - chosen.keys())
    }
    source = _source(profile, options)
    config = _config(profile)
    version = options.version or _names_from(source)
    guest = guest_addresses(source, version)
    elf = Elf(port)
    protos = Prototypes(source, tuple(config["headers"]))
    native_of = function_pointers(guest, elf.by_name)
    game = {n for n in guest if n in elf.by_name}
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    batch = cfg["runs"] / "replay" / stamp
    batch.mkdir(parents=True, exist_ok=True)
    cases: dict[str, list[Case]] = {}
    missing: list[str] = []
    numbered = 0
    for fn in sorted(chosen):
        entry = elf.by_name.get(fn)
        if entry is None:
            missing.append(fn)
            continue
        for s in chosen[fn]:
            cases.setdefault(s.pak, []).append(make_case(numbered, s, protos.get(fn), entry, native_of))
            numbered += 1
    stages["plan"] = round(time.monotonic() - began - stages["record"], 1)
    verdicts: dict[str, Counter[str]] = {}
    why: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    differing: dict[str, list[str]] = {}
    timings: dict[str, Any] = {}
    now = time.time()
    for pak, group in sorted(cases.items()):
        outcomes, timings[pak] = run_port(cfg, port, batch, pak, group)
        spent += timings[pak]["cpu_seconds"]
        for case in group:
            verdict, found, reason = judge(case, outcomes.get(case.id), elf, game, native_of)
            verdicts.setdefault(case.sample.function, Counter())[verdict] += 1
            if verdict == "untestable":
                why[reason] += 1
            if found:
                evidence = f"{batch}/results-{pak}.txt#{case.id}"
                rows += finding_rows(case, found, evidence, stamp, now)
                differing.setdefault(case.sample.function, []).extend(sorted({m.field for m in found}))
    functions = set(verdicts)
    if options.findings:
        write_findings(cfg["runs"] / FINDINGS, rows, functions)
        write_findings(
            cfg["runs"] / FINDINGS, oracle_rows(aborts, stamp, now), {a.movie for a in aborts}, ORACLE_KIND, "movie"
        )
    per_function = {
        "same": sorted(f for f, c in verdicts.items() if c["differs"] == 0 and c["same"] > 0),
        "differs": sorted(differing),
        "untestable": sorted(f for f, c in verdicts.items() if not c["same"] and not c["differs"]),
    }
    own = round(time.process_time() - own_began, 1)
    cpu = round(spent + own, 1)
    incomplete = bool(aborts or unsampled or missing or per_function["untestable"] or not verdicts)
    verdict = "differs" if differing else "untestable" if incomplete else "same"
    result = {
        "verdict": verdict,
        "batch": str(batch),
        "movies": recorded,
        "oracle": str(oracle["host"]),
        "port": str(port),
        "functions": {
            "reached": len(seen),
            "sampled": len(chosen),
            "replayed": len(verdicts),
            "same": len(per_function["same"]),
            "differs": len(per_function["differs"]),
            "untestable": len(per_function["untestable"]),
            "not_in_port": len(missing),
            "unsampled": len(unsampled),
        },
        "samples": {"recorded": len(samples), "replayed": sum(len(g) for g in cases.values())},
        "written_off": dict(skipped),
        "oracle_aborts": {a.movie: a.tail for a in aborts},
        "untestable_reasons": dict(why.most_common()),
        "differing": {f: sorted(set(v)) for f, v in sorted(differing.items())},
        "not_in_port": missing[:50],
        "unsampled": unsampled,
        "findings": {"file": str(cfg["runs"] / FINDINGS), "written": len(rows)} if options.findings else None,
        "timing": {
            "wall_seconds": round(time.monotonic() - began, 1),
            "cpu_seconds": cpu,
            "cpu_budget": options.cpu_budget,
            "stages": stages,
            "port": timings,
        },
    }
    (batch / "report.json").write_text(json.dumps(result, indent=1))
    return result, 0, []


def _corpus_movies(cfg: dict[str, Any]) -> list[str]:
    path = cfg["movies"] / "corpus.json"
    with contextlib.suppress(OSError, ValueError):
        entries = json.loads(path.read_text()).get("entries", {})
        if entries:
            return sorted(entries)
    return [p.stem for p in tas.recordings(cfg)]


def _config(profile: Profile) -> dict[str, Any]:
    from abu_decomp.behave.run import configuration

    return configuration(profile)


def _names_from(source: Path) -> str:
    import tomllib

    try:
        return str(tomllib.loads((source / "config.toml").read_text())["names_from"])
    except (OSError, KeyError, tomllib.TOMLDecodeError) as error:
        raise ReplayError(
            "behave.replay.version", f"{source}/config.toml names no names_from; pass --version"
        ) from error
