"""`decomp cleanup dead-decls`: the declarations of one folder that nothing reads, removed to a fixpoint as one batch per
module folder.
A header folder collects declarations (types, enum constants, externs, prototypes, inline functions) whose last use
went away in the cleanup. A name is live when a file outside the folder spells it, or when a live declaration of the
folder spells it (a type used only by an inline, an extern used only by another header); a preprocessor line or a
construct this reader does not recognize keeps every name it spells. Three cases decide the result and are tested:
- a declaration of several names (`extern int a, b;`, an enum with its constants) is live when ANY of its names is;
- a dead declaration used by another dead declaration goes in a later layer, so each layer leaves a tree that
  still compiles (layer 0 first, then layer 1 over it); a cycle of dead declarations is one layer;
- an enum constant used elsewhere keeps its enum.
Headers of the folder are one scope (a name is live when a file outside the folder spells it). Each source unit of the
folder is a scope of its own (unit-local): a typedef, tag, extern or prototype the unit never spells again is dead; a
definition (function, variable, initialized data) is never touched, so the objects stay byte-identical. A unit's
outside words are the preprocessor lines of every header it includes (a macro expands in the unit) and the full text of
any non-header file it includes; a source another file includes is not a unit and is left alone.
The run repeats passes (sources, then headers over the reduced sources) until a pass finds nothing: the layers of one
pass already cover every transitive user, the next pass covers a header name whose last user was a dead unit-local
declaration. `--apply` writes the whole fixpoint. `packs` gets one diff per module folder (`dead-decls-NN-<module>.diff`,
no file cap: the train applies a tool batch whole) and `dead-decls.json`; module folders whose dead declarations use
each other share one batch, and source batches come before header batches.
The reader is a declaration splitter over comment-free text (not a C parser): a chunk ends at `;` at brace depth 0, or
at the `}` of a function definition. It is conservative: when unsure a declaration is live."""

from __future__ import annotations

import difflib
import json
import posixpath
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

IDENT = re.compile(r"[A-Za-z_]\w*")
COMMENT = re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|/\*.*?\*/|//[^\n]*', re.S)
KEYWORDS = frozenset(
    [
        "struct",
        "union",
        "enum",
        "typedef",
        "extern",
        "static",
        "inline",
        "const",
        "volatile",
        "unsigned",
        "signed",
        "void",
        "char",
        "short",
        "int",
        "long",
        "float",
        "double",
        "register",
        "auto",
        "return",
        "sizeof",
        "if",
        "else",
        "for",
        "while",
        "do",
        "switch",
        "case",
        "default",
        "break",
        "continue",
        "goto",
    ]
)
C_SUFFIXES = (".h", ".hpp", ".inc")
SOURCE_SUFFIXES = (".c", ".cpp", ".cc")
HEADER_SUFFIXES = (".h", ".hpp", ".hh")
INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.M)
MAX_BYTES = 8 << 20
MAX_PASSES = 16


def strip_comments(text: str) -> str:
    """Comments blanked (newlines kept, so line numbers hold); string literals stay."""

    def blank(m: re.Match[str]) -> str:
        s = m.group(0)
        return s if s[0] in "\"'" else re.sub(r"[^\n]", " ", s)

    return COMMENT.sub(blank, text)


@dataclass
class Chunk:
    first: int  # first and last line (0-based, inclusive) of the declaration
    last: int
    text: str
    defs: set[str] = field(default_factory=set)
    refs: set[str] = field(default_factory=set)
    opaque: bool = False  # a directive or a construct not understood: every name it spells is live
    enforced: bool = False  # the compiler checks it (an array bound that fails to compile): a root, with what it spells


