"""Which units a header edit cannot reach: the data check rebuilds an includer only when the edit can change its compile key.

`decomp data check` used to preprocess every includer of a touched header (3400 units over five versions for a header
many units include: 171 CPU s) and compile those whose key moved. The build's compile key (`hdrkey`, unbake's Makefile)
is the preprocessed unit with every top-level declaration the unit never names, transitively, left out. So an edit
that only changes droppable declarations (extern variables and prototypes, typedefs, struct/union/enum definitions)
changes a unit's key only if the unit's key names one of the names those declarations define:

  key(U) is the closure, from the unit's definitions, over the names the kept items spell. An item the key does not
  define-and-name stays out of the closure. If no name the changed items define (before or after the edit) occurs in the
  old key of U, then no kept item defined a changed item and no kept item names a changed item's definition, so the
  closure over the new text is the same set of items with the same text: the key is equal and the object is too.

The proof holds only when (1) the header's changed region holds nothing but droppable declarations, parsed here from
the raw text with no preprocessor line beyond the include guard and unchanged includes; (2) no name involved is a
macro anywhere (a macro could rename the declaration inside the unit); (3) the unit's old key is the key of the
state the header diff starts from: the unit's `.i.ok` is not newer than the tree's extraction marker, so no build after
the pool minted the tree has touched it. Anything else keeps the unit (never a skip on doubt)."""

from __future__ import annotations

import difflib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

KEYWORDS = frozenset(
    [
        "auto",
        "break",
        "case",
        "char",
        "const",
        "continue",
        "default",
        "do",
        "double",
        "else",
        "enum",
        "extern",
        "float",
        "for",
        "goto",
        "if",
        "int",
        "long",
        "register",
        "return",
        "short",
        "signed",
        "sizeof",
        "static",
        "struct",
        "switch",
        "typedef",
        "union",
        "unsigned",
        "void",
        "volatile",
        "while",
        "inline",
        "__inline",
        "__inline__",
        "__volatile__",
        "__const",
        "__const__",
        "__signed__",
        "asm",
        "__asm",
        "__asm__",
        "__attribute__",
        "__attribute",
        "__extension__",
        "__typeof__",
        "typeof",
        "restrict",
        "__restrict",
        "_Bool",
    ]
)
_TOKEN = re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|[A-Za-z_]\w*|\d\w*|\.\.\.|->|\S')
_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_IDENT = re.compile(r"[A-Za-z_]\w*$")


@dataclass
class Parsed:
    items: list[tuple[str, ...]] = field(default_factory=list)
    why: str = ""  # non-empty: the text is outside what this parser proves anything about


def _blank(m: re.Match[str]) -> str:
    return re.sub(r"[^\n]", " ", m.group(0))


def parse(text: str) -> Parsed:
    """The includes and the top-level items of a header's raw text. `why` names the first thing it does not handle."""
    text = _COMMENT.sub(_blank, text.replace("\\\n", "  "))
    out = Parsed()
    code: list[str] = []
    guard: list[str] = []
    for line in text.split("\n"):
        s = line.strip()
        if not s.startswith("#"):
            code.append(line)
            continue
        m = re.match(r'#\s*include\s+(["<][^">]+[">])\s*$', s)
        if m:  # an include is an item in its place: moving an item across it, or changing it, is not analysable
            code.append('__INC__ "' + m.group(1).strip('"<>') + '" ;')
            continue
        m = re.match(r"#\s*(ifndef|define|endif)\b\s*(\w*)\s*$", s)
        if m and not guard and m.group(1) == "ifndef" and m.group(2):
            guard = [m.group(2)]
            continue
        if m and len(guard) == 1 and m.group(1) == "define" and m.group(2) == guard[0]:
            guard.append("def")
            continue
        if m and m.group(1) == "endif" and len(guard) == 2:
            guard.append("end")
            continue
        out.why = f"preprocessor line {s[:40]!r}"
        return out
    if guard and len(guard) != 3:
        out.why = "unbalanced include guard"
        return out
    toks = _TOKEN.findall("\n".join(code))
    item: list[str] = []
    depth = {"(": 0, "[": 0, "{": 0}
    pair = {")": "(", "]": "[", "}": "{"}
    for t in toks:
        if t in depth:
            depth[t] += 1
        elif t in pair:
            depth[pair[t]] -= 1
            if depth[pair[t]] < 0:
                out.why = "unbalanced brackets"
                return out
        item.append(t)
        if t == ";" and not any(depth.values()):
            if item[:-1]:
                out.items.append(tuple(item[:-1]))
            item = []
        elif t == "}" and not any(depth.values()) and item[0] not in ("extern", "typedef", "struct", "union", "enum"):
            out.items.append(tuple(item))  # a function body or an initialiser: ends at its brace
            item = []
    if item or any(depth.values()):
        out.why = "text after the last item"
    return out


