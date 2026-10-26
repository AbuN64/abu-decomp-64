"""`decomp tas bake`: the static site the Runner page reads (GAME-RUNNER.md section 7), in `<tas.runs>/site/`.

Nothing here analyses anything new; it joins what other modules already know and writes it as files the page can cache:
- fns.HASH.json (search index and function grid: decomp state and reach as separate columns) and fnrows.HASH.txt (one
  name per row) from the map (module tas_phasemap) and the coverage table (module tas_coverage);
- summary.json: the table's totals, gcovr totals, the ladder stage (`decomp phase`) and the build;
- fn/NAME.json for every function: the C slice (Pygments), the line gutter from the gcovr union JSON, callers /
  callees / refs from the relocations of the page port's objects (`tas.build.units`), port and oracle counts,
  behaviour, reached-by movies, claims and behave runs;
- movies.json, posters/MOVIE.jpg and reach/MOVIE.bin per movie.
Writes are atomic (temp + rename); content-hashed names are immutable; manifest.json is written last. Each name ships
with a `.gz` sibling. `--what` picks the inputs that are read again (source, xref, coverage, movie); the parts of the
others come from `<tas.runs>/site.parts/`. A bake runs under the seat pool at batch priority and holds the site lock
for its whole write.
"""

from __future__ import annotations

import contextlib
import fcntl
import gzip
import hashlib
import io
import json
import os
import re
import struct
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, evidence, slots, tas, tas_coverage, tas_fn, tas_phasemap

WHAT = ("source", "xref", "coverage", "movie")
SITE, PARTS, LOCK = "site", "site.parts", "site.lock"
UNION = "coverage/union.json"
MANIFEST_VERSION = 1
POSTER_QUALITY = 72
MAX_SLICE = 400  # lines of C kept per function
MAX_MOVIES = 24
MAX_LINKS = 60
MAX_BEHAVE = 6
MAX_CASES = 8
MAX_CLAIMS = 6
SAFE_NAME = re.compile(r"^[A-Za-z_][\w.$]*$")
NOISE = ("__", "_GLOBAL", "_sub_", "_Z")  # compiler and instrumentation symbols that are not the game's data


# --- files ---


def gz(body: bytes) -> bytes:
    return gzip.compress(body, 9, mtime=0)