def _depth(line: str) -> int:
    clean = re.sub(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', "", line)
    return clean.count("{") - clean.count("}") + clean.count("(") - clean.count(")")


def split(stripped: str) -> list[Chunk]:
    lines = stripped.split("\n")
    out: list[Chunk] = []
    cur: list[str] = []
    start = depth = 0
    n = 0
    while n < len(lines):
        line = lines[n]
        if not cur:
            text = line.strip()
            if not text:
                n += 1
                continue
            if text.startswith("#"):
                end = n
                while lines[end].rstrip().endswith("\\") and end + 1 < len(lines):
                    end += 1
                out.append(Chunk(n, end, "\n".join(lines[n : end + 1]), opaque=True))
                n = end + 1
                continue
            if text == "}" or re.match(r'extern\s*"C"\s*\{', text):
                out.append(Chunk(n, n, line, opaque=True))
                n += 1
                continue
            start, depth = n, 0
        cur.append(line)
        depth += _depth(line)
        body = "\n".join(cur)
        tail = line.rstrip()
        head = body.split("{", 1)[0]
        function = not re.match(r"\s*(typedef|struct|union|enum)\b", head) and "=" not in head
        if depth <= 0 and (tail.endswith(";") or (tail.endswith("}") and function and "{" in body)):
            out.append(Chunk(start, n, body))
            cur, depth = [], 0
        n += 1
    if cur:
        out.append(Chunk(start, len(lines) - 1, "\n".join(cur), opaque=True))
    return out


def _segments(text: str) -> list[str]:
    """`text` split at commas that are not inside brackets."""
    parts, depth, last = [], 0, 0
    for i, ch in enumerate(text):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[last:i])
            last = i + 1
    parts.append(text[last:])
    return parts


def _declarator(segment: str) -> str | None:
    """The name one declarator introduces: `(*cb)` a function pointer, the word before `(` a function, else the last
    word before `[` or an initializer."""
    segment = segment.split("=", 1)[0]
    pointer = re.search(r"\(\s*\*+\s*(\w+)", segment)
    if pointer:
        return pointer.group(1)
    if "(" in segment:
        segment = segment.split("(", 1)[0]
    segment = segment.split("[", 1)[0]
    words = IDENT.findall(segment)
    words = [w for w in words if w not in KEYWORDS]
    return words[-1] if words else None


def _declared(text: str) -> set[str]:
    return {name for seg in _segments(text.strip().rstrip(";")) if (name := _declarator(seg))}


def _enforced(text: str) -> bool:
    """Whether a declaration holds a compile-time check: an array whose bound is a condition or a comparison
    (`typedef char T_size_check[(sizeof(T) == 8) ? 1 : -1];`), which fails the build when false and needs no user."""
    if "{" in text:
        return False
    return any(
        re.search(r"\?|[=!<>]=|[<>]", bound) for bound in re.findall(r"\[([^\[\]]*(?:\([^\]]*\)[^\[\]]*)*)\]", text)
    )


def analyse(chunk: Chunk) -> None:
    """Fill `defs` and `refs`; a chunk with no recognizable definition becomes opaque."""
    if chunk.opaque:
        return
    text = chunk.text
    if text.count("{") != text.count("}") or text.count("(") != text.count(")"):
        chunk.opaque = True  # a conditional body, a stray bracket: not a unit this reader can cut out
        return
    kind = re.match(r"\s*(typedef\s+)?(struct|union|enum)\b", text)
    defs: set[str] = set()
    if "{" in text and kind:
        open_at = text.index("{")
        depth, close = 0, -1
        for i in range(open_at, len(text)):
            depth += (text[i] == "{") - (text[i] == "}")
            if depth == 0:
                close = i
                break
        if close < 0:
            chunk.opaque = True
            return
        tag = IDENT.findall(text[:open_at])
        tag = [w for w in tag if w not in KEYWORDS]
        defs |= set(tag[:1])
        if kind.group(2) == "enum":
            for seg in _segments(text[open_at + 1 : close]):
                words = IDENT.findall(seg)
                if words:
                    defs.add(words[0])
        defs |= _declared(text[close + 1 :])
    elif "{" in text:  # a function definition: the word before its first `(`
        name = _declarator(text.split("{", 1)[0])
        if name:
            defs.add(name)
    elif kind:  # forward declaration, or a typedef of a tagged type
        words = [w for w in IDENT.findall(text) if w not in KEYWORDS]
        if re.match(r"\s*(struct|union|enum)\s+\w+\s*;\s*$", text):
            defs.add(words[0])
        else:
            defs |= _declared(re.sub(r"^\s*(typedef\s+)?(struct|union|enum)\s+\w+", "typedef int", text, count=1))
    else:
        defs |= _declared(re.sub(r"^\s*(typedef|extern)\b", "", text))
    if not defs:
        chunk.opaque = True
        return
    chunk.defs = defs
    chunk.refs = {w for w in IDENT.findall(text) if w not in KEYWORDS} - defs
    chunk.enforced = _enforced(text)


