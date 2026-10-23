"""The sanitizer port and random-input runs: `tas play --sanitize`, `tas monkey`.
The sanitizer port is a separate build of the game (rage-ports `-DRAGE_DECOMP_SANITIZE=ON`, i386, AddressSanitizer plus
UBSan on the original units, the shim and the game glue, every report recoverable) in its own build directory, named
by `tas.sanitizer.port` in the profile; it never replaces the page's port. `tas play --sanitize` (or `--port` with any
sanitizer build) replays a movie on it with the runtime options this module sets, so one run goes on after a report and
lists many. When the run ends `collect` reads the run's output and files every report as one JSON line in
`<tas.runs>/findings.jsonl` (the file the triage bucketing reads):

    {"schema": 1, "kind": "asan"|"ubsan", "movie", "run", "poll", "function", "file", "line", "site": "file:line",
     "type": the error kind, "message", "signature": "kind:type:site", "report", "evidence", "port", "build",
     "count": reports of the same signature in this run, "time"}

`report` is the report's own text (with its stack) in `RUN/sanitizer/NNN-kind.txt`. The shim tags every report with the
controller poll it happened in (`n64c-san: poll=N` after the runtime's SUMMARY line, N64C_SANITIZER_HOOK); a report
without the tag has `poll` null. A report repeats a signature (kind, type, file:line) only once per run, with `count`.
`monkey` records a movie of seeded random controller input (optionally after a prefix movie that reaches a start
point), plays it on the sanitizer port and keeps the movie when it found something, so any finding replays.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, tas_findings

FILE, DIR = "findings.jsonl", "sanitizer"
SCHEMA = 1
# Recoverable, quiet about leaks (the game never frees at exit), symbolized in-process from the build's own debug info.
ENV = {
    "ASAN_OPTIONS": "halt_on_error=0:handle_segv=0:detect_leaks=0:abort_on_error=0:print_summary=1:symbolize=1:"
    "detect_stack_use_after_return=0:allocator_may_return_null=1:strip_path_prefix=",
    "UBSAN_OPTIONS": "halt_on_error=0:print_stacktrace=1:print_summary=1",
}
TIMEOUT_FACTOR = 4  # the instrumented game runs a few times slower than the plain port
MAX_REPORTS = 200  # distinct signatures filed per run; a flood past that only counts
_UB = re.compile(r"^(?P<path>\S[^:\n]*):(?P<line>\d+):(?P<col>\d+): runtime error: (?P<message>.*)$")
_ASAN = re.compile(r"^==\d+==ERROR: AddressSanitizer: (?P<type>[A-Za-z-]+)(?P<rest>.*)$")
_FRAME = re.compile(r"^\s+#(?P<n>\d+) 0x[0-9a-fA-F]+ (?:in (?P<fn>\S+)(?: (?P<where>\S+))?|\((?P<lib>[^)]*)\))")
_SUMMARY = re.compile(r"^SUMMARY: (?P<tool>\w+): (?P<text>.*)$")
_POLL = re.compile(r"^n64c-san: poll=(?P<poll>-?\d+)\s*$")
_LINE = re.compile(r"^(?P<file>.*?):(?P<line>\d+)(?::\d+)?$")
_RUNTIME = re.compile(
    r"__interceptor_|__asan_|__sanitizer|__ubsan|libsanitizer|libasan|libubsan|/asan/|sanitizer_common"
)
_UB_TYPES = (
    ("insufficient space", "object-size"),
    ("shift", "shift"),
    ("signed integer overflow", "signed-integer-overflow"),
    ("index", "out-of-bounds"),
    ("out of bounds", "out-of-bounds"),
    ("null pointer", "null-pointer"),
    ("misaligned", "misaligned-address"),
    ("division by zero", "divide-by-zero"),
    ("not a valid value", "invalid-value"),
    ("outside the range", "float-cast-overflow"),
    ("overflow", "overflow"),
    ("applying non-zero offset", "pointer-overflow"),
    ("variable length array", "vla-bound"),
    ("negative", "negative-value"),
    ("call to function", "function-type-mismatch"),
)


def settings(table: Any) -> dict[str, Any]:
    """The profile's `tas.sanitizer` section: {"port": path of the sanitizer build's executable}."""
    return dict(table) if isinstance(table, dict) else {}


def options_env() -> dict[str, str]:
    return dict(ENV)


def relative(path: str) -> str:
    """A source path as the repo knows it: below the decomp tree (ragewars-decomp/ or rage_decomp_source-SHA/),
    from shim/ or platform/port/ for the port's own code, else the last two components."""
    path = path.replace("\\", "/")
    match = re.search(r"(?:ragewars-decomp|rage_decomp_source-[0-9a-f]+)/(.+)$", path)
    if match:
        return match[1]
    for mark in ("/shim/", "/platform/port/", "/boot/"):
        at = path.rfind(mark)
        if at >= 0:
            return path[at + 1 :]
    return "/".join(path.split("/")[-2:])