def _idents(tokens) -> set[str]:
    return {t for t in tokens if _IDENT.match(t) and t not in KEYWORDS}


def _depth0(t: tuple[str, ...], stop: str):
    d = 0
    for i, x in enumerate(t):
        if x in "([{":
            if x == stop and d == 0:
                return i
            d += 1
        elif x in ")]}":
            d -= 1
        elif x == stop and d == 0:
            return i
    return -1


def defs(item: tuple[str, ...]) -> set[str] | None:
    """The names a droppable top-level item defines (a superset is always safe); None when the item is not droppable by
    hdrkey's rule or not understood (a definition, an `extern "C"`, a struct with declarators)."""
    if not item:
        return None
    head = item[0]
    if head == "extern":
        if len(item) < 2 or item[1].startswith('"') or "{" in item:
            return None
        if _depth0(item, ",") >= 0:
            return _idents(item)
        p = _depth0(item, "(")
        if p > 0:
            return {item[p - 1]} if _IDENT.match(item[p - 1]) and item[p - 1] not in KEYWORDS else _idents(item)
        cut = min((i for i in (_depth0(item, "["), _depth0(item, "=")) if i >= 0), default=len(item))
        name = item[cut - 1]
        return {name} if _IDENT.match(name) and name not in KEYWORDS else _idents(item)
    body = _depth0(item, "{")
    if head == "typedef":
        if body >= 0:
            close = _match(item, body)
            tag = item[body - 1] if body >= 2 and item[body - 1] not in KEYWORDS else ""
            rest = item[close + 1 :]
            if "(" in rest or close < 0:
                return _idents(item)
            names = _idents(rest) | ({tag} if tag else set())
            return names | (_idents(item[body + 1 : close]) if item[1] == "enum" else set())
        if "(" in item or _depth0(item, ",") >= 0:
            return _idents(item)
        cut = _depth0(item, "[")
        name = item[(cut if cut >= 0 else len(item)) - 1]
        return {name} if _IDENT.match(name) and name not in KEYWORDS else _idents(item)
    if head in ("struct", "union", "enum"):
        if body < 0:
            return {item[1]} if len(item) == 2 and _IDENT.match(item[1]) else None
        close = _match(item, body)
        if close < 0 or close != len(item) - 1:
            return None  # declarators after the body: a variable definition, kept always
        tag = item[1] if body == 2 and item[1] not in KEYWORDS else ""
        if head != "enum" and not tag:
            return None
        return ({tag} if tag else set()) | (_idents(item[body + 1 : close]) if head == "enum" else set())
    return None


def _match(item: tuple[str, ...], open_at: int) -> int:
    d = 0
    for i in range(open_at, len(item)):
        d += item[i] == "{"
        d -= item[i] == "}"
        if d == 0:
            return i
    return -1


def changed_defs(old: str | None, new: str | None) -> tuple[set[str] | None, str]:
    """(names defined by the items the edit removed or added, "") or (None, why the edit is not analysable)."""
    a, b = parse(old or ""), parse(new or "")
    if a.why or b.why:
        return None, a.why or b.why
    names: set[str] = set()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a.items, b=b.items, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        for it in (*a.items[i1:i2], *b.items[j1:j2]):
            d = defs(it)
            if d is None:
                return None, f"changed item is not a droppable declaration: {' '.join(it[:6])}"
            names |= d
    return names, ""


_DEFINE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)", re.M)


