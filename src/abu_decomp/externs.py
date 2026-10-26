"""`decomp cleanup owners` without `--dir`: hoist the unit-local `extern` declarations into their owning headers.

Every source unit may redeclare a global its own way; the tree should hold ONE extern per global, in the header of the
module that owns it. The plan is computed, never claimed:
- the owner of an id is the module of the unit that DEFINES it (the majority module when twin units define it); for a
  symbol no C unit defines, the module of the unit whose address range holds the symbol's address (the hex number its
  name ends in, against the map's optional `start`/`end` columns), else the module of its majority user;
- the prototype is the definition's, else the declarations' agreed evidence; an id whose declarations differ is
  REFUSED and listed with its conflicting prototypes, never guessed;
- an id is also REFUSED when a unit that sees the destination header (every includer, and every unit that gets the
  include) spells the name as something else (a static, a type, a macro), or when no header exists for the owner;
- the destination header gets exactly one extern per id, every unit-local redeclaration is deleted, and a unit that
  no longer sees the destination gets its `#include`.
The moves are emitted as batches (`externs-batch-NN.diff`, each over the tree the earlier ones leave) that the train
applies like any tool batch: no file cap, one stamp per batch. A batch's stamp rebuilds every unit that sees a header it
changes, so a batch is filled until its estimated cost (recompiled units x `unit_cpu`) reaches `batch_cpu`, which
`externs.json` records per batch beside the refusals and the unit-extern counts before and after. When the project's
data config names a census, a batch is also run through the stamp gate and every id it refuses is refused here."""

from __future__ import annotations

import csv
import difflib
import json
import re
import subprocess
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from abu_decomp import DecompError, deaddecls, owners
from abu_decomp.data import config as data_config
from abu_decomp.data import gate as data_gate

HEADER_SUFFIXES = (".h", ".hpp", ".hh")
INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.M)
ADDRESS = re.compile(r"(?:^|_)([0-9A-Fa-f]{6,8})$")
TYPEDEF = re.compile(r"\btypedef\b[^;{}]*?(?:\(\s*\*\s*(\w+)\s*\)[^;{}]*|\b(\w+)\s*(?:\[[^\]]*\])*\s*);|\}\s*(\w+)\s*;")
DEFINED = re.compile(r"^\s*#\s*define\s+(\w+)", re.M)
TAG = re.compile(r"\b(?:struct|union|enum)\s+(\w+)\s*[{;]")
USED_TAG = re.compile(r"\b(?:struct|union|enum)\s+(\w+)")
WORD = re.compile(r"[A-Za-z_]\w*")
BATCH_CPU = 300.0  # estimated CPU s per batch: half of the 600 s a header-changing stamp may spend (CARD-RULES rule 3)
UNIT_CPU = 0.25  # estimated CPU s a stamp spends per recompiled unit (calibrated by the recorded `stamp build: cpu=`)


@dataclass
class Decl:
    name: str
    proto: str
    unit: str
    first: int
    last: int


@dataclass
class Scan:
    externs: dict[str, list[Decl]] = field(default_factory=lambda: defaultdict(list))  # unit-local, by id
    in_headers: dict[str, dict[str, set[str]]] = field(default_factory=lambda: defaultdict(dict))  # id -> hdr -> protos
    definitions: dict[str, list[tuple[str, str]]] = field(default_factory=lambda: defaultdict(list))  # (unit, proto)
    compound: set[str] = field(default_factory=set)  # ids declared together with others: not movable line-wise
    local_names: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # file -> names it spells itself
    includes: dict[str, list[str]] = field(default_factory=dict)  # file -> raw include targets
    texts: dict[str, str] = field(default_factory=dict)
    types: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # type/macro -> declaring files
    tags: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # struct/union/enum tag -> files


def tracked(root: Path) -> list[str]:
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True)
    return sorted(n for n in listed.stdout.decode().split("\0") if n)


def proto_of(text: str) -> str:
    """The normalised declaration: no `extern`, no trailing `;`, whitespace collapsed."""
    flat = re.sub(r"\s+", " ", text.strip()).removeprefix("extern ").rstrip(";").strip()
    return re.sub(r"\s*([(),\[\]*])\s*", r"\1", flat).replace(",", ", ")


