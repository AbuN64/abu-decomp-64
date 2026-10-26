"""`decomp cleanup dead-decls`: the declarations of one header folder that nothing reads, as layered removal packs.
A header folder collects declarations (types, enum constants, externs, prototypes, inline functions) whose last use
went away in the cleanup. A name is live when a file outside the folder spells it, or when a live declaration of the
folder spells it (a type used only by an inline, an extern used only by another header); a preprocessor line or a
construct this reader does not recognize keeps every name it spells. Three cases decide the result and are tested:
- a declaration of several names (`extern int a, b;`, an enum with its constants) is live when ANY of its names is;
- a dead declaration used by another dead declaration goes in a later layer, so each layer leaves a tree that
  still compiles (layer 0 first, then layer 1 over it); a cycle of dead declarations is one layer;
- an enum constant used elsewhere keeps its enum.
The reader is a declaration splitter over comment-free text (not a C parser): a chunk ends at `;` at brace depth 0, or
at the `}` of a function definition. It is conservative: when unsure a declaration is live."""

from __future__ import annotations

import difflib
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
MAX_BYTES = 8 << 20


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


def outside_words(root: Path, exclude: set[str]) -> set[str]:
    """Every word of the tracked files but `exclude` (comments blanked in C text): the names the project spells."""
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
    words: set[str] = set()
    for name in listed.decode("utf-8", "replace").split("\0"):
        path = root / name
        if not name or name in exclude or not path.is_file() or path.stat().st_size > MAX_BYTES:
            continue
        try:
            text = path.read_bytes().decode("latin-1")
        except OSError:
            continue
        if "\0" in text:
            continue
        if name.lower().endswith((".c", ".cpp", ".cc", ".h", ".hpp", ".inc")):
            text = strip_comments(text)
        words.update(IDENT.findall(text))
    return words


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


def header_files(root: Path, folder: str) -> list[str]:
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", folder], capture_output=True, check=True
    ).stdout
    return sorted(n for n in listed.decode().split("\0") if n.lower().endswith(C_SUFFIXES))


def run(root: Path, folder: str, packs: Path | None = None, apply: bool = False) -> dict[str, object]:
    """Find the dead declarations of `folder` (a path in the project). `packs` gets one diff per layer
    (`dead-decls-L<n>.diff`, each over the tree the earlier ones leave); `apply` writes layer 0 into the tree."""
    names = header_files(root, folder)
    texts = {n: (root / n).read_text(encoding="latin-1") for n in names}
    files = {}
    for n, t in texts.items():
        chunks = split(strip_comments(t))
        for c in chunks:
            analyse(c)
        files[n] = chunks
    layers = dead_layers(files, outside_words(root, set(names)))
    state = dict(texts)
    rows = []
    for k, layer in enumerate(layers):
        by_file: dict[str, list[Chunk]] = {}
        for p, c in layer:
            by_file.setdefault(p, []).append(c)
        # the chunks' line numbers refer to the original text: later layers re-read the reduced text
        diff: list[str] = []
        for p, chunks in sorted(by_file.items()):
            if k:
                chunks = _reparse(state[p], {name for c in chunks for name in c.defs})
            new = remove(state[p], chunks)
            diff += difflib.unified_diff(state[p].splitlines(), new.splitlines(), f"a/{p}", f"b/{p}", lineterm="", n=3)
            state[p] = new
        if packs is not None:
            packs.mkdir(parents=True, exist_ok=True)
            (packs / f"dead-decls-L{k}.diff").write_text("\n".join(diff) + "\n")
        rows.append(
            {
                "layer": k,
                "declarations": len(layer),
                "files": len(by_file),
                "names": sorted({n for _, c in layer for n in c.defs})[:8],
            }
        )
        if apply and k == 0:
            for p in by_file:
                (root / p).write_text(state[p], encoding="latin-1")
    return {"folder": folder, "headers": len(names), "dead": sum(r["declarations"] for r in rows), "layers": rows}  # type: ignore[misc]


def _reparse(text: str, defs: set[str]) -> list[Chunk]:
    """The chunks of `text` (a state after earlier layers) that define any of `defs`."""
    chunks = split(strip_comments(text))
    for c in chunks:
        analyse(c)
    return [c for c in chunks if not c.opaque and c.defs & defs]
