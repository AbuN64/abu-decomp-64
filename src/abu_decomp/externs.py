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
import json
import re
import subprocess
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from abu_decomp import DecompError, deaddecls, owners
from abu_decomp.data import config as data_config
from abu_decomp.data import gate as data_gate

HEADER_SUFFIXES = (".h", ".hpp", ".hh")
# a source file may macro a keyword (`#define char x` in a shim): the language owns these, no header provides them
C_KEYWORDS = frozenset(
    {
        *("char", "short", "int", "long", "float", "double", "signed", "unsigned", "void", "const", "volatile"),
        *("register", "static", "extern", "_Bool", "bool"),
    }
)
INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.M)
ADDRESS = re.compile(r"(?:^|_)([0-9A-Fa-f]{6,8})$")
TYPEDEF = re.compile(r"\btypedef\b[^;{}]*?(?:\(\s*\*\s*(\w+)\s*\)[^;{}]*|\b(\w+)\s*(?:\[[^\]]*\])*\s*);|\}\s*(\w+)\s*;")
DEFINED = re.compile(r"^\s*#\s*define\s+(\w+)", re.M)
TAG = re.compile(r"\b(?:struct|union|enum)\s+(\w+)\s*[{;]")
USED_TAG = re.compile(r"\b(?:struct|union|enum)\s+(\w+)")
WORD = re.compile(r"[A-Za-z_]\w*")
ALIAS = re.compile(r"^[ \t]*typedef[ \t]+((?:[A-Za-z_]\w*[ \t]+)*[A-Za-z_]\w*)[ \t]+(\w+)[ \t]*;", re.M)
BASE_WORDS = {"int", "char", "short", "long", "float", "double", "void", "unsigned", "signed", "const", "volatile"}
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
    compound_in: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # file -> its compound ids
    local_names: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # file -> names it spells itself
    plain_includes: dict[str, list[str]] = field(default_factory=dict)  # file -> include targets under no `#if`
    texts: dict[str, str] = field(default_factory=dict)
    types: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # type/macro -> declaring files
    tags: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # struct/union/enum tag -> files
    aliases: dict[str, str] = field(default_factory=dict)  # `typedef <words> NAME;` -> the words (unambiguous ones)
    originals: dict[str, str] = field(default_factory=dict)  # unit -> its text before the multi-declarator split
    created: set[str] = field(default_factory=set)  # headers the plan creates (their text is in `texts`)
    needs: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # created header -> includes it needs


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


def unconditional_includes(text: str, is_header: bool) -> list[str]:
    """The `#include` targets every build of the file reads: none under an `#if`/`#ifdef`/`#ifndef` (a header's own
    include guard excepted). An include under a condition does not make the header visible to the other builds."""
    out: list[str] = []
    depth, first = 0, True
    for ln in text.splitlines():
        word = re.match(r"\s*#\s*(\w+)", ln)
        if not word:
            continue
        kind = word.group(1)
        if kind.startswith("if") and not (first and is_header and kind == "ifndef"):
            depth += 1
        elif kind == "endif" and depth:
            depth -= 1
        elif kind == "include" and depth == 0 and (m := INCLUDE.match(ln)):
            out.append(m.group(1))
        first = False
    return out


def scan(root: Path, files: list[str], texts: dict[str, str] | None = None) -> Scan:
    found = Scan()
    clash: set[str] = set()
    for name in files:
        low = name.lower()
        is_header = low.endswith(HEADER_SUFFIXES)
        if not (is_header or (name.startswith("src/") and low.endswith(owners.SOURCE_SUFFIXES))):
            continue
        raw = texts[name] if texts and name in texts else (root / name).read_bytes().decode("latin-1")
        found.texts[name] = raw
        text = deaddecls.strip_comments(raw)
        for m in ALIAS.finditer(text):
            base = " ".join(m.group(1).split())
            if base.split()[0] in ("struct", "union", "enum", "typedef", "extern", "static"):
                continue
            if found.aliases.setdefault(m.group(2), base) != base:
                clash.add(m.group(2))
        found.plain_includes[name] = unconditional_includes(text, is_header)
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
                    found.compound_in[name] |= c.defs
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
    for name in clash:
        found.aliases.pop(name, None)
    return found