def definition_proto(text: str) -> str:
    head = re.split(r"\{|=", text.strip(), maxsplit=1)[0]
    return proto_of(re.sub(r"^(?:static|inline)\s+", "", head.strip()).rstrip(";"))


def scan(root: Path, files: list[str]) -> Scan:
    found = Scan()
    for name in files:
        low = name.lower()
        is_header = low.endswith(HEADER_SUFFIXES)
        if not (is_header or (name.startswith("src/") and low.endswith(owners.SOURCE_SUFFIXES))):
            continue
        raw = (root / name).read_bytes().decode("latin-1")
        found.texts[name] = raw
        text = deaddecls.strip_comments(raw)
        found.includes[name] = INCLUDE.findall(text)
        for m in TYPEDEF.finditer(text):
            found.types[m.group(1) or m.group(2) or m.group(3)].add(name)
        for m in DEFINED.finditer(text):
            found.types[m.group(1)].add(name)
        for m in TAG.finditer(text):
            found.tags[m.group(1)].add(name)
        for c in deaddecls.split(text):
            deaddecls.analyse(c)
            body = c.text.strip()
            if c.opaque:
                m = owners.DEFINE.match(body)
                if m:
                    found.local_names[name].add(m.group(1))
                continue
            if re.match(r"extern\b", body) and "{" not in body:
                if len(c.defs) != 1:
                    found.compound |= c.defs
                    continue
                (ident,) = c.defs
                if is_header:
                    found.in_headers[ident].setdefault(name, set()).add(proto_of(body))
                else:
                    found.externs[ident].append(Decl(ident, proto_of(body), name, c.first, c.last))
            elif is_header and owners.kind_of(c) == "value" and not owners.is_definition(c) and len(c.defs) == 1:
                found.in_headers[next(iter(c.defs))].setdefault(name, set()).add(proto_of(body))  # a bare prototype
            elif not is_header and owners.is_definition(c) and not re.match(r"static\b", body):
                for ident in c.defs:
                    found.definitions[ident].append((name, definition_proto(body)))
            else:
                found.local_names[name] |= c.defs
    return found


def unseen_types(found: Scan, ident: str, proto: str, visible: set[str]) -> list[str]:
    """The types and macros a prototype spells that the tree declares but the destination header cannot see: a
    declaration hoisted there would not parse in the units that include it (the unit it came from saw them)."""
    tagged = set(USED_TAG.findall(proto))
    missing = [t for t in tagged if found.tags.get(t) and not found.tags[t] & visible]
    bare = set(WORD.findall(USED_TAG.sub("", proto))) - {ident}
    missing += [t for t in bare if found.types.get(t) and not found.types[t] & visible]
    return sorted(missing)


def resolve(index: dict[str, list[str]], including: str, target: str) -> str | None:
    cands = index.get(target.lower(), [])
    if not cands:
        return None
    here = str(Path(including).parent)
    return next((c for c in cands if str(Path(c).parent) == here), cands[0])


def closures(found: Scan, files: list[str]) -> dict[str, set[str]]:
    """Every header each file sees through its includes (transitively)."""
    index: dict[str, list[str]] = defaultdict(list)
    for h in files:
        if h.lower().endswith(HEADER_SUFFIXES):
            parts = h.lower().split("/")
            for i in range(len(parts)):
                index["/".join(parts[i:])].append(h)
    direct = {
        n: [r for t in targets if (r := resolve(index, n, t.lower().replace("\\", "/")))]
        for n, targets in found.includes.items()
    }
    seen: dict[str, set[str]] = {}

    def sees(name: str, stack: frozenset[str] = frozenset()) -> set[str]:
        if name in seen:
            return seen[name]
        out: set[str] = set()
        for h in direct.get(name, ()):
            out.add(h)
            if h not in stack:
                out |= sees(h, stack | {name})
        if not stack:
            seen[name] = out
        return out

    return {n: sees(n) for n in found.includes}


def address_ranges(path: Path) -> list[tuple[int, int, str]]:
    """(start, end, unit) of the units the module map gives an address range (`start`/`end` hex columns)."""
    out: list[tuple[int, int, str]] = []
    with open(path, encoding="utf-8", newline="") as handle:
        lines = (line for line in handle if line.strip() and not line.startswith("#"))
        for row in csv.DictReader(lines, delimiter="\t"):
            try:
                out.append((int(row["start"], 16), int(row["end"], 16), row["unit"]))
            except (KeyError, TypeError, ValueError):
                continue
    return sorted(out)