def _where(text: str | None) -> tuple[str, int] | None:
    match = _LINE.match(text or "")
    return (relative(match["file"]), int(match["line"])) if match else None


def ub_type(message: str) -> str:
    lowered = message.lower()
    for needle, name in _UB_TYPES:
        if needle in lowered:
            return name
    return "undefined-behavior"


def _frame_site(frames: list[dict[str, Any]]) -> dict[str, Any]:
    """The first frame that belongs to the game or shim: not the sanitizer runtime, not a bare library address."""

    def game(f: dict[str, Any]) -> bool:
        return bool(f["fn"]) and not _RUNTIME.search(f["fn"] + " " + (f["where"] or "")) and f["site"] is not None

    for frame in frames:
        if game(frame):
            return frame
    for frame in frames:
        if frame["fn"] and not _RUNTIME.search(frame["fn"]):
            return frame
    return frames[0] if frames else {"fn": None, "site": None}


def blocks(text: str) -> list[dict[str, Any]]:
    """Every sanitizer report in a run's output: kind, type, message, frames, the report's own text and its poll."""
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    last: dict[str, Any] | None = None  # the newest finished report, waiting for its poll tag

    def finish() -> None:
        nonlocal current, last
        if current is not None:
            out.append(current)
            last = current
        current = None

    for line in text.splitlines():
        ub, asan, frame = _UB.match(line), _ASAN.match(line), _FRAME.match(line)
        poll = _POLL.match(line)
        if poll:
            if current is not None:  # the tag follows its report's summary line
                current["poll"] = int(poll["poll"])
                finish()
            elif last is not None and last["poll"] is None:
                last["poll"] = int(poll["poll"])
            continue
        if ub:
            finish()
            current = {
                "kind": "ubsan",
                "type": ub_type(ub["message"]),
                "message": ub["message"],
                "at": (relative(ub["path"]), int(ub["line"])),
                "frames": [],
                "text": [line],
                "poll": None,
            }
            continue
        if asan:
            finish()
            current = {
                "kind": "asan",
                "type": asan["type"],
                "message": line.split("ERROR: ", 1)[1],
                "at": None,
                "frames": [],
                "text": [line],
                "poll": None,
            }
            continue
        if current is None:
            continue
        if frame:
            site = _where(frame["where"])
            current["frames"].append({"fn": frame["fn"], "where": frame["where"], "site": site})
            current["text"].append(line)
        elif _SUMMARY.match(line):
            current["text"].append(line)
            current["summary"] = line
        elif current["kind"] == "ubsan" or current.get("summary") or line.startswith("n64c-"):
            finish()  # a UBSan report is its line, stack and summary; a line after a summary ends any report
        else:
            current["text"].append(line)  # ASan's body: access, allocation stacks, shadow dump
    finish()
    return out


def finding(block: dict[str, Any]) -> dict[str, Any]:
    """The block's identity: function, file, line, and the signature triage buckets by."""
    frame = _frame_site(block["frames"])
    site = block["at"] if block["kind"] == "ubsan" and block["at"] else frame.get("site")
    if site is None and block.get("summary"):
        m = re.search(r"(\S+):(\d+)(?::\d+)? in (\S+)", block["summary"])
        if m:
            site = (relative(m[1]), int(m[2]))
            frame = {**frame, "fn": frame.get("fn") or m[3]}
    function = frame.get("fn")
    file, line = site if site else (None, None)
    where = f"{file}:{line}" if file else None
    return {
        "kind": block["kind"],
        "type": block["type"],
        "message": block["message"],
        "function": function,
        "file": file,
        "line": line,
        "site": where,
        "signature": f"{block['kind']}:{block['type']}:{where or function or 'unknown'}",
        "poll": block["poll"],
    }


