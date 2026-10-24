"""A small C type reader and layout calculator for the owner structs of declared aggregates (MIPS o32 rules), and the
offset -> member path resolution (`OWNER.a.b[2]`). Headers are read as text: preprocessor lines are dropped, simple
`#define NAME int` constants and enum members feed array sizes. A layout is trusted only after it reproduces the
compiler's sizeof of the owner (`verify`), so a header with `#if` members is refused, never guessed."""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from abu_decomp.data.cdecl import strip_comments

SCALARS = {
    "char": 1, "s8": 1, "u8": 1, "short": 2, "s16": 2, "u16": 2, "int": 4, "s32": 4, "u32": 4, "long": 4,
    "float": 4, "f32": 4, "double": 8, "f64": 8, "s64": 8, "u64": 8, "void": 1,
}  # fmt: skip
FLOATS = {"float", "f32", "double", "f64"}
MODS = {"unsigned", "signed", "const", "volatile", "static", "register"}
TOK = re.compile(r"[A-Za-z_]\w*|0[xX][0-9A-Fa-f]+|\d+|<<|>>|.", re.S)


class LayoutError(Exception):
    pass


@dataclass
class CType:
    kind: str  # scalar | ptr | struct | array
    size: int = 0
    align: int = 1
    name: str = ""
    float: bool = False
    members: list[tuple[str, CType, int]] = field(default_factory=list)  # struct/union: (name, type, offset)
    elem: CType | None = None
    count: int = 0
    union: bool = False


def scalar(name: str, size: int) -> CType:
    return CType("scalar", size, min(size, 8), name, name in FLOATS)


PTR = CType("ptr", 4, 4, "ptr")


def array(elem: CType, count: int) -> CType:
    return CType("array", elem.size * count, elem.align, elem=elem, count=count)