def top_commas(text: str) -> list[str]:
    """TEXT cut at the commas outside any bracket."""
    parts, level, cut = [], 0, 0
    for i, ch in enumerate(text):
        level += (ch in "([{") - (ch in ")]}")
        if ch == "," and level == 0:
            parts.append(text[cut:i])
            cut = i + 1
    return [*parts, text[cut:]]


# a declarator is a name with its array dimensions or its parameter list (no nesting: a function pointer is not one)
DECLARED_SUFFIX = r"((?:\[[^\]]*\]\s*)*|\([^()]*\))"
DECLARED_FIRST = r"^(.*?[\s*])([A-Za-z_]\w*)\s*" + DECLARED_SUFFIX + "$"
DECLARED_NEXT = r"^(\*[\s*]*)?([A-Za-z_]\w*)\s*" + DECLARED_SUFFIX + "$"


def tight(suffix: str) -> str:
    """Array dimensions lose their spaces; a parameter list keeps the text as written."""
    return suffix if suffix.startswith("(") else suffix.replace(" ", "")


def split_declaration(body: str) -> list[str] | None:
    """`extern char *a, b[2];` as one declaration per name (`extern char *a;`, `extern char b[2];`); None when the text
    is not a plain list of declarators (an initialiser, a function or function-pointer declarator, a body)."""
    flat = " ".join(body.strip().rstrip(";").split())
    if not flat.startswith("extern ") or re.search(r"[={}#]|/[*/]", flat):
        return None
    parts = [p.strip() for p in top_commas(flat)]
    if len(parts) < 2:
        return None
    first = re.match(DECLARED_FIRST, parts[0])
    if not first or first.group(1).strip() == "extern":
        return None
    head = first.group(1).rstrip("* ")
    stars = first.group(1)[len(head) :].replace(" ", "")
    out = [f"{head} {stars}{first.group(2)}{tight(first.group(3))};"]
    for part in parts[1:]:
        m = re.match(DECLARED_NEXT, part)
        if not m:
            return None
        out.append(f"{head} {(m.group(1) or '').replace(' ', '')}{m.group(2)}{tight(m.group(3))};")
    return out


def split_compound(root: Path, files: list[str]) -> dict[str, str]:
    """The source units whose multi-declarator `extern` lines are rewritten to one declaration per name (unit -> new
    text). Only whole-line declarations without comments or preprocessor lines are rewritten."""
    changed: dict[str, str] = {}
    for name in files:
        if not (name.startswith("src/") and name.lower().endswith(owners.SOURCE_SUFFIXES)):
            continue
        raw = (root / name).read_bytes().decode("latin-1")
        if not re.search(r"\bextern\b[^;{}()=]*,", raw):
            continue
        lines = rows(raw)
        edits: list[tuple[int, int, list[str]]] = []
        for c in deaddecls.split(deaddecls.strip_comments(raw)):
            body = c.text.strip()
            if not re.match(r"extern\b", body) or "{" in body or c.opaque:
                continue
            span = "".join(lines[c.first : c.last + 1])
            pieces = split_declaration(body)
            if not pieces or re.search(r"/[*/]|^\s*#", span, re.M):
                continue
            if " ".join(span.split()) != " ".join(body.split()):
                continue  # other text shares the lines
            edits.append((c.first, c.last, pieces))
        if edits:
            nl = eol(lines)
            for first, last, pieces in sorted(edits, reverse=True):
                lines[first : last + 1] = [p + nl for p in pieces]
            changed[name] = "".join(lines)
    return changed


