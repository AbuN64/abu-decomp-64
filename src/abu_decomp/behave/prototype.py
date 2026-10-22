"""Function prototypes from C text, and the MIPS o32 slots their arguments and result use.

Only what input generation and comparison need: how many argument words a function takes, which are floats,
pointers or narrow integers, and what the result register holds. Anything not recognised is a `word` (an unknown
32-bit value) and the prototype says so, so the verdict can tell a comparison made on facts from one made blind.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

INT_KINDS = {
    "s8": "int8",
    "u8": "uint8",
    "char": "int8",
    "signed char": "int8",
    "unsigned char": "uint8",
    "s16": "int16",
    "u16": "uint16",
    "short": "int16",
    "signed short": "int16",
    "unsigned short": "uint16",
    "s32": "int32",
    "u32": "uint32",
    "int": "int32",
    "signed int": "int32",
    "unsigned int": "uint32",
    "unsigned": "uint32",
    "signed": "int32",
    "long": "int32",
    "unsigned long": "uint32",
    "signed long": "int32",
    "s64": "int64",
    "u64": "uint64",
    "long long": "int64",
    "signed long long": "int64",
    "unsigned long long": "uint64",
    "f32": "float",
    "float": "float",
    "f64": "double",
    "double": "double",
    "void": "void",
    "bool": "uint8",
    "BOOL": "int32",
}
QUALIFIERS = {"const", "volatile", "register", "extern", "static", "inline", "__inline__", "struct", "union", "enum"}
_NAME = re.compile(r"[A-Za-z_]\w*")
_STORAGE = QUALIFIERS - {"struct", "union", "enum"}


@dataclass(frozen=True)
class Param:
    name: str
    ctype: str
    kind: str  # an INT_KINDS value, pointer, funcptr, struct (by value), or word (unknown 32-bit value)

    @property
    def words(self) -> int:
        return 2 if self.kind in ("int64", "uint64", "double") else 1


@dataclass(frozen=True)
class Prototype:
    name: str
    ret_ctype: str
    ret: str
    params: tuple[Param, ...]
    variadic: bool
    source: str  # where the declaration came from: a path:line, or "inferred"

    @property
    def known(self) -> bool:
        return self.source != "inferred"

    @property
    def blind(self) -> bool:
        """True when a part of the signature is a guess: unknown types, by-value structs or variadic arguments."""
        return (
            not self.known
            or self.variadic
            or any(p.kind in ("word", "struct") for p in self.params)
            or self.ret in ("word", "struct")
        )

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "source": self.source,
            "returns": self.ret_ctype or "void",
            "params": [{"name": p.name, "type": p.ctype, "kind": p.kind} for p in self.params],
            "variadic": self.variadic,
        }


def inferred(name: str) -> Prototype:
    """What a function with no declaration is assumed to take: four unknown argument words."""
    return Prototype(name, "", "word", tuple(Param(f"a{i}", "", "word") for i in range(4)), False, "inferred")


def split_arguments(text: str) -> list[str]:
    """Top-level comma split of a parameter list (function-pointer parameters keep their commas)."""
    parts, depth, current = [], 0, []
    for char in text:
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if "".join(current).strip():
        parts.append("".join(current).strip())
    return parts


def classify(ctype: str, aliases: dict[str, str] | None = None) -> str:
    """The kind of a C type spelled in `ctype`; `aliases` maps a typedef name to its kind or to a C type."""
    text = re.sub(r"\s+", " ", ctype.replace("\t", " ")).strip()
    if "(*" in text.replace(" ", ""):
        return "funcptr"
    if "*" in text or "[" in text:
        return "pointer"
    words = [w for w in text.split(" ") if w not in _STORAGE]
    if words and words[0] in ("struct", "union"):
        return "struct"
    words = [w for w in words if w != "enum"]
    name = " ".join(words)
    if name in INT_KINDS:
        return INT_KINDS[name]
    if aliases and name in aliases:
        return aliases[name]
    return "word" if name else "void"


def _param(text: str, index: int, aliases: dict[str, str] | None) -> Param:
    text = text.strip()
    if "(*" in text.replace(" ", ""):
        match = re.search(r"\(\s*\*\s*(\w+)?", text)
        return Param((match.group(1) if match else None) or f"arg{index}", text, "funcptr")
    text = re.sub(r"\[[^\]]*\]", "[]", text)
    match = re.search(r"([A-Za-z_]\w*)\s*(\[\])?\s*$", text)
    name, ctype = f"arg{index}", text
    if match:
        before = text[: match.start()].strip()
        # `s32 arg0` names a parameter; a lone `s32` or `CVector3 *` does not (no preceding type token).
        if (
            before
            and (before[-1] == "*" or _NAME.search(before))
            and match.group(1) not in INT_KINDS
            and match.group(1) not in QUALIFIERS
        ):
            name, ctype = match.group(1), before + (" *" if match.group(2) else "")
    return Param(name, ctype, classify(ctype, aliases))


def parse_declaration(
    text: str, name: str, aliases: dict[str, str] | None = None, source: str = ""
) -> Prototype | None:
    """The first declaration or definition of `name(...)` in C text, or None.

    Calls and assignments are not declarations: the text before the name must be a type, and the closing
    parenthesis must be followed by `;`, `{` or an attribute.
    """
    for match in re.finditer(rf"(?<![\w.>]){re.escape(name)}\s*\(", text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        head = text[line_start : match.start()].strip()
        if not head or head.endswith(("=", "return", "(", ",", "+", "-", "&&", "||")) or "(" in head:
            continue
        depth, end = 1, match.end()
        while end < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[end], 0)
            end += 1
        if depth or not re.match(r"\s*(;|\{|__attribute__)", text[end:]):
            continue
        ret = " ".join(w for w in head.split() if w not in _STORAGE)
        inner = text[match.end() : end - 1]
        rows = split_arguments(inner)
        variadic = bool(rows) and rows[-1] == "..."
        rows = [r for r in rows if r != "..."]
        if len(rows) == 1 and rows[0] in ("void", ""):
            rows = []
        params = tuple(_param(row, i, aliases) for i, row in enumerate(rows))
        line = text.count("\n", 0, match.start()) + 1
        return Prototype(name, ret, classify(ret, aliases), params, variadic, f"{source}:{line}" if source else "text")
    return None


def scan_aliases(texts: list[str]) -> dict[str, str]:
    """typedef name -> kind, from `typedef <type> name;` lines and `typedef struct ... { } name;` bodies."""
    aliases: dict[str, str] = {}
    for text in texts:
        flat = re.sub(r"/\*.*?\*/|//[^\n]*", " ", text, flags=re.S)
        for match in re.finditer(r"typedef\s+(?:struct|union)\s*\w*\s*\{", flat):
            depth, i = 1, match.end()
            while i < len(flat) and depth:
                depth += {"{": 1, "}": -1}.get(flat[i], 0)
                i += 1
            tail = re.match(r"\s*([\w\s,\*]+);", flat[i:])
            if tail:
                for piece in tail.group(1).split(","):
                    names = _NAME.findall(piece)
                    if names:
                        aliases.setdefault(names[-1], "pointer" if "*" in piece else "struct")
        for match in re.finditer(r"typedef\s+([^;{}()]+?)\s+(\**)\s*(\w+)\s*;", flat):
            base, stars, name = match.groups()
            if base.split()[:1] in (["struct"], ["union"]) and not stars:
                aliases.setdefault(name, "struct")
            else:
                aliases.setdefault(name, "pointer" if stars else classify(base, aliases))
        for match in re.finditer(r"typedef\s+[^;()]+\(\s*\*\s*(\w+)\s*\)\s*\([^;]*\)\s*;", flat):
            aliases.setdefault(match.group(1), "funcptr")
    return aliases


def find(root: Path, name: str, globs: tuple[str, ...], preferred: Path | None = None) -> Prototype | None:
    """The prototype of `name` from `preferred` (a candidate file) first, then the headers and sources under
    `root` matched by `globs`; typedefs come from the headers."""
    paths = sorted({p for pattern in globs for p in root.glob(pattern) if p.is_file()})
    texts = {p: p.read_text(errors="replace") for p in paths}
    aliases = scan_aliases(list(texts.values()))
    if preferred is not None:
        text = preferred.read_text(errors="replace")
        found = parse_declaration(text, name, aliases, str(preferred))
        if found:
            return found
    for path, text in texts.items():
        if name in text:
            found = parse_declaration(text, name, aliases, str(path.relative_to(root)))
            if found:
                return found
    return None


# --- the o32 calling convention -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    param: Param
    where: tuple[str, ...]  # registers ("a0",), ("a0", "a1") for a 64-bit value, ("f12",), or ("stack:16",)


def argument_slots(proto: Prototype) -> list[Slot]:
    """Where o32 puts each argument: a0-a3 in order, the first two arguments in f12/f14 while every earlier one was a
    float or double, the rest on the stack (a 64-bit value takes an even-aligned pair of words)."""
    slots, word, leading_floats, fp_used = [], 0, True, 0
    for param in proto.params:
        if param.words == 2:
            word += word & 1
        floaty = param.kind in ("float", "double")
        if floaty and leading_floats and fp_used < 2:
            where: tuple[str, ...] = (("f12", "f14")[fp_used],)
            fp_used += 1
        elif word + param.words <= 4:
            where = tuple(f"a{word + i}" for i in range(param.words))
        else:
            where = tuple(f"stack:{16 + 4 * (word + i)}" for i in range(param.words))
        leading_floats = leading_floats and floaty
        word += param.words
        slots.append(Slot(param, where))
    return slots