class Index:
    """The words every tracked file spells (comments blanked in C text), its `#include` lines and, for a header, the
    words of its preprocessor lines: what a scope reads of the files around it. `put` swaps in a reduced text."""

    def __init__(self, root: Path) -> None:
        self.words: dict[str, set[str]] = {}
        self.includes: dict[str, list[str]] = {}
        self.directives: dict[str, set[str]] = {}
        listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
        for name in listed.decode("utf-8", "replace").split("\0"):
            path = root / name
            if not name or not path.is_file() or path.stat().st_size > MAX_BYTES:
                continue
            try:
                self.put(name, path.read_bytes().decode("latin-1"))
            except OSError:
                continue
        lower: dict[str, list[str]] = {}
        for name in self.words:
            lower.setdefault(name.lower(), []).append(name)
        self.by_lower = {k: v[0] for k, v in lower.items() if len(v) == 1}
        base: dict[str, list[str]] = {}
        for name in self.words:
            base.setdefault(posixpath.basename(name).lower(), []).append(name)
        self.by_base = {k: v[0] for k, v in base.items() if len(v) == 1}

    def put(self, name: str, text: str) -> None:
        if "\0" in text:
            return
        if name.lower().endswith(C_SUFFIXES + SOURCE_SUFFIXES):
            text = strip_comments(text)
            self.includes[name] = INCLUDE.findall(text)
            if name.lower().endswith(HEADER_SUFFIXES):
                self.directives[name] = directive_words(text)
        self.words[name] = set(IDENT.findall(text))

    def resolve(self, includer: str, name: str) -> str | None:
        """The tracked file `#include "name"` of `includer` reads: next to it, from the root, under include/, else the
        one file of that name (case folded)."""
        here = posixpath.dirname(includer)
        for cand in (posixpath.normpath(posixpath.join(here, name)), name, "include/" + name):
            if cand in self.words:
                return cand
            if cand.lower() in self.by_lower:
                return self.by_lower[cand.lower()]
        return self.by_base.get(posixpath.basename(name).lower())

    def outside(self, exclude: set[str]) -> set[str]:
        """Every word of the tracked files but `exclude`: the names the project spells."""
        out: set[str] = set()
        for name, words in self.words.items():
            if name not in exclude:
                out |= words
        return out

    def unit_outside(self, unit: str) -> set[str]:
        """What a source unit reads of other files: the preprocessor lines of every header it includes (a macro
        expands in the unit) and the full text of any other file it includes."""
        out: set[str] = set()
        seen = {unit}
        stack = [unit]
        while stack:
            for name in self.includes.get(stack.pop(), ()):
                target = self.resolve(unit, name)
                if target and target not in seen:
                    seen.add(target)
                    stack.append(target)
                    out |= self.directives[target] if target in self.directives else self.words.get(target, set())
        return out

    def included_files(self) -> set[str]:
        return {t for f, names in self.includes.items() for n in names if (t := self.resolve(f, n))}


def directive_words(text: str) -> set[str]:
    """Every identifier on a preprocessor line of TEXT (continuation lines included), wherever the line sits: a macro
    body or an #if condition spells names the compiler reads for some version (a `#define` swallowed into a
    neighbouring chunk, or inside a declaration, is still a use)."""
    words: set[str] = set()
    lines = text.split("\n")
    n = 0
    while n < len(lines):
        if lines[n].lstrip().startswith("#"):
            end = n
            while lines[end].rstrip().endswith("\\") and end + 1 < len(lines):
                end += 1
            words.update(IDENT.findall("\n".join(lines[n : end + 1])))
            n = end
        n += 1
    return words - KEYWORDS


def dead_layers(files: dict[str, list[Chunk]], outside: set[str]) -> list[list[tuple[str, Chunk]]]:
    """Layers of dead declarations: layer 0 is used by no other dead declaration, layer 1 only by layer 0, ..."""
    every = [(p, c) for p, chunks in files.items() for c in chunks]
    live = set(outside)
    for _, c in every:
        if c.enforced:
            live |= c.defs | c.refs  # the check stays, and so does everything it measures
        elif c.opaque:
            live |= set(IDENT.findall(c.text)) - KEYWORDS
        else:
            live |= directive_words(c.text)  # a directive inside a declaration chunk still uses its names
    changed = True
    while changed:
        changed = False
        for _, c in every:
            if not c.opaque and c.defs & live and not c.refs <= live:
                live |= c.refs
                changed = True
    dead = [(p, c) for p, c in every if not c.opaque and not c.defs & live]
    owner = {name: i for i, (_, c) in enumerate(dead) for name in c.defs}
    users: dict[int, set[int]] = {i: set() for i in range(len(dead))}
    for i, (_, c) in enumerate(dead):
        for ref in c.refs:
            j = owner.get(ref)
            if j is not None and j != i:
                users[j].add(i)
    remaining = set(users)
    layers: list[list[tuple[str, Chunk]]] = []
    while remaining:
        now = {i for i in remaining if not users[i] & remaining} or set(remaining)  # a cycle goes together
        layers.append([dead[i] for i in sorted(now)])
        remaining -= now
    return layers