def canonical(proto: str, aliases: dict[str, str]) -> str:
    """The declaration with its typedef aliases spelled out and its parameter names dropped: two spellings of one
    type compare equal."""
    out = proto
    for _ in range(4):
        spelled = re.sub(r"[A-Za-z_]\w*", lambda m: aliases.get(m.group(0), m.group(0)), out)
        if spelled == out:
            break
        out = spelled
    if out.endswith(")") and "(" in out:
        head, _, params = out.partition("(")

        def bare(param: str) -> str:
            words = WORD.findall(param)
            if len(words) >= 2 and words[-1] not in BASE_WORDS and words[-2] not in ("struct", "union", "enum"):
                param = re.sub(r"\b" + words[-1] + r"\b(?!.*\b" + words[-1] + r"\b)", "", param)
            return param.strip()

        out = head + "(" + ", ".join(bare(p) for p in top_commas(params[:-1])) + ")"
    return re.sub(r"\s+", " ", out).strip()


def header_rules(module_header: dict[str, str]) -> Callable[[str], str]:
    """Learn how the map spells a module's header from the modules that have one (the module path and the header path
    share their trailing components: `src/game/MENU/TRACK.CPP` -> `include/MENU/TRACK.H` is the rule
    `src/game/` -> `include/` and `.H`), and return the function that spells it for a module that has none ('' when
    no learned rule fits)."""
    votes: Counter[tuple[tuple[str, ...], tuple[str, ...], str]] = Counter()
    for module, header in module_header.items():
        m, h = PurePosixPath(module), PurePosixPath(header)
        mp, hp = list(m.with_suffix("").parts), list(h.with_suffix("").parts)
        while mp and hp and mp[-1] == hp[-1]:
            mp.pop()
            hp.pop()
        votes[tuple(mp), tuple(hp), h.suffix] += 1

    def derive(module: str) -> str:
        parts = list(PurePosixPath(module).with_suffix("").parts)
        fits = [
            (len(mp), n, hp, ext)
            for (mp, hp, ext), n in votes.items()
            if len(parts) > len(mp) and tuple(parts[: len(mp)]) == mp
        ]
        if not fits:
            return ""
        k, _, hp, ext = max(fits)
        return str(PurePosixPath(*hp, *parts[k:]).with_suffix(ext))

    return derive


def new_header(path: str, includes: list[str]) -> str:
    guard = re.sub(r"\W", "_", owners.include_path(str(PurePosixPath(path).with_suffix("")))).upper() + "_H"
    body = "".join(f'#include "{owners.include_path(h)}"\n' for h in includes)
    return f"#ifndef {guard}\n#define {guard}\n{body}#endif\n"


def unseen_types(found: Scan, ident: str, proto: str, visible: set[str]) -> list[str]:
    """The types and macros a prototype spells that the tree declares but the destination header cannot see: a
    declaration hoisted there would not parse in the units that include it (the unit it came from saw them)."""
    tagged = set(USED_TAG.findall(proto))
    missing = [t for t in tagged if found.tags.get(t) and not found.tags[t] & visible]
    bare = set(WORD.findall(USED_TAG.sub("", proto))) - {ident} - C_KEYWORDS
    missing += [t for t in bare if found.types.get(t) and not found.types[t] & visible]
    return sorted(missing)


def resolve(index: dict[str, list[str]], including: str, target: str) -> str | None:
    cands = index.get(target.lower(), [])
    if not cands:
        return None
    here = str(Path(including).parent)
    return next((c for c in cands if str(Path(c).parent) == here), cands[0])