def macro_hits(tree: Path, names: set[str], dirs=("include", "src"), flags: list[str] | None = None) -> set[str]:
    """NAMES that are `#define`d (or -D) anywhere under DIRS: a macro could rename the declaration inside a unit."""
    hit = {f[2:].split("=")[0] for f in flags or [] if f.startswith("-D")} & names
    if not names:
        return hit
    cmd = ["grep", "-rhoE", r"^[ \t]*#[ \t]*define[ \t]+[A-Za-z_][A-Za-z_0-9]*", *dirs]
    out = subprocess.run(cmd, cwd=tree, capture_output=True, text=True).stdout
    return hit | {m.group(1) for m in _DEFINE.finditer(out)} & names


def unit_key(tree: Path, obj_i: Path, hdrkey: Path, fill: bool = True) -> str | None:
    """The compile key of a unit's preprocessed `.i` (the `.i.key` make keeps when it is newer than the `.i`; else hdrkey
    computes it and, with FILL, leaves it there as make would). None when no key can be had."""
    k = obj_i.parent / (obj_i.name + ".key")
    try:
        if k.is_file() and k.stat().st_mtime >= obj_i.stat().st_mtime:
            return k.read_text(errors="replace")
        if not hdrkey.is_file():
            return None
        r = subprocess.run([str(hdrkey), str(obj_i)], capture_output=True, text=True)
        if r.returncode or not r.stdout:
            return None
        if fill:
            tmp = k.with_name(k.name + ".new")
            tmp.write_text(r.stdout)
            tmp.replace(k)
        return r.stdout
    except OSError:
        return None


def narrow(
    tree: Path,
    version: str,
    headers: list[str],
    units: list[str],
    object_fmt: str,
    flags: list[str] | None = None,
    budget: int = 100000,
) -> tuple[list[str], dict]:
    """(UNITS to keep, stats): drops the units that no edit of HEADERS can reach (module doc). `object_fmt` is the config's
    object path template; BUDGET bounds the keys computed on the fly (hdrkey takes ~1 ms a unit; the rest are kept)."""
    stats = {"headers": len(headers), "includers": len(units), "skipped": 0, "why": ""}
    for marker in (
        tree / "build/.pool-built-at",
        tree / "build/.pool-extracted-at",
    ):  # the pool's build ends after its extraction
        try:
            base = marker.read_text().strip()
            since = marker.stat().st_mtime
            break
        except OSError:
            continue
    else:
        stats["why"] = "no pool marker: tree not minted from a pool"
        return units, stats
    git = ["git", "-C", str(tree)]
    if subprocess.run([*git, "cat-file", "-e", base + "^{commit}"], capture_output=True).returncode:
        stats["why"] = "mint commit unknown"
        return units, stats
    names: set[str] = set()
    for h in headers:
        if not h.lower().endswith(".h"):
            stats["why"] = f"{h} is not a header"
            return units, stats
        r = subprocess.run([*git, "show", f"{base}:{h}"], capture_output=True, text=True, errors="replace")
        old = r.stdout if r.returncode == 0 else None
        try:
            new = (tree / h).read_text(errors="replace")
        except OSError:
            new = None
        got, why = changed_defs(old, new)
        if got is None:
            stats["why"] = f"{h}: {why}"
            return units, stats
        names |= got
    if not names:
        stats["why"] = "no declaration changed"
    mac = macro_hits(tree, names, flags=flags)
    if mac:
        stats["why"] = f"{sorted(mac)[0]} is a macro"
        return units, stats
    if not names:
        keep: list[str] = []
        stats["skipped"] = len(units)
        return keep, stats
    pat = re.compile(r"(?<![\w$])(?:" + "|".join(map(re.escape, sorted(names, key=len, reverse=True))) + r")(?![\w$])")
    hdrkey = tree / "build/hdrkey"
    keep = []
    spent = 0
    for u in units:
        unit_i = tree / (object_fmt.format(v=version, unit=u)[: -len(".o")] + ".i")
        try:
            pristine = (unit_i.parent / (unit_i.name + ".ok")).stat().st_mtime <= since
        except OSError:
            pristine = False
        key = None
        if pristine:
            have = (unit_i.parent / (unit_i.name + ".key")).is_file()
            if have or spent < budget:
                key = unit_key(tree, unit_i, hdrkey)
                spent += 0 if have else 1
        if key is None or pat.search(key):
            keep.append(u)
        else:
            stats["skipped"] += 1
    stats["keys_computed"] = spent
    return keep, stats