def remove(text: str, chunks: list[Chunk]) -> str:
    """`text` without the lines of `chunks` (and the comment lines directly above each, and a doubled blank line)."""
    lines = text.split("\n")
    gone: set[int] = set()
    for c in chunks:
        top = c.first
        while top > 0 and lines[top - 1].strip().startswith(("/*", "*", "//")) and top - 1 not in gone:
            top -= 1
        gone.update(range(top, c.last + 1))
    out: list[str] = []
    for i, line in enumerate(lines):
        if i in gone:
            continue
        if not line.strip() and out and not out[-1].strip() and i - 1 in gone:
            continue
        out.append(line)
    return "\n".join(out)


TAG_ONLY = re.compile(r"(struct|union|enum)\b[^;{}]*(\{.*\})?\s*;\Z", re.S)
PROTOTYPE = re.compile(r"(?:\w+[\s*]+)+(\w+)\s*\(\s*[^*\s]", re.S)


def declaration_only(text: str) -> bool:
    """A chunk that only declares (a typedef, a tag, an extern, a prototype): the cut that leaves the object as it was.
    A definition (a function, a variable, initialized data) is not."""
    t = text.strip()
    if t.startswith("typedef"):
        return True
    if "=" in t or t.startswith("static"):
        return False
    if TAG_ONLY.match(t):
        return True
    if "{" in t:
        return False
    return t.startswith("extern") or bool(PROTOTYPE.match(t))


def chunks_of(text: str) -> list[Chunk]:
    chunks = split(strip_comments(text))
    for c in chunks:
        analyse(c)
    return chunks


def source_files(index: Index, folder: str) -> list[str]:
    """The units of `folder`: its sources that no other file includes (an included source is part of its includer)."""
    included = index.included_files()
    return sorted(n for n in index.words if under(n, folder) and n.lower().endswith(SOURCE_SUFFIXES) and n not in included)


def header_files(index: Index, folder: str) -> list[str]:
    return sorted(n for n in index.words if under(n, folder) and n.lower().endswith(C_SUFFIXES))


def under(name: str, folder: str) -> bool:
    folder = folder.strip("/")
    return not folder or folder == "." or name == folder or name.startswith(folder + "/")


def layer_row(kind: str, k: int, layer: list[tuple[str, Chunk]]) -> dict[str, object]:
    return {
        "scope": kind,
        "layer": k,
        "declarations": len(layer),
        "files": len({p for p, _ in layer}),
        "names": sorted({n for _, c in layer for n in c.defs})[:8],
    }


def cut(state: dict[str, str], layer_sets: list[list[tuple[str, Chunk]]]) -> dict[str, list[Chunk]]:
    """Remove every layer of one scope from `state` at once (a chunk's lines refer to the text the scope read)."""
    by_file: dict[str, list[Chunk]] = {}
    for layer in layer_sets:
        for p, c in layer:
            by_file.setdefault(p, []).append(c)
    for p, chunks in by_file.items():
        state[p] = remove(state[p], chunks)
    return by_file


def lines_of(text: str) -> list[str]:
    out = re.findall(r"[^\n]*\n|[^\n]+", text)
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n\\ No newline at end of file\n"
    return out


def file_diff(path: str, before: str, after: str) -> str:
    return f"diff --git a/{path} b/{path}\n" + "".join(
        difflib.unified_diff(lines_of(before), lines_of(after), f"a/{path}", f"b/{path}")
    )


def slug(folders: list[str]) -> str:
    name = re.sub(r"[^A-Za-z0-9]+", "_", folders[0]).strip("_") or "root"
    return name + (f"+{len(folders) - 1}" if len(folders) > 1 else "")


def merge(folders: list[str], needs: set[tuple[str, str]]) -> list[list[str]]:
    """Folders whose dead declarations use each other's, as groups (one batch each)."""
    parent = {f: f for f in folders}

    def find(f: str) -> str:
        while parent[f] != f:
            parent[f] = parent[parent[f]]
            f = parent[f]
        return f

    for a, b in needs:
        parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for f in folders:
        groups.setdefault(find(f), []).append(f)
    return [sorted(g) for g in sorted(groups.values())]