def closures(found: Scan, files: list[str]) -> dict[str, set[str]]:
    """Every header each file sees in every build, through its unconditional includes (transitively)."""
    index: dict[str, list[str]] = defaultdict(list)
    for h in files:
        if h.lower().endswith(HEADER_SUFFIXES):
            parts = h.lower().split("/")
            for i in range(len(parts)):
                index["/".join(parts[i:])].append(h)
    direct = {
        n: [r for t in targets if (r := resolve(index, n, t.lower().replace("\\", "/")))]
        for n, targets in found.plain_includes.items()
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

    return {n: sees(n) for n in found.plain_includes}


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


def newly_seen_clash(
    found: Scan, sees: dict[str, set[str]], unit: str, dest: str, header_ids: dict[str, dict[str, set[str]]]
) -> list[str]:
    """The names a unit would meet when it gets `#include dest`: every header of dest's closure the unit does not see
    yet comes with its declarations, and the unit's own spelling of such a name (a compound extern, a static, a type, an
    extern with another prototype) no longer compiles beside them. Returns `header:name` for each."""
    gained = ({dest} | sees.get(dest, set())) - sees.get(unit, set()) - {unit}
    mine: dict[str, set[str]] = defaultdict(set)  # name -> the unit's extern prototypes ({} = not an extern)
    for ident in found.externs.keys() & {n for h in gained for n in header_ids.get(h, {})}:
        mine[ident] |= {d.proto for d in found.externs[ident] if d.unit == unit}
    own = found.local_names.get(unit, set()) | found.compound_in.get(unit, set())
    out = []
    for h in sorted(gained):
        for ident, protos in header_ids.get(h, {}).items():
            if ident in own or (mine.get(ident) and mine[ident] - protos):
                out.append(f"{h}:{ident}")
    return out


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
    for module in set(unit_module.values()):  # a module's own source file (the merged unit) belongs to that module
        unit_module.setdefault(module, module)
    split = split_compound(root, files)
    found = scan(root, files, split)
    found.originals = {n: (root / n).read_bytes().decode("latin-1") for n in split}
    derive = header_rules(module_header)
    sees = closures(found, files)
    includers: dict[str, set[str]] = defaultdict(set)
    for n, hs in sees.items():
        for h in hs:
            includers[h].add(n)
    header_ids: dict[str, dict[str, set[str]]] = defaultdict(dict)  # header -> the names it declares -> prototypes
    for ident, held in found.in_headers.items():
        for h, protos in held.items():
            header_ids[h][ident] = protos
    for h, names in list(found.local_names.items()) + list(found.compound_in.items()):
        if h.lower().endswith(HEADER_SUFFIXES):
            for ident in names:
                header_ids[h].setdefault(ident, set())
    reach: dict[tuple[str, str], list[str]] = {}
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
            stated = arity(protos[0])
            odd = sorted({d.proto for d in decls if stated is not None and arity(d.proto) not in (None, stated)})
            if odd:  # callers written against it pass another number of arguments than the definition
                why = "declaration parameter count differs from the definition"
                refused.append({"id": ident, "reason": why, "protos": [*protos, *odd]})
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
                spellings: dict[str, list[str]] = defaultdict(list)
                for p in protos:
                    spellings[canonical(p, found.aliases)].append(p)
                if len(spellings) > 1:
                    refused.append({"id": ident, "reason": "declarations conflict", "protos": protos})
                    continue
                # one type under several spellings: the header's, else the most used, else the first
                (same,) = spellings.values()
                votes = Counter(d.proto for d in decls)
                protos = [min(same, key=lambda p: (p not in headed, -votes[p], p))]
        dest = module_header.get(owner, "") or (derive(owner) if owner else "")
        if not owner or not dest:
            refused.append({"id": ident, "reason": f"no header for {owner or 'any module'}", "protos": protos})
            continue
        if dest not in found.texts:  # the module has none: the plan creates it, with the includes its types need
            found.texts[dest] = new_header(dest, [])
            found.created.add(dest)
            sees.setdefault(dest, set())
        held = found.in_headers.get(ident, {})
        if held and dest not in held:
            dest = sorted(held)[0]  # already declared once in a header: that one stays the owner
            reason += ", declared in header"
        absent = unseen_types(found, ident, protos[0], sees.get(dest, set()) | {dest})
        if absent and dest in found.created:
            provide = {
                t: sorted(
                    h
                    for h in found.types.get(t, set()) | found.tags.get(t, set())
                    if h.lower().endswith(HEADER_SUFFIXES) and h != dest
                )
                for t in absent
            }
            if all(provide.values()):
                for t in absent:
                    found.needs[dest].add(provide[t][0])
                    sees[dest] |= {provide[t][0], *sees.get(provide[t][0], ())}
                found.texts[dest] = new_header(dest, sorted(found.needs[dest]))
                absent = unseen_types(found, ident, protos[0], sees[dest] | {dest})
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
        for u in users:
            if dest not in sees.get(u, set()):
                if (u, dest) not in reach:
                    reach[u, dest] = newly_seen_clash(found, sees, u, dest, header_ids)
                clash += reach[u, dest]
        if clash:
            refused.append({"id": ident, "reason": "include clashes in " + ", ".join(clash[:3]), "protos": protos})
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
                "definers": sorted({u for u, _ in defined}),
                "units": users,
            }
        )
    unattributed: list[str] = []
    while True:
        sees_dest = {m["to"]: includers[str(m["to"])] for m in moves}
        batches = fill(moves, sees_dest, batch_cpu, unit_cpu, frozenset(found.created))
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
        "split_units": len(found.originals),
        "created_headers": sorted({str(m["to"]) for m in moves} & found.created),
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
    for batch, text in zip(batches, diffs[len(diffs) - len(batches) :], strict=True):
        for violation in gate(text):
            named = [str(m["id"]) for m in batch if re.search(r"\b" + re.escape(str(m["id"])) + r"\b", violation)]
            for ident in named:
                refusing.setdefault(ident, violation[:240])
            if not named and violation not in unattributed:
                unattributed.append(violation)
    return refusing


