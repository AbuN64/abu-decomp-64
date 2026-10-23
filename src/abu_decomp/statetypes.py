"""C types of the game's global variables, laid out twice: for the port (i386) and for the original (MIPS o32).

Where the types come from: the preprocessed units the port build already wrote (`<units>/**/*.i`). Every distinct
file-scope statement of every unit is parsed once (header text is shared, so each header chunk is read once;
function bodies and initialisers are cut away before parsing), which gives the typedefs, struct/union/enum bodies and
the declared type of every global. Nothing here knows a game: a symbol's type is whatever the headers declare for it.

A layout is a list of runs `(offset, kind, count)`; kind is the canonical element the two sides can both read back as
the same logical value: b (u8), h (u16), w (u32, also float bits and enums), d (u64 and double: two words, high
first), p (a pointer). Bytes that have no host-independent meaning (padding, bitfields, unions, anything unresolved)
are left out."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pycparser import c_ast
from pycparser.c_parser import CParser, ParseError, _TokenStream

MARKER = re.compile(r'^# (\d+) "([^"]*)"((?: \d+)*)$', re.M)
_TOKEN = re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|[{}()\[\];=]')
_ATTRIBUTE = re.compile(r"__(?:attribute|asm)(?:__)?\b")
_NOISE = re.compile(r"\b(?:__extension__|__restrict__|__restrict|__inline__|__inline|__volatile__|_Noreturn)\b")
_PUNCT_BEFORE_EQUALS = frozenset("<>!=+-*/%&|^")
KINDS = "bhwdp"
SIZE = {"b": 1, "h": 2, "w": 4, "d": 8, "p": 4}
ABIS = ("i386", "mips")
_BUILTIN_WORDS = frozenset(
    [
        "void",
        "char",
        "short",
        "int",
        "long",
        "float",
        "double",
        "signed",
        "unsigned",
        "_Bool",
        "__int128",
        "_Complex",
        "__builtin_va_list",
    ]
)
Run = tuple[int, str, int]


def chunks(text: str, seen: set[tuple[str, int]]) -> list[tuple[str, str]]:
    """(file, body) per line-marker chunk of a preprocessed unit; one already read (same file and line) is left out."""
    marks = list(MARKER.finditer(text))
    out = []
    for i, mark in enumerate(marks):
        name = mark[2]
        if name.startswith("<"):
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        key = (name, int(mark[1]))
        if key in seen:
            continue
        seen.add(key)
        out.append((name, text[mark.end() + 1 : end]))
    return out


def _balanced_cut(text: str, word: re.Pattern[str]) -> str:
    """Remove every `word(...)`-shaped extension (attribute lists, asm labels) with its balanced parentheses."""
    while True:
        m = word.search(text)
        if not m:
            return text
        i = m.end()
        while i < len(text) and text[i] in " \t\n":
            i += 1
        if i < len(text) and text[i] == "(":
            depth = 0
            while i < len(text):
                depth += (text[i] == "(") - (text[i] == ")")
                i += 1
                if depth == 0:
                    break
        text = text[: m.start()] + " " + text[i:]


def clean(statement: str) -> str:
    return _NOISE.sub(" ", _balanced_cut(statement, _ATTRIBUTE)).strip()


def statements(body: str) -> list[str]:
    """File-scope statements of one chunk, without function bodies or initialisers (`T x[] = {...};` -> `T x[]`)."""
    out: list[str] = []
    start = 0
    braces = parens = 0
    cut: int | None = None
    function = False
    for m in _TOKEN.finditer(body):
        c = m.group()
        if c[0] in "\"'":
            continue
        if function:
            if c == "{":
                braces += 1
            elif c == "}":
                braces -= 1
                if braces == 0:
                    function, start, cut = False, m.end(), None
            continue
        if c in "([":
            parens += 1
        elif c in ")]":
            parens -= 1
        elif c == "=":
            if braces == 0 and parens == 0 and cut is None and body[m.end() : m.end() + 1] != "=":
                before = body[m.start() - 1 : m.start()]
                if before not in _PUNCT_BEFORE_EQUALS:
                    cut = m.start()
        elif c == "{":
            if braces == 0 and parens == 0 and cut is None and body[start : m.start()].rstrip().endswith(")"):
                function = True
            braces += 1
        elif c == "}":
            braces -= 1
        elif c == ";" and braces == 0 and parens == 0:
            text = clean(body[start : cut if cut is not None else m.start()])
            if text:
                out.append(text)
            start, cut = m.end(), None
    return out


class _Parser(CParser):
    """pycparser keeping its typedef names between statements (one statement per parse)."""

    def __init__(self) -> None:
        super().__init__()
        self.known: dict[str, bool] = {"__builtin_va_list": True}

    def one(self, text: str) -> c_ast.FileAST:
        self._scope_stack = [self.known]
        self.clex.input(text, "")
        self._tokens = _TokenStream(self.clex)
        try:
            ast = self._parse_translation_unit_or_empty()
            if self._peek() is not None:
                raise ParseError("trailing tokens")
        finally:
            for name in [n for n, is_type in self.known.items() if not is_type]:
                del self.known[name]
        return ast


@dataclass
class TypeDb:
    typedefs: dict[str, Any] = field(default_factory=dict)
    tags: dict[str, c_ast.Node] = field(default_factory=dict)
    variables: dict[str, c_ast.Decl] = field(default_factory=dict)
    statements: int = 0
    failed: int = 0
    parser: _Parser = field(default_factory=_Parser)

    def add(self, statement: str) -> None:
        self.statements += 1
        try:
            ast = self.parser.one(statement + ";")
        except Exception:  # an unsupported construct loses that statement, never the run
            self.failed += 1
            return
        for ext in ast.ext:
            self._register(ext)

    def _register(self, ext: c_ast.Node) -> None:
        if isinstance(ext, c_ast.Typedef):
            self._tags_in(ext.type)
            self.typedefs[ext.name] = ext.type
        elif isinstance(ext, c_ast.Decl):
            self._tags_in(ext.type)
            if ext.name and not isinstance(ext.type, c_ast.FuncDecl) and "typedef" not in (ext.storage or []):
                have = self.variables.get(ext.name)
                if have is None or _open_array(have.type):
                    self.variables[ext.name] = ext

    def _tags_in(self, node: Any) -> None:
        while node is not None:
            if isinstance(node, (c_ast.Struct, c_ast.Union)):
                if node.decls is not None:
                    if node.name:
                        self.tags[f"{type(node).__name__.lower()} {node.name}"] = node
                    for d in node.decls:
                        self._tags_in(d.type)
                return
            if isinstance(node, c_ast.Enum):
                if node.name and node.values is not None:
                    self.tags[f"enum {node.name}"] = node
                return
            if isinstance(node, c_ast.FuncDecl):
                return
            node = getattr(node, "type", None)

    def read_unit(self, text: str, seen: set[tuple[str, int]], seen_statements: set[str]) -> None:
        for _name, body in chunks(text, seen):
            for statement in statements(body):
                if statement not in seen_statements:
                    seen_statements.add(statement)
                    self.add(statement)


def _open_array(node: Any) -> bool:
    return isinstance(node, c_ast.ArrayDecl) and node.dim is None


class Unsized(Exception):
    """A type whose size the headers do not give."""


Shape = (
    tuple  # ("k", kind) | ("a", count, Shape) | ("s", ((name|None, Shape), ...)) | ("x",): what the runs are made of
)


@dataclass
class Layout:
    size: int
    align: int
    runs: list[Run]
    shape: Shape = ("x",)


def _scalar(words: list[str], abi: str) -> tuple[str, int]:
    """(kind, alignment) of a builtin type spelled by these words."""
    w = [x for x in words if x not in ("signed", "unsigned", "const", "volatile")]
    if "double" in w:
        return "d", 4 if abi == "i386" else 8
    if "float" in w:
        return "w", 4
    if w.count("long") >= 2 or "__int128" in w:
        return "d", 4 if abi == "i386" else 8
    if "char" in w or "_Bool" in w:
        return "b", 1
    if "short" in w:
        return "h", 2
    if "__builtin_va_list" in w:
        return "p", 4
    return "w", 4


class Layouts:
    """Lays out declared types for one ABI at a time."""

    def __init__(self, db: TypeDb, abi: str) -> None:
        self.db, self.abi = db, abi
        self._cache: dict[int, Layout] = {}

    def of(self, node: Any, depth: int = 0) -> Layout:
        if depth > 40:
            raise Unsized("recursive type")
        if isinstance(node, c_ast.Decl):
            node = node.type
        if isinstance(node, c_ast.PtrDecl):
            return Layout(4, 4, [(0, "p", 1)], ("k", "p"))
        if isinstance(node, c_ast.FuncDecl):
            raise Unsized("function")
        if isinstance(node, c_ast.ArrayDecl):
            return self._array(node, depth)
        if isinstance(node, c_ast.TypeDecl):
            return self.of(node.type, depth + 1)
        if isinstance(node, c_ast.Typename):
            return self.of(node.type, depth + 1)
        if isinstance(node, c_ast.IdentifierType):
            names = list(node.names)
            if len(names) == 1 and names[0] not in _BUILTIN_WORDS:
                target = self.db.typedefs.get(names[0])
                if target is None:
                    raise Unsized(f"unknown type {names[0]}")
                return self.of(target, depth + 1)
            kind, align = _scalar(names, self.abi)
            return Layout(SIZE[kind], align, [(0, kind, 1)], ("k", kind))
        if isinstance(node, c_ast.Enum):
            return Layout(4, 4, [(0, "w", 1)], ("k", "w"))
        if isinstance(node, (c_ast.Struct, c_ast.Union)):
            return self._aggregate(node, depth)
        raise Unsized(type(node).__name__)

    def _array(self, node: c_ast.ArrayDecl, depth: int, count: int | None = None) -> Layout:
        if count is None:
            if node.dim is None:
                raise Unsized("open array")
            count = self.constant(node.dim)
        if count < 0:
            raise Unsized("negative dimension")
        item = self.of(node.type, depth + 1)
        if count == 0 or not item.runs:
            return Layout(item.size * count, item.align, [], ("x",))
        shape = ("a", count, item.shape)
        if len(item.runs) == 1 and item.runs[0][0] == 0 and item.runs[0][2] == 1 and item.runs[0][1] in "bhwp":
            return Layout(item.size * count, item.align, [(0, item.runs[0][1], count)], shape)
        runs: list[Run] = []
        for i in range(count):
            runs.extend((i * item.size + o, k, n) for o, k, n in item.runs)
        return Layout(item.size * count, item.align, _merge(runs), shape)

    def open_array(self, node: Any, count: int) -> Layout:
        """The layout of an `extern T x[]` whose element count the symbol table gives."""
        if not isinstance(node, c_ast.ArrayDecl):
            raise Unsized("not an array")
        return self._array(node, 0, count)

    def _aggregate(self, node: Any, depth: int) -> Layout:
        kind = type(node).__name__.lower()
        if node.decls is None:
            body = self.db.tags.get(f"{kind} {node.name}")
            if body is None:
                raise Unsized(f"incomplete {kind} {node.name}")
            node = body
        key = id(node)
        if key in self._cache:
            return self._cache[key]
        union = kind == "union"
        offset, align, runs, fields = 0, 1, [], []
        bit_unit_end = 0  # bytes of the current bitfield storage unit still open (struct only)
        bits_used = 0
        for decl in node.decls:
            if decl.bitsize is not None:
                unit = self.of(decl.type, depth + 1)
                width = self.constant(decl.bitsize)
                align = max(align, unit.align)
                if not union:
                    if bit_unit_end and bits_used + width <= (bit_unit_end - offset) * 8 and width:
                        bits_used += width
                    else:
                        offset = _round(max(offset, bit_unit_end), unit.align)
                        bit_unit_end = offset + unit.size
                        bits_used = width
                continue
            if bit_unit_end:
                offset, bit_unit_end, bits_used = max(offset, bit_unit_end), 0, 0
            item = self.of(decl.type, depth + 1)
            align = max(align, item.align)
            if union:
                offset = max(offset, item.size)
                continue
            at = _round(offset, item.align)
            runs.extend((at + o, k, n) for o, k, n in item.runs)
            if item.runs:
                fields.append((decl.name, item.shape))
            offset = at + item.size
        if bit_unit_end:
            offset = max(offset, bit_unit_end)
        if union:
            runs, fields = [], []  # members alias each other: no host-independent reading
        result = Layout(_round(offset, align), align, _merge(runs), ("s", tuple(fields)) if fields else ("x",))
        self._cache[key] = result
        return result

    def constant(self, node: Any) -> int:
        if isinstance(node, c_ast.Constant):
            text = node.value.rstrip("uUlL")
            if node.type in ("char",):
                return ord(text.strip("'")[-1])
            return int(text, 0)
        if isinstance(node, c_ast.UnaryOp):
            if node.op == "sizeof":
                return self.of(node.expr).size
            v = self.constant(node.expr)
            return {"-": -v, "+": v, "~": ~v, "!": int(not v)}[node.op]
        if isinstance(node, c_ast.BinaryOp):
            a, b = self.constant(node.left), self.constant(node.right)
            ops = {
                "+": lambda: a + b,
                "-": lambda: a - b,
                "*": lambda: a * b,
                "/": lambda: a // b,
                "%": lambda: a % b,
                "<<": lambda: a << b,
                ">>": lambda: a >> b,
                "&": lambda: a & b,
                "|": lambda: a | b,
                "^": lambda: a ^ b,
            }
            if node.op in ops:
                return ops[node.op]()
        if isinstance(node, c_ast.Cast):
            return self.constant(node.expr)
        raise Unsized(f"non-constant {type(node).__name__}")


def _round(value: int, align: int) -> int:
    return (value + align - 1) // align * align


def _merge(runs: list[Run]) -> list[Run]:
    out: list[Run] = []
    for off, kind, n in runs:
        if out and out[-1][1] == kind and out[-1][0] + SIZE[kind] * out[-1][2] == off:
            out[-1] = (out[-1][0], kind, out[-1][2] + n)
        else:
            out.append((off, kind, n))
    return out


def is_const(node: Any) -> bool:
    """A read-only datum (`const T x`, `const T x[]`): it cannot change, so it is no state."""
    while node is not None:
        if isinstance(node, c_ast.TypeDecl):
            return "const" in (node.quals or [])
        if isinstance(node, c_ast.PtrDecl):
            return False
        node = getattr(node, "type", None)
    return False


def elements(shape: Shape) -> int:
    """How many canonical elements a shape holds."""
    if shape[0] == "k":
        return 1
    if shape[0] == "a":
        return shape[1] * elements(shape[2])
    if shape[0] == "s":
        return sum(elements(s) for _n, s in shape[1])
    return 0


def field_at(shape: Shape, index: int, prefix: str = "") -> tuple[str, str]:
    """(path, kind) of element number `index` of a shape: `players[2].back`, `kind`."""
    while True:
        if shape[0] == "k":
            return prefix or "", shape[1]
        if shape[0] == "a":
            per = elements(shape[2])
            prefix, index, shape = f"{prefix}[{index // per}]", index % per, shape[2]
        elif shape[0] == "s":
            for name, sub in shape[1]:
                n = elements(sub)
                if index < n:
                    prefix = f"{prefix}.{name}" if name and prefix else (name or prefix)
                    shape = sub
                    break
                index -= n
            else:
                raise IndexError(index)
        else:
            raise IndexError(index)