def make_struct(members: list[tuple[str, CType]], union: bool, name: str = "") -> CType:
    off, align, laid = 0, 1, []
    size = 0
    for n, t in members:
        align = max(align, t.align)
        if union:
            laid.append((n, t, 0))
            size = max(size, t.size)
            continue
        off = (off + t.align - 1) // t.align * t.align
        laid.append((n, t, off))
        off += t.size
    size = size if union else off
    return CType("struct", (size + align - 1) // align * align, align, name, members=laid, union=union)


class Types:
    """Typedefs, tags, constants of a set of headers."""

    def __init__(self) -> None:
        self.typedefs: dict[str, CType] = {}
        self.tags: dict[str, CType] = {}
        self.consts: dict[str, int] = {}
        self.t: list[str] = []
        self.i = 0
        self.errors: list[str] = []  # the last pass's refusals: what a header holds that the reader does not know

    # ---- reading
    def add_text(self, text: str) -> None:
        for m in re.finditer(r"^\s*#\s*define\s+(\w+)\s+(\(?-?\w+\)?)\s*$", text, re.M):
            with contextlib.suppress(ValueError):
                self.consts.setdefault(m.group(1), int(m.group(2).strip("()"), 0))
        code = "\n".join("" if ln.lstrip().startswith("#") else ln for ln in strip_comments(text).splitlines())
        self.t, self.i = TOK.findall(code), 0
        self.t = [x for x in self.t if not x.isspace()]
        while self.i < len(self.t):
            try:
                self.top()
            except LayoutError as exc:
                self.errors.append(f"{exc} near {' '.join(self.t[max(0, self.i - 6) : self.i + 2])}")
                self.skip_stmt()

    def peek(self) -> str:
        return self.t[self.i] if self.i < len(self.t) else ""

    def take(self, want: str | None = None) -> str:
        tok = self.peek()
        if want is not None and tok != want:
            raise LayoutError(f"expected {want!r}, got {tok!r}")
        self.i += 1
        return tok

    def skip_stmt(self) -> None:
        depth = 0
        while self.i < len(self.t):
            tok = self.take()
            if tok == "{":
                depth += 1
            elif tok == "}":
                depth -= 1
                if depth <= 0:
                    return
            elif tok == ";" and depth <= 0:
                return

    def top(self) -> None:
        tok = self.peek()
        if tok == "typedef":
            self.take()
            base = self.specifier()
            while True:
                name, typ = self.declarator(base)
                self.typedefs[name] = typ
                if self.peek() == ",":
                    self.take()
                    continue
                break
            self.take(";")
        elif tok in ("struct", "union") and self.t[self.i + 2 : self.i + 3] == [";"]:
            self.specifier()
            self.take(";")
        elif tok in ("struct", "union", "enum") and self.t[self.i + 1 : self.i + 3] and self.t[self.i + 2] == "{":
            self.specifier()
            self.skip_stmt() if self.peek() != ";" else self.take(";")
        else:
            self.skip_stmt()

    # ---- grammar
    def specifier(self) -> CType:
        words: list[str] = []
        mods: list[str] = []
        while self.peek() in MODS:
            mods.append(self.take())
        tok = self.peek()
        if tok in ("struct", "union"):
            self.take()
            tag = self.take() if self.peek() != "{" else ""
            if self.peek() == "{":
                self.take("{")
                members: list[tuple[str, CType]] = []
                while self.peek() != "}":
                    if not self.peek():
                        raise LayoutError("unterminated body")
                    base = self.specifier()
                    while True:
                        name, typ = self.declarator(base)
                        members.append((name, typ))
                        if self.peek() == ",":
                            self.take()
                            continue
                        break
                    self.take(";")
                self.take("}")
                st = make_struct(members, tok == "union", tag)
                if tag in self.tags:  # a forward-declared tag: fill the object the typedefs already point at
                    self.tags[tag].__dict__.update(st.__dict__)
                    return self.tags[tag]
                if tag:
                    self.tags[tag] = st
                return st
            if tag not in self.tags:  # forward use: a placeholder that its definition fills in later
                self.tags[tag] = CType("struct", 0, 1, tag, union=(tok == "union"))
            return self.tags[tag]
        if tok == "enum":
            self.take()
            if self.peek() != "{":
                self.take()
            if self.peek() == "{":
                self.take("{")
                nxt = 0
                while self.peek() != "}":
                    name = self.take()
                    if self.peek() == "=":
                        self.take()
                        nxt = self.expr({",", "}"})
                    self.consts[name] = nxt
                    nxt += 1
                    if self.peek() == ",":
                        self.take()
                self.take("}")
            return scalar("int", 4)
        known = set(SCALARS) | set(self.typedefs)
        while self.peek() in MODS | {"long", "short", "int", "char", "float", "double", "void"} or (
            not words and self.peek() in known
        ):
            words.append(self.take())
            if words[-1] in self.typedefs and len(words) == 1:
                return self.typedefs[words[-1]]
        words = [w for w in words if w not in MODS] or words
        if not words and mods:
            return scalar("int", 4)
        if not words:
            raise LayoutError(f"unknown type {self.peek()!r}")
        if words.count("long") == 2:
            return scalar("s64", 8)
        key = next((w for w in words if w in SCALARS), "int")
        return scalar(key, SCALARS[key])

    def expr(self, stop: set[str]) -> int:
        parts: list[str] = []
        depth = 0
        while self.peek() and not (depth == 0 and self.peek() in stop):
            tok = self.take()
            if tok == "sizeof" and self.peek() == "(":
                self.take()
                inner = []
                while self.peek() != ")":
                    if not self.peek():
                        raise LayoutError("unterminated sizeof")
                    inner.append(self.take())
                self.take(")")
                parts.append(str(self.sizeof_tokens(inner)))
                continue
            depth += tok == "("
            depth -= tok == ")"
            parts.append(tok)
        out = []
        for p in parts:
            if p in self.consts:
                p = str(self.consts[p])
            elif re.match(r"[A-Za-z_]", p):
                raise LayoutError(f"unknown constant {p}")
            out.append(p)
        src = " ".join(out)
        if not re.fullmatch(r"[0-9xXa-fA-F+\-*/()<> ]*", src) or not src.strip():
            raise LayoutError(f"bad constant {src!r}")
        return int(eval(src.replace("/", "//"), {"__builtins__": {}}))

    def sizeof_tokens(self, toks: list[str]) -> int:
        saved = self.t, self.i
        self.t, self.i = toks, 0
        try:
            typ = self.specifier()
            while self.peek() == "*":
                self.take()
                typ = PTR
        finally:
            self.t, self.i = saved
        return typ.size

    def declarator(self, base: CType) -> tuple[str, CType]:
        typ = base
        while self.peek() == "*":
            self.take()
            typ = PTR
            while self.peek() in MODS:
                self.take()
        if self.peek() == "(":  # function pointer: (*name)(...)
            self.take()
            while self.peek() == "*":
                self.take()
            name = self.take()
            self.take(")")
            self.take("(")
            depth = 1
            while depth:
                tok = self.take()
                depth += tok == "("
                depth -= tok == ")"
            return name, PTR
        name = self.take()
        if not re.match(r"[A-Za-z_]", name):
            raise LayoutError(f"bad declarator {name!r}")
        dims: list[int] = []
        while self.peek() == "[":
            self.take()
            dims.append(self.expr({"]"}))
            self.take("]")
        if self.peek() == ":":
            raise LayoutError("bit-field")
        for d in reversed(dims):
            typ = array(typ, d)
        return name, typ

    # ---- use
    def parse_type_text(self, text: str) -> CType:
        """`Name`, `Name[4]`, `struct Tag`, `s32[2][3]` (a decl's type text)."""
        m = re.match(r"^(.*?)\s*((?:\[[^\]]*\])*)$", text.strip())
        base, dims = m.group(1), re.findall(r"\[([^\]]*)\]", m.group(2))
        if "*" in base:
            raise LayoutError("pointer")
        saved = self.t, self.i
        self.t, self.i = TOK.findall(base), 0
        self.t = [x for x in self.t if not x.isspace()]
        try:
            typ = self.specifier()
            counts = [int(str(self.consts.get(d.strip(), d.strip()) or 0), 0) if d.strip() else 0 for d in dims]
        finally:
            self.t, self.i = saved
        for c in reversed(counts):
            typ = array(typ, c)
        return typ


def leaf_path(typ: CType, offset: int, width: int, want_float: bool | None, prefix: str = "") -> tuple[str, str] | None:
    """The member path (with the leading `.` or `[i]`) of the leaf at OFFSET in TYP, plus a note ('' | padding |
    subword | union). None when no leaf of that width starts there."""
    if typ.kind == "array":
        if typ.elem is None or typ.elem.size == 0:
            return None
        i, rest = divmod(offset, typ.elem.size)
        if i >= typ.count:
            return None
        return leaf_path(typ.elem, rest, width, want_float, prefix + f"[{i}]")
    if typ.kind == "struct":
        hits = [(n, t, o) for n, t, o in typ.members if o <= offset < o + max(t.size, 1)]
        for n, t, o in hits:
            got = leaf_path(t, offset - o, width, want_float, prefix + "." + n)
            if got:
                note = got[1] or ("union" if typ.union and len(hits) > 1 else "")
                if re.search(r"(reserved|pad|unused)", n, re.I) and not got[1]:
                    note = "padding"
                return got[0], note
        return None
    if offset == 0 and (width == 0 or typ.size == width or (typ.kind == "ptr" and width == 4)):
        if want_float is not None and typ.kind == "scalar" and typ.float != want_float:
            return prefix, "kind"
        return prefix, ""
    if 0 < offset < typ.size or (offset == 0 and typ.size > width > 0):
        return prefix, "subword"
    return None


def closure(include_dir: Path, header: str) -> list[Path]:
    """HEADER and every header it includes ("X" includes below include_dir), dependencies first."""
    order: list[Path] = []
    seen: set[str] = set()

    def visit(rel: str) -> None:
        if rel in seen:
            return
        seen.add(rel)
        f = include_dir / rel
        if not f.is_file():
            return
        text = strip_comments(f.read_text(errors="replace"))
        for m in re.finditer(r'^\s*#\s*include\s+"([^"]+)"', text, re.M):
            visit(m.group(1))
        order.append(f)

    visit(header)
    return order


def read_closure(include_dir: Path, header: str) -> Types:
    """The types visible from HEADER: only what it includes, so a tag that two units define differently stays apart."""
    t = Types()
    texts = [f.read_text(errors="replace") for f in closure(include_dir, header)]
    last = None
    for _ in range(4):  # a forward typedef is completed by a later body
        t.errors = []
        for text in texts:
            t.add_text(text)
        sig = sorted((k, v.size) for k, v in t.tags.items())
        if sig == last:
            break
        last = sig
    return t