def collect(
    cfg: dict[str, Any],
    run: Path,
    output: str,
    *,
    movie: str,
    port: Path | str,
    build: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    """File the reports of a finished run: RUN/sanitizer/NNN-kind.txt per signature and one findings.jsonl line each.
    Returns {"findings": n, "reports": all reports counted, "asan": n, "ubsan": n, "file": path}; n is 0 when clean."""
    seen: dict[str, dict[str, Any]] = {}
    total = 0
    for block in blocks(output):
        total += 1
        row = finding(block)
        if row["signature"] in seen:
            seen[row["signature"]]["count"] += 1
            continue
        if len(seen) >= MAX_REPORTS:
            continue
        row["count"] = 1
        row["_text"] = "\n".join(block["text"]) + "\n"
        seen[row["signature"]] = row
    stamp = round(now if now is not None else time.time(), 1)  # epoch seconds, like every writer of the feed
    lines = []
    for index, row in enumerate(seen.values(), 1):
        report = run / DIR / f"{index:03d}-{row['kind']}.txt"
        report.parent.mkdir(exist_ok=True)
        report.write_text(row.pop("_text"))
        row.update(
            schema=SCHEMA,
            movie=movie,
            run=run.name,
            report=str(report),
            evidence=str(report),
            port=str(port),
            build=build,
            time=stamp,
        )
        row.update(fn=row["function"], fp=row["signature"])  # tas_findings buckets by fn, one finding per site
        lines.append(row)
    path = tas_findings.path(cfg["runs"])
    tas_findings.append(cfg["runs"], lines)
    kinds = [row["kind"] for row in seen.values()]
    return {
        "findings": len(seen),
        "reports": total,
        "asan": kinds.count("asan"),
        "ubsan": kinds.count("ubsan"),
        "file": str(path),
    }


def read(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Every line of findings.jsonl (a torn or foreign line is skipped)."""
    rows = []
    with contextlib.suppress(OSError):
        for line in (cfg["runs"] / FILE).read_text().splitlines():
            with contextlib.suppress(ValueError):
                row = json.loads(line)
                if isinstance(row, dict) and row.get("kind") in ("asan", "ubsan"):
                    rows.append(row)
    return rows


def grouped(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Findings grouped by file:line (then kind and type): count of runs and movies, one example each."""
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row.get("site") or row.get("function") or "unknown", row.get("kind", ""), row.get("type", ""))
        group = groups.setdefault(
            key,
            {
                "site": key[0],
                "kind": key[1],
                "type": key[2],
                "function": row.get("function"),
                "reports": 0,
                "movies": set(),
                "example": row,
            },
        )
        group["reports"] += int(row.get("count") or 1)
        group["movies"].add(row.get("movie"))
    out = []
    for group in sorted(groups.values(), key=lambda g: (-g["reports"], g["site"])):
        example = group.pop("example")
        movies = sorted(m for m in group.pop("movies") if m)
        out.append({**group, "movies": movies, "poll": example.get("poll"), "report": example.get("report")})
    return out


# Monkey: seeded random controller input.
_MASK = (1 << 64) - 1
# Buttons a random player presses, with how often (A/B/START and the d-pad matter most to a menu or a level).
_PRESS = (
    (0x8000, 6),  # A
    (0x4000, 5),  # B
    (0x2000, 3),  # Z
    (0x1000, 3),  # START
    (0x0800, 3),
    (0x0400, 3),
    (0x0200, 3),
    (0x0100, 3),  # d-pad
    (0x0020, 1),
    (0x0010, 1),  # L R
    (0x0008, 2),
    (0x0004, 2),
    (0x0002, 2),
    (0x0001, 2),  # C buttons
)


class Splitmix:
    """splitmix64: a fixed, documented generator, so a seed names the same input on any Python."""

    def __init__(self, seed: int) -> None:
        self.state = seed & _MASK

    def next(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & _MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK
        return z ^ (z >> 31)

    def below(self, n: int) -> int:
        return (self.next() >> 11) % n


def random_polls(seed: int, count: int, *, hold: int = 24) -> list[tuple[int, int, int]]:
    """`count` polls of random input: segments of 1..hold equal polls, 0-3 buttons (weighted), the stick centred half
    the time and else anywhere in the controller's +-80 range. Deterministic per (seed, count, hold)."""
    rng = Splitmix(seed)
    total = sum(weight for _, weight in _PRESS)
    polls: list[tuple[int, int, int]] = []
    while len(polls) < count:
        length = 1 + rng.below(hold)
        buttons = 0
        for _ in range(rng.below(4)):
            pick = rng.below(total)
            for bit, weight in _PRESS:
                if pick < weight:
                    buttons |= bit
                    break
                pick -= weight
        if rng.below(2):
            x = y = 0
        else:
            x, y = rng.below(161) - 80, rng.below(161) - 80
        polls.extend([(buttons, x, y)] * length)
    return polls[:count]


def name_for(seed: int, count: int) -> str:
    return f"monkey-{seed}-{count}"


def check_args(seed: int, polls: int, hold: int) -> None:
    if seed < 0:
        raise DecompError("tas.monkey", f"a seed is 0 or more, not {seed}")
    if not 1 <= polls <= 1_000_000:
        raise DecompError("tas.monkey", f"polls is 1 .. 1000000, not {polls}")
    if not 1 <= hold <= 600:
        raise DecompError("tas.monkey", f"hold is 1 .. 600 polls, not {hold}")


def present(text: str) -> bool:
    """The output carries a sanitizer report (a sanitizer port started by `--port`, not `--sanitize`)."""
    return "runtime error: " in text or "AddressSanitizer" in text or "n64c-san: poll=" in text


def seeds_of(spec: str) -> list[int]:
    """`3`, `1-8` or `1,4,9-11`: the seeds of a batch."""
    seeds: list[int] = []
    for part in spec.split(","):
        low, _, high = part.strip().partition("-")
        try:
            first, last = int(low), int(high or low)
        except ValueError:
            raise DecompError("tas.monkey", f"seeds are numbers, ranges A-B or a comma list, not {spec!r}") from None
        if last < first or last - first > 10_000:
            raise DecompError("tas.monkey", f"seed range {part!r} is empty or longer than 10000")
        seeds.extend(range(first, last + 1))
    return seeds


def movie_name(seed: int, polls: int, prefix: str | None) -> str:
    tag = f"-{prefix[:36]}" if prefix else ""
    return f"monkey{tag}-{seed}-{polls}"


def monkey(
    profile: Any,
    seeds: list[int],
    *,
    polls: int,
    hold: int = 24,
    prefix: str | None = None,
    port: Path | str | None = None,
    jobs: int = 2,
    keep: str = "findings",
    speed: int = 0,
    label: str | None = None,
) -> dict[str, Any]:
    """Record one movie per seed (`prefix` movie's polls, then `polls` random ones), replay each on the sanitizer port
    and keep the movie when it found a report or the run did not end ok (`keep` all keeps every one, so any finding
    replays by its movie name). One row per seed."""
    from concurrent.futures import ThreadPoolExecutor

    from abu_decomp import tas

    if keep not in ("findings", "all"):
        raise DecompError("tas.monkey", f"keep is findings or all, not {keep!r}")
    for seed in seeds:
        check_args(seed, polls, hold)
    cfg = tas.settings(profile)
    pak, cpak, head, card = "on", "none", [], None
    if prefix:
        head, pak, cpak = tas.movie_polls(cfg, prefix), tas.movie_pak(cfg, prefix), tas.movie_cpak(cfg, prefix)
        card = tas.start_card(cfg, cpak) if len(cpak) == 64 else None
    folder = cfg["runs"] / "monkey"
    folder.mkdir(parents=True, exist_ok=True)

    def one(seed: int) -> dict[str, Any]:
        name = movie_name(seed, polls, prefix)
        raw = folder / f"{name}.raw"
        raw.write_text(tas.format_raw([*head, *random_polls(seed, polls, hold=hold)]))
        note = f"monkey seed={seed} polls={polls} hold={hold} prefix={prefix or '-'}"
        tas.save(profile, name, source=raw, note=note, replace=True, pak=pak, cpak=cpak, cpak_image=card)
        try:
            played = tas.play(profile, name, speed=speed, port=port, label=label, sanitize=True)
        except DecompError as error:
            tas.delete(profile, name)
            return {
                "seed": seed,
                "movie": name,
                "outcome": "refused",
                "subject": str(error),
                "findings": 0,
                "kept": False,
            }
        found = played.get("sanitizer") or {}
        clean = played["outcome"] == "ok" and not found.get("findings")
        kept = keep == "all" or not clean
        if not kept:
            tas.delete(profile, name)
        return {
            "seed": seed,
            "movie": name,
            "run": played["run"],
            "outcome": played["outcome"],
            "subject": played.get("subject", ""),
            "polls_reached": played.get("polls_reached", 0),
            "polls": played.get("polls", 0),
            "findings": found.get("findings", 0),
            "reports": found.get("reports", 0),
            "kept": kept,
            "wall_seconds": played.get("wall_seconds"),
            "cpu_seconds": played.get("cpu_seconds"),
        }

    with ThreadPoolExecutor(max_workers=max(1, min(jobs, len(seeds) or 1))) as pool:
        rows = list(pool.map(one, seeds))
    return {"movies": rows, "findings": sum(r["findings"] for r in rows), "summary": grouped(read(cfg))[:20]}