def fill(
    moves: list[dict[str, object]],
    sees_dest: dict[object, set[str]],
    batch_cpu: float,
    unit_cpu: float,
    created: frozenset[str] = frozenset(),
) -> list[list[dict[str, object]]]:
    """Group the moves that edit a common file (destination header, unit, definer of a created header), then pack the
    groups (biggest first) while the estimated cost of the batch (units recompiled: those that see a changed header
    plus the units edited) stays under `batch_cpu`. The batches share no file, so the train takes them in one round and
    each diff applies to main by itself. A group whose cost alone exceeds the cap gets a batch of its own."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for m in moves:
        dest = str(m["to"])
        files = {dest, *(d.unit for d in m["decls"])}  # type: ignore[attr-defined]
        if dest in created:
            files |= set(m["definers"])  # type: ignore[arg-type]
        for f in files:
            parent[find(f)] = find(dest)
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for m in moves:
        groups[find(str(m["to"]))].append(m)
    dests = {key: {str(m["to"]) for m in group} for key, group in groups.items()}
    batches: list[list[dict[str, object]]] = []
    recompiled: list[set[str]] = []
    for dest, group in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        units = {u for d in dests[dest] for u in sees_dest[d]} | {u for m in group for u in m["units"]}  # type: ignore[attr-defined]
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


def include_anchor(lines: list[str], first: int) -> int:
    """The line a new include goes after: the last unconditional `#include` before the first removed declaration, so
    every use of the hoisted names still follows it (-1: none, the include goes first)."""
    depth, anchor = 0, -1
    for i, ln in enumerate(lines[:first]):
        word = re.match(r"\s*#\s*(\w+)", ln)
        if not word:
            continue
        if word.group(1).startswith("if"):
            depth += 1
        elif word.group(1) == "endif":
            depth = max(depth - 1, 0)
        elif word.group(1) == "include" and depth == 0:
            anchor = i
    return anchor


def arity(proto: str) -> int | None:
    """The parameter count a function prototype states (None: not a function, `()` or variadic, which state none)."""
    if not proto.endswith(")"):
        return None
    depth, start = 0, len(proto)
    for i in range(len(proto) - 1, -1, -1):
        depth += (proto[i] == ")") - (proto[i] == "(")
        if depth == 0:
            start = i
            break
    inner, parts, level, cut = proto[start + 1 : -1], [], 0, 0
    for i, ch in enumerate(inner):
        level += (ch in "([") - (ch in ")]")
        if ch == "," and level == 0:
            parts.append(inner[cut:i])
            cut = i + 1
    parts.append(inner[cut:])
    parts = [p.strip() for p in parts]
    if parts == [""] or "..." in parts:
        return None
    return 0 if parts == ["void"] else len(parts)


def git_diff(name: str, old: list[str], new: list[str]) -> str:
    """The section git itself would print for NAME going from OLD to NEW: the train compares each landed file's added
    and removed line counts with the queued ones, and another alignment (difflib's) counts blank lines differently."""
    pair = (f"a/{name}", f"b/{name}")
    with tempfile.TemporaryDirectory() as tmp:
        for side, lines in (("a", old), ("b", new)):
            target = Path(tmp, side, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes("".join(lines).encode("latin-1"))
        run = subprocess.run(
            ["git", "diff", "--no-index", "--no-color", "--no-ext-diff", "--src-prefix=", "--dst-prefix=", *pair],
            cwd=tmp,
            capture_output=True,
        )
    if run.returncode not in (0, 1):
        raise DecompError(f"git diff of {name} failed: {run.stderr.decode(errors='replace').strip()}")
    return re.sub(r"^index [^\n]*\n", "", run.stdout.decode("latin-1"), flags=re.M)


def write_batches(
    found: Scan, batches: list[list[dict[str, object]]], packs: Path | None, sees: dict[str, set[str]], unit_cpu: float
) -> tuple[list[dict[str, object]], list[str]]:
    """One diff per batch over main (the batches share no file): declarations leave their units, one extern per id joins
    the destination header before its closing `#endif`, a unit that no longer sees the destination
    includes it. Returns the batch records and the diff texts; the files are written only when `packs` is given."""
    texts = {n: rows(t) for n, t in found.texts.items()}
    seen: dict[str, set[str]] = defaultdict(set, {n: set(h) for n, h in sees.items()})
    written: list[dict[str, object]] = []
    diffs: list[str] = []
    fresh = set(found.created)
    touched: set[str] = set()
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
            if dest in found.created:  # the definer states the header's declaration too: it is checked against it
                for u in m.get("definers", ()):  # type: ignore[attr-defined]
                    if dest not in seen[u]:
                        wants[u].add(dest)
        edited = set(removals) | set(adds) | {n for n, w in wants.items() if w}
        touched |= edited
        for n in edited:
            before[n] = [] if n in fresh else rows(found.originals[n]) if n in found.originals else list(texts[n])
        for n in sorted(set(removals) | {n for n, w in wants.items() if w}):
            cur, spans = texts[n], removals.get(n, [])
            drop = {i for a, b in spans for i in range(a, b)}
            last_inc = include_anchor(cur, min((a for a, _ in spans), default=len(cur)))
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
            if n in fresh:
                fresh.discard(n)
                body = texts[n]
                diff.append(
                    f"diff --git a/{n} b/{n}\nnew file mode 100644\n--- /dev/null\n+++ b/{n}\n"
                    f"@@ -0,0 +1,{len(body)} @@\n"
                    + "".join("+" + ln if ln.endswith("\n") else "+" + ln + "\n" for ln in body)
                )
            else:
                diff.append(git_diff(n, before[n], texts[n]))
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
    lone = {n: t for n, t in found.originals.items() if n not in touched}
    if lone:  # batch 0: the units whose multi-declarator lines no move batch edits are split on their own
        split = [f"# MSG: Split multi-declarator unit externs into one declaration per name ({len(lone)} units)\n"]
        split += [git_diff(n, rows(t), rows(found.texts[n])) for n, t in sorted(lone.items())]
        diffs.insert(0, "".join(split))
        if packs is not None:
            (packs / "externs-batch-00-split.diff").write_text(diffs[0], encoding="latin-1")
        written.insert(
            0,
            {
                "batch": "externs-batch-00-split.diff",
                "ids": 0,
                "files": len(lone),
                "destinations": [],
                "lines_removed": 0,
                "recompiled_units": len(lone),
                "estimated_cpu": round(len(lone) * unit_cpu, 1),
            },
        )
    return written, diffs