def put(path: Path, body: bytes, *, compress: bool = True) -> bool:
    """Write `body` (and its `.gz` sibling) atomically; False when the file already holds exactly this."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.read_bytes() == body:
            return False
    except OSError:
        pass
    for target, data in ((path, body), (path.with_name(path.name + ".gz"), gz(body) if compress else None)):
        if data is None:
            continue
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
    return True


def dump(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def short(body: bytes) -> str:
    return hashlib.sha1(body).hexdigest()[:10]


@contextlib.contextmanager
def locked(runs: Path) -> Iterator[None]:
    """One writer of the site at a time (a movie bake after each play, a full bake after a train round)."""
    runs.mkdir(parents=True, exist_ok=True)
    with (runs / LOCK).open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def part_read(runs: Path, name: str) -> dict[str, Any] | None:
    try:
        value = json.loads((runs / PARTS / f"{name}.json").read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def part_write(runs: Path, name: str, value: dict[str, Any]) -> None:
    path = runs / PARTS / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, separators=(",", ":")))
    tmp.replace(path)


# --- inputs ---


class Inputs:
    """The profile's places, read once per bake."""

    def __init__(self, profile: Any, union: Path | None = None) -> None:
        self.profile = profile
        self.cfg = tas.settings(profile)
        self.runs: Path = self.cfg["runs"]
        self.site = self.runs / SITE
        serve = dict(profile.section("tas").get("serve", {}))
        build = dict(profile.section("tas").get("build", {}))
        self.pin = profile.path("tas.serve.pin", serve["pin"]) if "pin" in serve else None
        self.claims = profile.path("tas.serve.claims", serve["claims"]) if "claims" in serve else None
        self.port_source = (
            profile.path("tas.serve.port_source", serve["port_source"]) if "port_source" in serve else None
        )
        given = serve.get("invocations", [])
        self.invocations = [
            str(profile.path("tas.serve.invocations", item)) for item in ([given] if isinstance(given, str) else given)
        ]
        self.units = profile.path("tas.build.units", build["units"]) if "units" in build else None
        self.union = union or self.runs / UNION
        cov = tas_coverage.settings(profile, self.cfg)
        self.version = cov["version"] if cov else "us-rev1"
        self.table = tas_coverage.read_table(self.cfg) or {}

    def sha(self) -> str | None:
        if not self.pin:
            return None
        done = subprocess.run(
            ["git", "-C", str(self.pin), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
        )
        return done.stdout.strip() or None

    def map_body(self) -> dict[str, Any]:
        """The /api/phase/map body (module tas_phasemap), computed the way the server does."""
        phase = tas_phasemap.Phase(self.profile, self.cfg, self.pin, lambda: self.table)
        body, status = phase.map(self.version)
        if status != 200:
            raise DecompError("tas.bake", f"no function map for {self.version}: {body.get('error')}")
        return body


DECOMP_STATE = {"unmapped": "asm", "unassigned": "asm", "data": "asm", "fuzzy": "fuzzy", "fakematch": "exact"}


def reach_of(row: dict[str, Any] | None) -> str:
    if not row:
        return "none"
    port, oracle = (row.get("port") or {}).get("hit"), (row.get("oracle") or {}).get("hit")
    return "both" if port and oracle else "port" if port else "oracle" if oracle else "none"


def function_rows(body: dict[str, Any], table: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    """(files, rows): [name, rom, size, decomp state, file#, reach, foundation] in ROM order. The map's combined
    `covered` state is not used: decomp state (named/exact/fuzzy/asm) and reach are separate columns."""
    cov = {r["name"]: r for r in table.get("functions", []) if r.get("in_version")}
    files: list[str] = []
    index: dict[str, int] = {}
    rows = []
    for name, rom, size, state, _pct, file, foundation in body["fns"]:
        if not SAFE_NAME.match(name):
            continue
        if state == "covered":  # exact C that something reached: the map hides its rung behind the reach colour
            state = "exact"
        if file not in index:
            index[file] = len(files)
            files.append(file)
        rows.append(
            [
                name,
                rom,
                size,
                DECOMP_STATE.get(state, state),
                index[file],
                reach_of(cov.get(name)),
                1 if foundation else 0,
            ]
        )
    return files, rows


# --- C slices ---


def highlighter() -> Any:
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import CLexer

    formatter, lexer = HtmlFormatter(nowrap=True), CLexer()
    return lambda text: highlight(text, lexer, formatter).rstrip("\n").split("\n")


def gcov_positions(union: dict[str, Any]) -> dict[tuple[str, str], tuple[int, int]]:
    """(source file, function) -> (first, last) line from the gcovr union's `functions[].pos`."""
    out = {}
    for entry in union.get("files", []):
        rel = rel_path(entry.get("file", ""))
        for fn in entry.get("functions", []):
            try:
                first, last = (int(p.split(":")[0]) for p in fn["pos"])
            except (KeyError, ValueError, TypeError):
                continue
            out[(rel, fn.get("name", ""))] = (first, last)
    return out


def rel_path(path: str) -> str:
    """The source path as the map names it: from the `src/` folder on."""
    _, sep, tail = path.rpartition("/src/")
    return f"src/{tail}" if sep else path


def read_union(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("files") else None


def header_line(lines: list[str], start: int) -> int:
    """0-based first line of the definition whose opening brace is on line `start`: its header lines are kept."""
    first = start
    while first > 0 and lines[first - 1].strip() and not re.search(r"[;}]\s*$|^\s*#", lines[first - 1]):
        first -= 1
    return first


def slices(inputs: Inputs, rows: list[list[Any]], files: list[str], union: dict[str, Any] | None) -> dict[str, Any]:
    """name -> {file, first, last, html}: the C of every function the pin defines in C, found by evidence.parse and,
    for the ones it misses, by the gcovr union's function positions; asm stand-ins get none."""
    if not inputs.pin:
        raise DecompError("tas.bake", "the profile has no tas.serve.pin: no source to slice")
    positions = gcov_positions(union) if union else {}
    highlight = highlighter()
    parsed: dict[str, tuple[list[str], dict[str, evidence.Fn]] | None] = {}
    out: dict[str, Any] = {}
    for row in rows:
        name, file = row[0], files[row[4]]
        if not file or file.endswith(".s"):
            continue
        if file not in parsed:
            try:
                text = (inputs.pin / file).read_text(errors="replace")
                parsed[file] = (text.splitlines(), evidence.parse(text))
            except OSError:
                parsed[file] = None
        got = parsed[file]
        if got is None:
            continue
        lines, fns = got
        fn = fns.get(name)
        if fn is not None and fn.kind != evidence.ASM:
            first, last = header_line(lines, fn.start) + 1, fn.end + 1
        elif fn is None and (file, name) in positions:
            first, last = positions[(file, name)]
        else:
            continue
        last = min(last, first + MAX_SLICE - 1, len(lines))
        if last < first:
            continue
        out[name] = {"file": file, "first": first, "last": last, "html": highlight("\n".join(lines[first - 1 : last]))}
    return out


def gutters(slice_of: dict[str, Any], union: dict[str, Any] | None) -> dict[str, Any]:
    """name -> one entry per slice line: null, [count] or [count, branches taken, branches total]. Empty while the
    gcovr union JSON does not exist: the page then says there are no line counts yet."""
    if not union:
        return {}
    by_file: dict[str, dict[int, Any]] = {}
    for entry in union.get("files", []):
        by_file[rel_path(entry.get("file", ""))] = {row["line_number"]: row for row in entry.get("lines", [])}
    out = {}
    for name, got in slice_of.items():
        lines = by_file.get(got["file"])
        if not lines:
            continue
        rows: list[Any] = []
        for number in range(got["first"], got["last"] + 1):
            line = lines.get(number)
            if line is None:
                rows.append(None)
                continue
            branches = line.get("branches") or []
            rows.append(
                [line["count"], sum(1 for b in branches if b["count"]), len(branches)] if branches else [line["count"]]
            )
        out[name] = rows
    return out


# --- cross references from relocations ---

SHT_SYMTAB, SHT_RELA, SHT_REL = 2, 4, 9
CALLS = {3: {2, 4}, 8: {4}}  # machine (i386, MIPS) -> relocation kinds that call (PC32 / PLT32, R_MIPS_26)
ADDRESSES = {3: {1, 3, 9}, 8: {2, 5}}  # R_386_32 / GOT32 / GOTOFF, R_MIPS_32 / HI16: a symbol's address taken


def object_refs(
    path: Path, functions: set[str]
) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, set[str]]]:
    """(calls, taken, data) of one ELF32 relocatable object, each keyed by the referring function or data symbol:
    function names it calls, function names whose address it takes, and data symbols it touches."""
    blob = path.read_bytes()
    if blob[:4] != b"\x7fELF" or blob[4] != 1:
        return {}, {}, {}
    order = "<" if blob[5] == 1 else ">"
    machine = struct.unpack_from(order + "H", blob, 18)[0]
    shoff = struct.unpack_from(order + "I", blob, 32)[0]
    shentsize, shnum, shstr = struct.unpack_from(order + "HHH", blob, 46)
    heads = [struct.unpack_from(order + "IIIIIIIIII", blob, shoff + i * shentsize) for i in range(shnum)]

    def cstr(base: int, at: int) -> str:
        end = blob.index(b"\0", base + at)
        return blob[base + at : end].decode(errors="replace")

    symbols: list[tuple[str, int, int, int, int]] = []  # name, value, size, type, shndx
    tab = next((h for h in heads if h[1] == SHT_SYMTAB), None)
    if tab is None:
        return {}, {}, {}
    strings = heads[tab[6]][4]
    for off in range(tab[4], tab[4] + tab[5], 16):
        name, value, size, info, _o, shndx = struct.unpack_from(order + "IIIBBH", blob, off)
        symbols.append((cstr(strings, name), value, size, info & 15, shndx))
    owners: dict[int, list[tuple[int, int, str]]] = {}
    for name, value, size, kind, shndx in symbols:
        if kind in (1, 2) and shndx and shndx < 0xFF00:  # OBJECT, FUNC
            owners.setdefault(shndx, []).append((value, size, name))
    names = [cstr(heads[shstr][4], h[0]) for h in heads]
    calls_kind, taken_kind = CALLS.get(machine, set()), ADDRESSES.get(machine, set())
    calls: dict[str, set[str]] = {}
    taken: dict[str, set[str]] = {}
    data: dict[str, set[str]] = {}
    for head in heads:
        if head[1] not in (SHT_REL, SHT_RELA) or not head[9]:
            continue
        target = head[7]
        section = names[target]
        if section.startswith(".debug") or section.startswith(".eh_frame") or section.startswith(".gcov"):
            continue
        local = owners.get(target, [])
        for off in range(head[4], head[4] + head[5], head[9]):
            r_offset, r_info = struct.unpack_from(order + "II", blob, off)
            sym = symbols[r_info >> 8] if (r_info >> 8) < len(symbols) else None
            if sym is None:
                continue
            who = next((n for v, s, n in local if v <= r_offset < v + max(s, 1)), None)
            if who is None:  # per-function sections (.text.NAME, .data.rel.local.NAME): the name ends the section
                suffix = section.rpartition(".")[2] if section.count(".") > 1 else None
                who = suffix if suffix in functions else None
            if who is None or sym[0] == who:
                continue
            kind = r_info & 0xFF
            if sym[3] == 3 or not sym[0] or sym[0].startswith("."):  # section symbols and literals
                continue
            if sym[0] in functions:
                if kind in calls_kind:
                    calls.setdefault(who, set()).add(sym[0])
                elif kind in taken_kind:
                    taken.setdefault(who, set()).add(sym[0])
            elif who in functions and not sym[0].startswith(NOISE) and kind in taken_kind | calls_kind:
                data.setdefault(who, set()).add(sym[0])
    return calls, taken, data


def xrefs(inputs: Inputs, functions: set[str]) -> dict[str, Any]:
    """name -> {callers, callees, refs, tables}: the relocations of the page port's objects (module docstring).
    A function whose address a function or a data table takes is reached through it; a caller list therefore also
    holds the functions that take its address, and `tables` the data symbols that hold it."""
    units = inputs.units
    if not units or not units.is_dir():
        raise DecompError("tas.bake", "the profile has no tas.build.units folder: no objects to read relocations from")
    callees: dict[str, set[str]] = {}
    callers: dict[str, set[str]] = {}
    refs: dict[str, set[str]] = {}
    tables: dict[str, set[str]] = {}
    for path in sorted(units.rglob("*.o")):
        if path.name.endswith(".stubs.o"):
            continue
        try:
            calls, taken, data = object_refs(path, functions)
        except (OSError, ValueError, struct.error, IndexError):
            continue
        for who, got in {**calls}.items():
            callees.setdefault(who, set()).update(got)
        for who, got in taken.items():
            callees.setdefault(who, set()).update(got)
        for who, got in data.items():
            if who in functions:
                refs.setdefault(who, set()).update(got)
    for who, got in callees.items():
        for target in got:
            (callers if who in functions else tables).setdefault(target, set()).add(who)
    return {
        name: {
            "callers": sorted(callers.get(name, ()))[:MAX_LINKS],
            "callees": sorted(callees.get(name, ()))[:MAX_LINKS],
            "refs": sorted(refs.get(name, ()))[:MAX_LINKS],
            "tables": sorted(tables.get(name, ()))[:MAX_LINKS],
        }
        for name in functions
    }


# --- behaviour, claims, movies ---


def claim_index(path: Path | None, names: set[str]) -> dict[str, list[str]]:
    try:
        text = path.read_text(errors="replace") if path else ""
    except OSError:
        return {}
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        for word in set(line.split()) & names:
            got = out.setdefault(word, [])
            if len(got) < MAX_CLAIMS:
                got.append(line.strip())
    return out


def decode_case(name: str, signature: dict[str, Any] | None, case: dict[str, Any]) -> dict[str, Any] | None:
    """Decode the observed o32 values, never infer a missing side or compare scratch files."""
    from abu_decomp.behave import prototype

    if not isinstance(case.get("input"), dict) or not isinstance(case.get("original"), dict):
        return None
    if signature and signature.get("source") != "inferred":
        proto = prototype.Prototype(
            name,
            str(signature.get("returns", "")),
            prototype.classify(str(signature.get("returns", ""))),
            tuple(prototype.Param(p["name"], p["type"], p["kind"]) for p in signature.get("params", [])),
            bool(signature.get("variadic")),
            str(signature.get("source", "inferred")),
        )
    else:
        proto = prototype.inferred(name)
    given = case["input"]
    registers, stack = given.get("registers", {}), given.get("stack", [])

    def number(raw: Any) -> int | None:
        try:
            return int(raw, 0) if isinstance(raw, str) else int(raw) if isinstance(raw, int) else None
        except ValueError:
            return None

    def value(kind: str, words: list[Any]) -> str:
        numbers = [number(word) for word in words]
        if not numbers or any(n is None for n in numbers):
            return "?"
        bits = 64 if kind in ("int64", "uint64", "double") else 32
        raw = numbers[0] if len(numbers) == 1 else ((numbers[0] & 0xFFFFFFFF) << 32) | (numbers[1] & 0xFFFFFFFF)
        raw &= (1 << bits) - 1
        if kind in ("float", "double"):
            return repr(struct.unpack(">f" if bits == 32 else ">d", raw.to_bytes(bits // 8, "big"))[0])
        if kind.startswith(("int", "uint")):
            width = int(kind.removeprefix("uint").removeprefix("int"))
            raw &= (1 << width) - 1
            if kind.startswith("int") and raw >= 1 << (width - 1):
                raw -= 1 << width
            return str(raw)
        return f"0x{raw:0{bits // 4}x}"

    arguments = []
    for slot in prototype.argument_slots(proto):
        words = []
        for place in slot.where:
            if place.startswith("stack:"):
                index = (int(place[6:]) - 16) // 4
                words.append(stack[index] if index < len(stack) else None)
            else:
                native = f"r{4 + int(place[1:])}" if place.startswith("a") else place
                words.append(registers.get(place, registers.get(native)))
        arguments.append(
            {
                "name": slot.param.name,
                "where": ":".join(slot.where),
                "kind": slot.param.kind,
                "value": value(slot.param.kind, words),
            }
        )

    def output(side: dict[str, Any] | None) -> dict[str, Any] | None:
        if side is None:
            return None
        status = side.get("status", "unknown")
        ret = side.get("ret") or {}
        where = (
            ("f0",)
            if proto.ret in ("float", "double")
            else ("v0", "v1")
            if proto.ret in ("int64", "uint64")
            else ("v0",)
        )
        returned = (
            "void" if proto.ret == "void" else f"{':'.join(where)}={value(proto.ret, [ret.get(r) for r in where])}"
        )
        return {"status": status, "return": returned if status == "ok" else None, "detail": side.get("detail")}

    original, candidate = output(case["original"]), output(case.get("candidate"))
    verdict = case.get("verdict", "untestable")
    oracle_ok = original["status"] == "ok"
    port_ok = (
        None if candidate is None or verdict == "untestable" else candidate["status"] == "ok" and verdict == "same"
    )
    call = f"{name}({', '.join(a['where'] + '=' + a['value'] for a in arguments)})"
    original_text = original["return"] or original["status"]
    if candidate is None:
        returned = f"original {original_text}; port not run"
    elif oracle_ok and port_ok and original["return"] == candidate["return"]:
        returned = original_text
    else:
        returned = f"original {original_text}; port {candidate['return'] or candidate['status']}"
    return {
        "text": f"{call} -> {returned}",
        "port": port_ok,
        "oracle": oracle_ok,
        "verdict": verdict,
        "arguments": arguments,
        "outputs": {"oracle": original, "port": candidate},
        "signature_known": not proto.blind,
        "reason": case.get("reason"),
        "difference": case.get("difference"),
    }


def behave_rows(inputs: Inputs, names: set[str]) -> dict[str, list[dict[str, Any]]]:
    """The page consumes case rows directly, folding identical observations within each recorded run."""
    index = tas_fn.behave_index(inputs.invocations)
    out = {}
    for name, records in index.items():
        if name not in names:
            continue
        shown = []
        for record in records[:MAX_BEHAVE]:
            folded: dict[bytes, dict[str, Any]] = {}
            for case in record.get("cases_detail") or []:
                row = decode_case(name, record.get("prototype"), case)
                if row is None:
                    continue
                key = dump(row)
                if key in folded:
                    folded[key]["count"] += 1
                elif len(folded) < MAX_CASES:
                    folded[key] = dict(row, count=1, invocation=record.get("invocation"), mode=record.get("mode"))
            shown.extend(folded.values())
        out[name] = shown
    return out


def reached_by(table: dict[str, Any]) -> dict[str, list[str]]:
    """function name -> movies whose newest run reached it, in table order."""
    functions = table.get("functions") or []
    sets, _ = tas_fn.movie_covered(table)
    out: dict[str, list[str]] = {}
    for run, members in zip(table.get("runs") or [], sets, strict=False):
        for i in members:
            if i < len(functions):
                out.setdefault(functions[i]["name"], []).append(run["movie"])
    return out


def fn_files(inputs: Inputs, files: list[str], rows: list[list[Any]], parts: dict[str, Any]) -> dict[str, bytes]:
    """name -> the fn/NAME.json body for every function row."""
    table = inputs.table
    cov = {r["name"]: r for r in table.get("functions", []) if r.get("in_version")}
    names = {r[0] for r in rows}
    movies = reached_by(table)
    claims = claim_index(inputs.claims, names)
    behave = behave_rows(inputs, names)
    src, gut, xref = parts["source"], parts["gutter"], parts["xref"]
    out = {}
    for name, rom, size, state, file_ix, reach, _foundation in rows:
        c = cov.get(name) or {}
        s = src.get(name) or {}
        x = xref.get(name) or {}
        reached = movies.get(name, [])
        out[name] = dump(
            {
                "name": name,
                "file": s.get("file") or files[file_ix],
                "first": s.get("first"),
                "rom": rom,
                "size": size,
                "state": state,
                "reach": reach,
                "behaviour": c.get("behaviour"),
                "port": c.get("port"),
                "oracle": c.get("oracle"),
                "html": s.get("html") or [],
                "lines": gut.get(name) or [],
                "callers": x.get("callers", []),
                "callees": x.get("callees", []),
                "refs": x.get("refs", []),
                "tables": x.get("tables", []),
                "movies": reached[:MAX_MOVIES],
                "movies_total": len(reached),
                "claims": claims.get(name, []),
                "behave": behave.get(name, []),
            }
        )
    return out


def summary_of(inputs: Inputs) -> dict[str, Any]:
    table = inputs.table
    s = table.get("summary") or {}
    phase = tas_phasemap.Phase(inputs.profile, inputs.cfg, inputs.pin, lambda: table)
    body, status = phase.phase()
    body = body if status == 200 and isinstance(body, dict) else {}
    try:
        built = inputs.cfg["port"].stat().st_mtime
    except OSError:
        built = 0
    return {
        "version": s.get("version"),
        "generated": table.get("generated"),
        "since": table.get("since"),
        "runs": len(table.get("runs") or []),
        "game": s.get("game"),
        "sdk": s.get("sdk"),
        "behaviour": s.get("behaviour"),
        "states": s.get("states"),
        "gcovr": table.get("gcovr"),
        "movies": s.get("movies"),
        "stage": body.get("stage"),
        "next_stage": body.get("next_stage"),
        "build": {"port": str(inputs.cfg["port"]), "pin": inputs.sha(), "built": built, "format": port_format(inputs)},
    }


def port_format(inputs: Inputs) -> int | None:
    try:
        value = json.loads((inputs.port_source / "port.json").read_text()).get("format") if inputs.port_source else None
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# --- movies ---


def newest_runs(cfg: dict[str, Any]) -> dict[str, list[tuple[Path, dict[str, Any]]]]:
    """movie -> its finished runs, newest first."""
    out: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for run, row in reversed(tas.run_rows(cfg)):
        if row.get("status") == "done" and row.get("movie"):
            out.setdefault(row["movie"], []).append((run, row))
    return out


def movie_row(
    cfg: dict[str, Any], path: Path, runs: list[tuple[Path, dict[str, Any]]], standing: dict[str, Any]
) -> list[Any]:
    """[name, polls, outcome, first-issue fn, last_run, kind, newest run] (the rows of Server.movies)."""
    name = path.stem
    last = next((r for _, r in runs if r.get("outcome") and not r.get("stopped") and not r.get("taken")), None)
    known = last or standing.get(name) or {}
    try:
        _, polls = tas.decode(path.read_text())
    except (OSError, DecompError, ValueError):
        polls = []
    fn = last.get("fn") if last else tas.fn_of(known)
    return [
        name,
        len(polls),
        known.get("outcome") or "",
        fn,
        last.get("ended") if last else None,
        known.get("kind") or None,
        runs[0][0].name if runs else None,
    ]


def poster(runs: list[tuple[Path, dict[str, Any]]]) -> bytes | None:
    """The last picture of the movie's newest run that kept one, as a small JPEG."""
    from PIL import Image

    from abu_decomp import tas_serve

    for run, _ in runs[:6]:
        try:
            png, _seq = tas_serve.picture((run / tas.VIEW).read_bytes(), "JPEG")
        except (OSError, ValueError):
            continue
        with Image.open(io.BytesIO(png)) as image:
            out = io.BytesIO()
            image.convert("RGB").save(out, "JPEG", quality=POSTER_QUALITY, optimize=True)
        return out.getvalue()
    return None


def reach_bits(
    inputs: Inputs, movie: str, runs: list[tuple[Path, dict[str, Any]]], index: dict[str, int]
) -> bytes | None:
    """The bitset over fns.json rows the movie reaches: the run's own RUN/reach.bin (the live reach stream) when it
    has the right size, else the coverage table's `covered` indexes for the movie."""
    size = (len(index) + 7) // 8
    for run, _ in runs[:3]:
        try:
            body = (run / "reach.bin").read_bytes()
        except OSError:
            continue
        if len(body) == size:
            return body
    table = inputs.table
    functions = table.get("functions") or []
    sets, _ = tas_fn.movie_covered(table)
    for run_row, members in zip(table.get("runs") or [], sets, strict=False):
        if run_row["movie"] != movie:
            continue
        bits = bytearray(size)
        for i in members:
            at = index.get(functions[i]["name"]) if i < len(functions) else None
            if at is not None:
                bits[at >> 3] |= 1 << (at & 7)
        return bytes(bits)
    return None


def bake_movies(inputs: Inputs, only: str | None, index: dict[str, int]) -> dict[str, Any]:
    """Posters, reach files and movies.json; `only` bakes one movie and keeps the other rows."""
    site = inputs.site
    try:
        rows = {r[0]: r for r in json.loads((site / "movies.json").read_text())["rows"]} if only else {}
    except (OSError, ValueError, KeyError):
        rows = {}
    try:
        standing = json.loads((inputs.runs / "standing.json").read_text())
    except (OSError, ValueError):
        standing = {}
    runs_of = newest_runs(inputs.cfg)
    movies = inputs.cfg["movies"]
    paths = {p.stem: p for p in sorted(movies.glob("*.tas"))} if movies.is_dir() else {}
    if only and only not in paths:
        raise DecompError("tas.bake", f"no saved movie {only!r} to bake")
    count = {"posters": 0, "reach": 0}
    for name, path in paths.items():
        if only and name != only:
            continue
        runs = runs_of.get(name, [])
        rows[name] = movie_row(inputs.cfg, path, runs, standing)
        picture = poster(runs)
        if picture and put(site / "posters" / f"{name}.jpg", picture, compress=False):
            count["posters"] += 1
        bits = reach_bits(inputs, name, runs, index) if index else None
        if bits is not None and put(site / "reach" / f"{name}.bin", bits, compress=False):
            count["reach"] += 1
    ordered = [rows[n] for n in sorted(rows) if n in paths]
    put(
        site / "movies.json",
        dump({"cols": ["name", "polls", "outcome", "fn", "last_run", "kind", "run"], "rows": ordered}),
    )
    return {"movies": len(ordered), **count}


# --- the bake ---


def _index_of(site: Path) -> dict[str, int]:
    """name -> fns.json row of the baked site (from its manifest), {} before the first bake."""
    try:
        manifest = json.loads((site / "manifest.json").read_text())
        names = (site / manifest["fnrows"]).read_text().split("\n")
    except (OSError, ValueError, KeyError):
        return {}
    return {n: i for i, n in enumerate(names) if n}


def bake(
    profile: Any, what: set[str] | None = None, movie: str | None = None, union: Path | None = None
) -> dict[str, Any]:
    """Bake `what` (None: everything) into the site; the result counts what was written."""
    unknown = (what or set()) - set(WHAT)
    if unknown:
        raise DecompError("tas.bake", f"--what takes {', '.join(WHAT)}; not {', '.join(sorted(unknown))}")
    if movie and what and what != {"movie"}:
        raise DecompError("tas.bake", "--movie bakes one movie: use it with --what movie")
    started = time.monotonic()
    with slots.hold("batch", 1, "tas bake"):
        inputs = Inputs(profile, union)
        with locked(inputs.runs):
            result = _bake(inputs, what, movie)
    result["seconds"] = round(time.monotonic() - started, 2)
    return result


def _bake(inputs: Inputs, what: set[str] | None, movie: str | None) -> dict[str, Any]:
    site, runs = inputs.site, inputs.runs
    want = set(WHAT) if what is None else what
    index = _index_of(site)
    if want == {"movie"}:
        if index:
            return {**bake_movies(inputs, movie, index), "wrote": ["movie"]}
        want = set(WHAT)  # no baked site to add a movie to yet: bake all of it
    if not inputs.table.get("functions"):
        raise DecompError("tas.bake", "no coverage table yet: run `decomp tas coverage` first")
    body = inputs.map_body()
    files, rows = function_rows(body, inputs.table)
    names = {r[0] for r in rows}
    source = None if "source" in want else part_read(runs, "source")
    xref = None if "xref" in want else part_read(runs, "xref")
    gut = None if want & {"source", "coverage"} else part_read(runs, "gutter")
    union = read_union(inputs.union) if source is None or gut is None else None
    if source is None:
        source = slices(inputs, rows, files, union)
        part_write(runs, "source", source)
    if xref is None:
        xref = xrefs(inputs, names)
        part_write(runs, "xref", xref)
    if gut is None:
        gut = gutters(source, union)
        part_write(runs, "gutter", gut)
    fns_body = dump(
        {
            "version": body["version"],
            "ref": body["ref"],
            "files": files,
            "cols": ["name", "rom", "size", "state", "file", "reach", "foundation"],
            "rows": rows,
        }
    )
    rows_body = ("\n".join(r[0] for r in rows) + "\n").encode()
    fns_name, rows_name = f"fns.{short(fns_body)}.json", f"fnrows.{short(rows_body)}.txt"
    put(site / fns_name, fns_body)
    put(site / rows_name, rows_body)
    put(site / "summary.json", dump(summary_of(inputs)))
    written = 0
    for name, text in fn_files(inputs, files, rows, {"source": source, "gutter": gut, "xref": xref}).items():
        written += put(site / "fn" / f"{name}.json", text)
    movies = bake_movies(inputs, None, {r[0]: i for i, r in enumerate(rows)}) if "movie" in want else {}
    for stale in [p for p in (site / "fn").iterdir() if p.name.endswith(".json") and p.name[:-5] not in names]:
        stale.unlink()
        stale.with_name(stale.name + ".gz").unlink(missing_ok=True)
    keep = {fns_name, rows_name}
    for old in [*site.glob("fns.*.json*"), *site.glob("fnrows.*.txt*")]:
        if old.name.removesuffix(".gz") not in keep:
            old.unlink()
    manifest = {
        "version": MANIFEST_VERSION,
        "generated": time.time(),
        "fns": fns_name,
        "fnrows": rows_name,
        "summary": "summary.json",
        "movies": "movies.json",
        "fn": {"dir": "fn/", "ext": ".json", "names": rows_name, "count": len(rows)},
        "gutter": bool(gut),
        "ref": body["ref"],
    }
    put(site / "manifest.json", dump(manifest))
    return {
        "fns": len(rows),
        "fn_written": written,
        "source_functions": len(source),
        "xref_functions": sum(1 for v in xref.values() if v["callers"] or v["callees"]),
        "gutter_functions": len(gut),
        **movies,
        "wrote": sorted(want),
    }


def movie_hook(profile: Any, name: str | None) -> None:
    """The end of `tas play`: bake the movie's poster, reach file and movies.json row. A bake that cannot run never
    fails the play; it says why on stderr and the next full bake covers it."""
    if not name:
        return
    try:
        bake(profile, {"movie"}, name)
    except Exception as error:
        print(f"tas bake --what movie --movie {name}: {error}", file=sys.stderr)