def range_owner(name: str, ranges: list[tuple[int, int, str]]) -> str | None:
    m = ADDRESS.search(name)
    if not m:
        return None
    addr = int(m.group(1), 16)
    return next((unit for start, end, unit in ranges if start <= addr < end), None)


def majority(units: list[str], unit_module: dict[str, str]) -> str:
    count = Counter(unit_module[u] for u in units if u in unit_module)
    return sorted(count.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if count else ""


def plan(
    root: Path,
    map_path: Path | None = None,
    packs: Path | None = None,
    batch_cpu: float = BATCH_CPU,
    unit_cpu: float = UNIT_CPU,
    gate: Callable[[str], list[str]] | None = None,
) -> dict[str, object]:
    map_path = map_path or root / "docs" / "MODULES.tsv"
    unit_module, module_header = owners.read_map(map_path)
    ranges = address_ranges(map_path)
    files = tracked(root)
    found = scan(root, files)
    sees = closures(found, files)
    includers: dict[str, set[str]] = defaultdict(set)
    for n, hs in sees.items():
        for h in hs:
            includers[h].add(n)
    spelled: dict[str, set[str]] = defaultdict(set)  # id -> files that spell it as something other than an extern
    for n, names in found.local_names.items():
        for ident in names & found.externs.keys():
            spelled[ident].add(n)

    moves: list[dict[str, object]] = []
    refused: list[dict[str, object]] = []
    for ident, decls in sorted(found.externs.items()):
        users = sorted({d.unit for d in decls})
        defined = found.definitions.get(ident, [])
        if ident in found.compound:
            refused.append({"id": ident, "reason": "declared together with other names", "protos": []})
            continue
        if defined:
            definers = sorted({u for u, _ in defined})
            owner_unit, reason = majority(definers, unit_module), "definer"
            owner = owner_unit
            protos = sorted({p for u, p in defined if unit_module.get(u) == owner} or {defined[0][1]})
            if len(protos) > 1:
                refused.append({"id": ident, "reason": "definitions differ", "protos": protos})
                continue
        else:
            at = range_owner(ident, ranges)
            owner = unit_module.get(at, "") if at else ""
            reason = "address range"
            if not owner:
                owner, reason = majority([d.unit for d in decls], unit_module), "majority user"
            held = found.in_headers.get(ident, {})
            headed = {p for ps in held.values() for p in ps}
            # a unit that already sees a header declaring the name compiles with both, so its text is no evidence
            blind = [d for d in decls if not held.keys() & sees.get(d.unit, set())]
            evidence = {d.proto for d in (blind if headed else decls)}
            protos = sorted(evidence | headed)
            if len(protos) > 1:
                refused.append({"id": ident, "reason": "declarations conflict", "protos": protos})
                continue
        dest = module_header.get(owner, "")
        if not owner or not dest or dest not in found.texts:
            refused.append({"id": ident, "reason": f"no header for {owner or 'any module'}", "protos": protos})
            continue
        held = found.in_headers.get(ident, {})
        if held and dest not in held:
            dest = sorted(held)[0]  # already declared once in a header: that one stays the owner
            reason += ", declared in header"
        absent = unseen_types(found, ident, protos[0], sees.get(dest, set()) | {dest})
        if absent:
            why = "type not visible from " + dest + ": " + ", ".join(absent[:4])
            refused.append({"id": ident, "reason": why, "protos": protos})
            continue
        seeing = includers[dest] | set(users)
        clash = sorted(n for n in spelled.get(ident, ()) if n in seeing and n not in {u for u, _ in defined})
        clash += sorted(
            h
            for h in found.local_names
            if h in sees
            and ident in found.local_names[h]
            and h.lower().endswith(HEADER_SUFFIXES)
            and includers[h] & seeing
        )
        if clash:
            refused.append({"id": ident, "reason": "name clashes in " + ", ".join(clash[:4]), "protos": protos})
            continue
        moves.append(
            {
                "id": ident,
                "owner": owner,
                "to": dest,
                "reason": reason,
                "proto": protos[0],
                "has_decl": dest in held,
                "decls": decls,
                "units": users,
            }
        )
    unattributed: list[str] = []
    while True:
        sees_dest = {m["to"]: includers[str(m["to"])] for m in moves}
        batches = fill(moves, sees_dest, batch_cpu, unit_cpu)
        refusing = refused_by_gate(found, batches, sees, unit_cpu, gate, unattributed) if gate else {}
        if not refusing:
            break
        for m in moves:
            if m["id"] in refusing:
                refused.append({"id": m["id"], "reason": "gate: " + refusing[str(m["id"])], "protos": [m["proto"]]})
        moves = [m for m in moves if m["id"] not in refusing]
    lines_before = sum(len(v) for v in found.externs.values())
    result: dict[str, object] = {
        "unit_extern_lines_before": lines_before,
        "unit_extern_ids_before": len(found.externs),
        "unit_extern_lines_after": lines_before - sum(len(m["decls"]) for m in moves),  # type: ignore[arg-type]
        "unit_extern_ids_after": len(found.externs) - len(moves),
        "planned_ids": len(moves),
        "refused_ids": len(refused),
        "batches": [],
        "refused": refused,
        "gate_unattributed": unattributed,
    }
    if packs is not None:
        packs.mkdir(parents=True, exist_ok=True)
        result["batches"] = write_batches(found, batches, packs, sees, unit_cpu)[0]
        (packs / "externs.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
        with open(packs / "externs.tsv", "w", encoding="utf-8", newline="") as handle:
            w = csv.writer(handle, delimiter="\t", lineterminator="\n")
            w.writerow(["id", "owner", "to", "reason", "proto", "units"])
            for m in moves:
                row = [m["id"], m["owner"], m["to"], m["reason"], m["proto"], len(m["units"])]
                w.writerow(row)  # type: ignore[arg-type]
    return result


def stamp_gate(root: Path) -> Callable[[str], list[str]] | None:
    """The project's stamp gate as a diff checker, when its data config names a census: a batch the gate would refuse at
    stamp time is refused at plan time, id by id."""
    try:
        cfg = data_config.load(None, root)
    except DecompError:
        return None
    census = Path(cfg.census).expanduser() if cfg.census else None
    if census is None or not census.is_file():
        return None
    return lambda text: data_gate.check_diff(cfg, census, text)


def refused_by_gate(
    found: Scan,
    batches: list[list[dict[str, object]]],
    sees: dict[str, set[str]],
    unit_cpu: float,
    gate: Callable[[str], list[str]],
    unattributed: list[str],
) -> dict[str, str]:
    """The ids whose batch the project's stamp gate would refuse, each with the violation that names it; a
    violation that names none of its batch's ids is kept in `unattributed` (the plan cannot drop it)."""
    refusing: dict[str, str] = {}
    diffs = write_batches(found, batches, None, sees, unit_cpu)[1]
    for batch, text in zip(batches, diffs, strict=True):
        for violation in gate(text):
            named = [str(m["id"]) for m in batch if re.search(r"\b" + re.escape(str(m["id"])) + r"\b", violation)]
            for ident in named:
                refusing.setdefault(ident, violation[:240])
            if not named and violation not in unattributed:
                unattributed.append(violation)
    return refusing


def fill(
    moves: list[dict[str, object]], sees_dest: dict[object, set[str]], batch_cpu: float, unit_cpu: float
) -> list[list[dict[str, object]]]:
    """Group the moves by destination header, then pack groups (biggest first) while the estimated cost of the batch
    (units recompiled: those that see a changed header plus the units edited) stays under `batch_cpu`. A header whose
    includers alone exceed it gets a batch of its own, and says so."""
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for m in moves:
        groups[str(m["to"])].append(m)
    batches: list[list[dict[str, object]]] = []
    recompiled: list[set[str]] = []
    for dest, group in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        units = set(sees_dest[dest]) | {u for m in group for u in m["units"]}  # type: ignore[attr-defined]
        for i, used in enumerate(recompiled):
            if len(used | units) * unit_cpu <= batch_cpu:
                batches[i] += group
                used |= units
                break
        else:
            batches.append(list(group))
            recompiled.append(set(units))
    for batch, used in zip(batches, recompiled, strict=True):
        for m in batch:
            m["batch_units"] = len(used)
    return batches


def rows(text: str) -> list[str]:
    """The lines of a file, split on newlines only (the splitter the declaration scan numbers lines by)."""
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def eol(lines: list[str]) -> str:
    return "\r\n" if sum(ln.endswith("\r\n") for ln in lines) * 2 > len(lines) else "\n"


def locate(current: list[str], wanted: list[str], hint: int) -> int:
    n = len(wanted)
    spots = [i for i in range(len(current) - n + 1) if current[i : i + n] == wanted]
    return min(spots, key=lambda i: abs(i - hint)) if spots else -1


def write_batches(
    found: Scan, batches: list[list[dict[str, object]]], packs: Path | None, sees: dict[str, set[str]], unit_cpu: float
) -> tuple[list[dict[str, object]], list[str]]:
    """One diff per batch over the tree the earlier ones leave: declarations leave their units, one extern per id joins
    the destination header before its closing `#endif`, a unit that no longer sees the destination
    includes it. Returns the batch records and the diff texts; the files are written only when `packs` is given."""
    texts = {n: rows(t) for n, t in found.texts.items()}
    seen = {n: set(h) for n, h in sees.items()}
    written: list[dict[str, object]] = []
    diffs: list[str] = []
    for number, batch in enumerate(batches, 1):
        before: dict[str, list[str]] = {}
        removals: dict[str, list[tuple[int, int]]] = defaultdict(list)
        adds: dict[str, list[str]] = defaultdict(list)
        wants: dict[str, set[str]] = defaultdict(set)
        for m in batch:
            dest = str(m["to"])
            for d in m["decls"]:  # type: ignore[attr-defined]
                cur = texts[d.unit]
                spot = locate(cur, rows(found.texts[d.unit])[d.first : d.last + 1], d.first)
                if spot < 0:
                    continue
                removals[d.unit].append((spot, spot + d.last - d.first + 1))
                if dest not in seen[d.unit]:
                    wants[d.unit].add(dest)
            if not m["has_decl"]:
                adds[dest].append(f"extern {m['proto']};" + eol(texts[dest]))
        edited = set(removals) | set(adds)
        for n in edited:
            before[n] = list(texts[n])
        for n, spans in removals.items():
            cur = texts[n]
            drop = {i for a, b in spans for i in range(a, b)}
            last_inc = max((i for i, ln in enumerate(cur) if re.match(r"\s*#\s*include\b", ln)), default=-1)
            out: list[str] = []
            for i, ln in enumerate(cur):
                if i not in drop:
                    out.append(ln)
                if i == last_inc:
                    out += [f'#include "{owners.include_path(h)}"' + eol(cur) for h in sorted(wants[n])]
            if last_inc < 0:
                out = [f'#include "{owners.include_path(h)}"' + eol(cur) for h in sorted(wants[n])] + out
            texts[n] = out
            for h in wants[n]:
                seen[n] |= {h, *seen.get(h, ())}
        for dest, new in adds.items():
            cur = texts[dest]
            end = max((i for i, ln in enumerate(cur) if ln.lstrip().startswith("#endif")), default=len(cur))
            if end == len(cur) and cur and not cur[-1].endswith("\n"):
                cur[-1] += eol(cur)
            texts[dest] = [*cur[:end], *new, *cur[end:]]
        diff = [f"# MSG: Hoist {len(batch)} unit externs into their owning headers (batch {number})\n"]
        for n in sorted(edited):
            diff += [f"diff --git a/{n} b/{n}\n", *difflib.unified_diff(before[n], texts[n], f"a/{n}", f"b/{n}")]
        name = f"externs-batch-{number:02d}.diff"
        diffs.append("".join(diff))
        if packs is not None:
            (packs / name).write_text(diffs[-1], encoding="latin-1")
        units = batch[0].get("batch_units", 0)
        written.append(
            {
                "batch": name,
                "ids": len(batch),
                "files": len(edited),
                "destinations": sorted({str(m["to"]) for m in batch}),
                "lines_removed": sum(len(m["decls"]) for m in batch),  # type: ignore[arg-type]
                "recompiled_units": units,
                "estimated_cpu": round(float(units) * unit_cpu, 1) if isinstance(units, int) else 0.0,
            }
        )
    return written, diffs