def run(root: Path, folder: str, packs: Path | None = None, apply: bool = False) -> dict[str, object]:
    """Find the dead declarations of `folder` (a path in the project) to a fixpoint: the headers of the folder are one
    scope, each source unit of it a scope of its own, and the passes repeat while one scope's removal frees the other's
    names. `packs` gets one diff per module folder (`dead-decls-NN-<module>.diff`, in landing order, each over the
    tree the earlier ones leave) and `dead-decls.json`; `apply` writes the whole fixpoint into the tree."""
    index = Index(root)
    headers = header_files(index, folder)
    units = source_files(index, folder)
    original = {n: (root / n).read_bytes().decode("latin-1") for n in [*headers, *units]}
    state = dict(original)
    removed: dict[str, list[Chunk]] = {}
    scopes: dict[str, str] = {}  # file -> "source" | "header"
    rows: list[dict[str, object]] = []
    passes = 0
    todo = {"source": True, "header": True}
    while any(todo.values()) and passes < MAX_PASSES:
        passes += 1
        now, todo = todo, {"source": False, "header": False}
        for unit in units if now["source"] else []:
            chunks = chunks_of(state[unit])
            for c in chunks:
                if not c.opaque and not declaration_only(c.text):
                    c.opaque = True
            layers = dead_layers({unit: chunks}, index.unit_outside(unit))
            if layers:
                rows += [layer_row("source", k, layer) for k, layer in enumerate(layers)]
                for p, cs in cut(state, layers).items():
                    removed.setdefault(p, []).extend(cs)
                    scopes[p] = "source"
                    index.put(p, state[p])
                todo["header"] = True
        files = {n: chunks_of(state[n]) for n in headers} if now["header"] else {}
        layers = dead_layers(files, index.outside(set(headers))) if files else []
        if layers:
            rows += [layer_row("header", k, layer) for k, layer in enumerate(layers)]
            for p, cs in cut(state, layers).items():
                removed.setdefault(p, []).extend(cs)
                scopes[p] = "header"
                index.put(p, state[p])
            todo["source"] = True
    changed = sorted(n for n in state if state[n] != original[n])
    batches = emit_batches(changed, original, state, removed, scopes, packs)
    if apply:
        for n in changed:
            (root / n).write_bytes(state[n].encode("latin-1"))
    dead = sum(int(r["declarations"]) for r in rows)  # type: ignore[call-overload]
    return {
        "folder": folder,
        "headers": len(headers),
        "units": len(units),
        "passes": passes,
        "dead": dead,
        "layers": rows,
        "batches": batches,
    }


def emit_batches(
    changed: list[str],
    original: dict[str, str],
    state: dict[str, str],
    removed: dict[str, list[Chunk]],
    scopes: dict[str, str],
    packs: Path | None,
) -> list[dict[str, object]]:
    """One batch per module folder (the folders whose dead declarations use each other's share one): sources first,
    then headers, which the sources' removal may have freed."""
    batches: list[dict[str, object]] = []
    for kind in ("source", "header"):
        by_folder: dict[str, list[str]] = {}
        for n in changed:
            if scopes[n] == kind:
                by_folder.setdefault(posixpath.dirname(n), []).append(n)
        owner = {name: f for f, ns in by_folder.items() for n in ns for c in removed[n] for name in c.defs}
        needs = {
            (f, owner[ref])
            for f, ns in by_folder.items()
            for n in ns
            for c in removed[n]
            for ref in c.refs
            if ref in owner and owner[ref] != f
        }
        for group in merge(sorted(by_folder), needs) if kind == "header" else [[f] for f in sorted(by_folder)]:
            files = sorted(n for f in group for n in by_folder[f])
            count = sum(len(removed[n]) for n in files)
            name = f"dead-decls-{len(batches):02d}-{slug(group)}.diff"
            message = f"Remove {count} dead declarations of {', '.join(group)}"
            if packs is not None:
                packs.mkdir(parents=True, exist_ok=True)
                diff = f"# MSG: {message}\n" + "".join(file_diff(n, original[n], state[n]) for n in files)
                (packs / name).write_bytes(diff.encode("latin-1"))
            batches.append(
                {
                    "batch": name,
                    "scope": kind,
                    "folders": group,
                    "files": len(files),
                    "declarations": count,
                    "patch": f"dead-decls={name}:{message}",
                }
            )
    if packs is not None:
        packs.mkdir(parents=True, exist_ok=True)
        (packs / "dead-decls.json").write_text(json.dumps({"batches": batches}, indent=1, sort_keys=True) + "\n")
    return batches
