"""`decomp sweep`: every root cause found once becomes a rule, so its siblings are found without a movie reaching them.
The engine is generic and names no project: the rules are project facts, TOML files that live with the code they check
(`[[rule]]` tables in the game repository's and the port repository's config) and are named on the command line with
`--rules`; `--tree LABEL=DIR` says where each tree the rules mention is. A rule:

    id        short name, the bucket key of its hits
    (a line's `unless` sees the line with its comments, so `/* swap */` can mark a line as meant)
    kind      pattern    a regex per line (`match`), minus lines that also match `unless`; with `multiline = true` the
                         regex runs over the whole text and a hit is the line the match starts on
              decl-use   `declare` finds the names (named group `datum`) a tree defines; `use` finds the places that
                         touch a declared name (also group `datum`): a word array read through a byte cast
              call-arity `callee` (regex, default every name) selects the functions whose definition is read from the
                         paths; a call with fewer arguments than the definition has parameters (`mode = "fewer"`, or
                         `"any"` for a different count) is a hit: a dropped argument
              return-spelling  a function defined to return a struct by value (`returns`, regex of the type names,
                         default a capitalised name; `callee` selects the functions) and spelled the other way:
                         a call with the hidden result pointer as an explicit first argument, or a declaration that says
                         void or a pointer. MIPS passes the hidden pointer where a first parameter goes, so the
                         original mixes both; a native 32-bit host pops the hidden pointer in the callee (ret $4), so
                         the other spelling leaves the stack 4 bytes off. The reverse (a declaration by value of a
                         function defined with a result pointer) is a hit too
              word-alias `symbols` (a file of `NAME = 0xADDRESS;` lines per tree, relative to the tree), `array`
                         (regex, named group `datum`: the word arrays a data unit defines; the unit's address is the
                         hex in its file name, arrays follow each other) and `narrow` (regex, named group `datum`: a
                         byte or halfword extern): a narrow extern whose address lies inside a word array. Big-endian
                         reads the low bytes of a word at +1..+3, a little-endian host reads other bytes. `defines`
                         lists the macros that count as set when the units' #if lines are evaluated
    why       one sentence: the failure this finds and where it was first seen
    severity  "report" (default) or "block"; only a new block hit fails the exit status
    trees     tree labels the rule reads (default every tree); paths / skip: globs relative to a tree root
    declare_paths  decl-use only: where names are declared (default `paths`); a `declare` with a (?P<local>...) group
              that matched (a `static` declaration) makes the name that file's own

A hit is {rule, tree, file, line, text, fp} plus `datum` and `in_fn` (the enclosing function) when known; a
call-arity hit
names the callee as `fn` and the caller in `callers`, so it joins the bucket of a crash in that callee. Comments are
blanked before matching. `fp` is a fingerprint of the rule, file and normalised line (not its number), so a baseline
survives edits above a hit. `--baseline FILE` marks hits already in it as known; `--update-baseline` rewrites it.
`--findings` appends every hit to the findings feed of the profile's `tas.runs` (module tas_findings).
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

from abu_decomp import DecompError

KINDS = ("pattern", "decl-use", "call-arity", "return-spelling", "word-alias")
SEVERITIES = ("report", "block")
_KEYS = frozenset(
    {
        "id",
        "kind",
        "why",
        "severity",
        "trees",
        "paths",
        "skip",
        "match",
        "unless",
        "multiline",
        "declare",
        "declare_paths",
        "use",
        "callee",
        "mode",
        "returns",
        "symbols",
        "array",
        "narrow",
        "defines",
    }
)
_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_IDENT_CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_DEF = re.compile(
    r"^(?P<head>[A-Za-z_][\w \t\*]*?)\b(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^;{}()]*(?:\([^;{}()]*\)[^;{}()]*)*)\)"
    r"\s*\{",
    re.M,
)
_CALL_KEYWORDS = frozenset({"return", "else", "case", "do", "goto", "sizeof"})
_NOT_FUNCTIONS = frozenset({"if", "while", "for", "switch", "return", "sizeof", "defined"})


_QUALIFIERS = frozenset({"extern", "static", "inline", "const", "volatile", "register", "struct", "enum", "union"})
_SCALARS = frozenset(
    {
        "void",
        "int",
        "long",
        "short",
        "char",
        "float",
        "double",
        "unsigned",
        "signed",
        "s8",
        "u8",
        "s16",
        "u16",
        "s32",
        "u32",
        "s64",
        "u64",
        "f32",
        "f64",
        "bool",
        "size_t",
    }
)
_DECL = re.compile(
    r"^(?P<head>[A-Za-z_][\w \t\*]*?)\b(?P<name>[A-Za-z_]\w*)\s*"
    r"\((?P<params>[^;{}()]*(?:\([^;{}()]*\)[^;{}()]*)*)\)\s*;",
    re.M,
)


def _value_type(words: list[str], returns: re.Pattern[str]) -> str | None:
    """The struct type a head returns by value: its last word matches `returns`, is no scalar, and has no star."""
    if not words or "*" in words:
        return None
    last = words[-1]
    if last in _SCALARS or not returns.fullmatch(last):
        return None
    return last


def _symbol_table(path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for line in path.read_text(errors="replace").splitlines():
            m = re.match(r"\s*(\w+)\s*=\s*0x([0-9A-Fa-f]+)", line)
            if m:
                out[m[1]] = int(m[2], 16)
    except OSError:
        pass
    return out


def _kept_lines(text: str, defines: set[str]) -> list[str]:
    """The text a preprocessor keeps when exactly `defines` are set, as one string per kept run of lines."""
    stack: list[list[bool]] = []
    kept: list[str] = []

    def value(expr: str) -> bool:
        e = re.sub(r"defined\s*\(\s*(\w+)\s*\)", lambda m: str(m[1] in defines), expr)
        e = re.sub(r"defined\s+(\w+)", lambda m: str(m[1] in defines), e)
        e = re.sub(r"\b(?!True\b|False\b)[A-Za-z_]\w*\b", "False", e)
        e = e.replace("&&", " and ").replace("||", " or ").replace("!", " not ")
        try:
            return bool(eval(e, {"__builtins__": {}}))
        except Exception:
            return False

    for line in text.split("\n"):
        t = line.strip()
        if t.startswith("#ifdef"):
            v = t.split()[1] in defines
            stack.append([v, v])
        elif t.startswith("#ifndef"):
            v = t.split()[1] not in defines
            stack.append([v, v])
        elif t.startswith("#if"):
            v = value(t[3:])
            stack.append([v, v])
        elif t.startswith("#elif") and stack:
            v = (not stack[-1][0]) and value(t[5:])
            stack[-1] = [stack[-1][0] or v, v]
        elif t.startswith("#else") and stack:
            v = not stack[-1][0]
            stack[-1] = [True, v]
        elif t.startswith("#endif") and stack:
            stack.pop()
        elif all(x[1] for x in stack):
            kept.append(line)
    return ["\n".join(kept)]


def _glob(pattern: str) -> re.Pattern[str]:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


class Rule:
    def __init__(self, table: dict[str, Any], origin: str) -> None:
        extra = set(table) - _KEYS
        if extra:
            raise DecompError("sweep.rule", f"{origin}: unknown rule keys: {', '.join(sorted(extra))}")
        for key in ("id", "kind", "why"):
            if not isinstance(table.get(key), str) or not table[key].strip():
                raise DecompError("sweep.rule", f"{origin}: rule needs a text `{key}`")
        self.id, self.kind, self.why = table["id"], table["kind"], table["why"]
        self.origin = origin
        if self.kind not in KINDS:
            raise DecompError("sweep.rule", f"{origin}: rule {self.id}: kind is one of {', '.join(KINDS)}")
        self.severity = str(table.get("severity", "report"))
        if self.severity not in SEVERITIES:
            raise DecompError("sweep.rule", f"{origin}: rule {self.id}: severity is report or block")
        self.trees = [str(t) for t in table.get("trees", [])]
        self.paths = [_glob(str(p)) for p in table.get("paths", ["**"])]
        self.skip = [_glob(str(p)) for p in table.get("skip", [])]
        self.declare_paths = [_glob(str(p)) for p in table["declare_paths"]] if "declare_paths" in table else self.paths
        self.multiline = bool(table.get("multiline", False))
        self.mode = str(table.get("mode", "fewer"))
        needs = {
            "pattern": ("match",),
            "decl-use": ("declare", "use"),
            "call-arity": (),
            "return-spelling": (),
            "word-alias": ("symbols", "array", "narrow"),
        }[self.kind]
        for key in needs:
            if not isinstance(table.get(key), str):
                raise DecompError("sweep.rule", f"{origin}: rule {self.id} ({self.kind}) needs `{key}`")
        try:
            flags = re.S | re.M if self.multiline else 0
            self.match = re.compile(table["match"], flags) if "match" in table else None
            self.unless = re.compile(table["unless"]) if "unless" in table else None
            self.declare = re.compile(table["declare"]) if "declare" in table else None
            self.use = re.compile(table["use"]) if "use" in table else None
            self.callee = re.compile(str(table.get("callee", r"\w+")))
            self.returns = re.compile(str(table.get("returns", r"[A-Z]\w*")))
            self.array = re.compile(table["array"], re.M) if "array" in table else None
            self.narrow = re.compile(table["narrow"], re.M) if "narrow" in table else None
            self.symbols = str(table.get("symbols", ""))
            self.defines = {str(d) for d in table.get("defines", [])}
        except re.error as error:
            raise DecompError("sweep.rule", f"{origin}: rule {self.id}: {error}") from error
        if self.kind == "decl-use":
            for name, rx in (("declare", self.declare), ("use", self.use)):
                if rx is not None and "datum" not in rx.groupindex:
                    raise DecompError("sweep.rule", f"{origin}: rule {self.id}: `{name}` needs a (?P<datum>...) group")
        if self.kind == "word-alias":
            for name, rx in (("array", self.array), ("narrow", self.narrow)):
                if rx is not None and "datum" not in rx.groupindex:
                    raise DecompError("sweep.rule", f"{origin}: rule {self.id}: `{name}` needs a (?P<datum>...) group")
        if self.mode not in ("fewer", "any"):
            raise DecompError("sweep.rule", f"{origin}: rule {self.id}: mode is fewer or any")

    def wants(self, tree: str, rel: str, paths: list[re.Pattern[str]] | None = None) -> bool:
        if self.trees and tree not in self.trees:
            return False
        if any(p.match(rel) for p in self.skip):
            return False
        return any(p.match(rel) for p in (self.paths if paths is None else paths))


def load_rules(files: list[Path]) -> list[Rule]:
    rules: list[Rule] = []
    for file in files:
        try:
            data = tomllib.loads(file.read_text())
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise DecompError("sweep.rules", f"{file}: {error}") from error
        for table in data.get("rule", []):
            rules.append(Rule(table, str(file)))
    seen: set[str] = set()
    for rule in rules:
        if rule.id in seen:
            raise DecompError("sweep.rules", f"rule id {rule.id} is defined twice")
        seen.add(rule.id)
    return rules


class Source:
    """One file of a tree: comment-blanked text, split into lines; the function index is built when first needed."""

    def __init__(self, tree: str, root: Path, rel: str) -> None:
        self.tree, self.root, self.rel = tree, root, rel
        raw = (root / rel).read_text(errors="replace")
        self.text = _COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m[0]), raw)
        self.lines = self.text.split("\n")
        self.raw_lines = raw.split("\n")
        self._defs: list[tuple[int, str, int | None]] | None = None

    def line_of(self, offset: int) -> int:
        return self.text.count("\n", 0, offset) + 1

    def definitions(self) -> list[tuple[int, str, int | None]]:
        """(line, name, parameter count or None for a variable argument list) of each function body in the file."""
        if self._defs is None:
            self._defs = []
            for m in _DEF.finditer(self.text):
                if m["name"] in _NOT_FUNCTIONS or m["head"].split()[:1] in (["return"], ["else"]):
                    continue
                self._defs.append((self.line_of(m.start("name")), m["name"], _count_params(m["params"])))
        return self._defs

    def function_at(self, line: int) -> str | None:
        found = None
        for start, name, _ in self.definitions():
            if start <= line:
                found = name
            else:
                break
        return found


def _split_top(text: str) -> list[str]:
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


def _count_params(params: str) -> int | None:
    text = params.strip()
    if text in ("", "void"):
        return 0
    if "..." in text:
        return None
    return len(_split_top(text))


def _call_args(text: str, open_at: int) -> tuple[int, int] | None:
    """(argument count, end offset) of the call whose `(` is at `open_at`; None when the parentheses never balance."""
    depth = 0
    for i in range(open_at, min(len(text), open_at + 4000)):
        ch = text[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                inner = text[open_at + 1 : i]
                return (0 if not inner.strip() else len(_split_top(inner)), i)
    return None


def _is_call(text: str, at: int) -> bool:
    """Whether the identifier starting at `at` is called (not declared or defined): what precedes it is not a type."""
    start = text.rfind("\n", 0, at) + 1
    if re.match(r"\s*(?:extern|static|typedef)\b", text[start:at]):
        return False
    i = at - 1
    while i >= 0 and text[i] in " \t\r\n":
        i -= 1
    if i < 0:
        return False
    ch = text[i]
    if ch.isalnum() or ch == "_":
        j = i
        while j >= 0 and (text[j].isalnum() or text[j] == "_"):
            j -= 1
        return text[j + 1 : i + 1] in _CALL_KEYWORDS
    if ch == "*":  # `type *name(`: a declaration when a type word stands before the stars
        while i >= 0 and text[i] in "* \t":
            i -= 1
        return not (i >= 0 and (text[i].isalnum() or text[i] == "_"))
    if ch == ">" and i > 0 and text[i - 1] == "-":
        return False  # a member call (p->f(x)) is a function pointer, never one of the definitions
    return ch != "."


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def _fp(rule: str, rel: str, text: str) -> str:
    return hashlib.sha1(f"{rule}\0{rel}\0{_norm(text)}".encode()).hexdigest()[:12]


class Sweep:
    def __init__(self, rules: list[Rule], trees: dict[str, Path]) -> None:
        self.rules, self.trees = rules, trees
        self.sources: dict[tuple[str, str], Source] = {}
        self.listing: dict[str, list[str]] = {}
        for rule in rules:
            for label in rule.trees:
                if label not in trees:
                    raise DecompError("sweep.tree", f"rule {rule.id} reads tree {label}: pass --tree {label}=DIR")

    def files(self, tree: str) -> list[str]:
        if tree not in self.listing:
            root, rows = self.trees[tree], []
            for base, dirs, names in os.walk(root):
                dirs[:] = sorted(d for d in dirs if d != ".git")
                rel = Path(base).relative_to(root).as_posix()
                rows += [(f"{rel}/{n}" if rel != "." else n) for n in sorted(names)]
            self.listing[tree] = rows
        return self.listing[tree]

    def source(self, tree: str, rel: str) -> Source:
        key = (tree, rel)
        if key not in self.sources:
            self.sources[key] = Source(tree, self.trees[tree], rel)
        return self.sources[key]

    def selected(self, rule: Rule, paths: list[re.Pattern[str]] | None = None) -> list[Source]:
        out = []
        for tree in rule.trees or sorted(self.trees):
            for rel in self.files(tree):
                if rule.wants(tree, rel, paths):
                    try:
                        out.append(self.source(tree, rel))
                    except OSError:
                        continue
        return out

    def hit(self, rule: Rule, src: Source, line: int, **extra: Any) -> dict[str, Any]:
        text = src.raw_lines[line - 1].strip()[:200] if line - 1 < len(src.raw_lines) else ""
        row: dict[str, Any] = {
            "rule": rule.id,
            "severity": rule.severity,
            "tree": src.tree,
            "file": src.rel,
            "line": line,
            "text": text,
            "fp": _fp(rule.id, src.rel, text),
        }
        fn = src.function_at(line)
        if fn:
            row["in_fn"] = fn
        row.update({k: v for k, v in extra.items() if v})
        return row

    def run_rule(self, rule: Rule) -> list[dict[str, Any]]:
        if rule.kind == "pattern":
            return self._pattern(rule)
        if rule.kind == "decl-use":
            return self._decl_use(rule)
        if rule.kind == "return-spelling":
            return self._return_spelling(rule)
        if rule.kind == "word-alias":
            return self._word_alias(rule)
        return self._call_arity(rule)

    def _pattern(self, rule: Rule) -> list[dict[str, Any]]:
        hits = []
        assert rule.match is not None
        for src in self.selected(rule):
            if rule.multiline:
                for m in rule.match.finditer(src.text):
                    if rule.unless and rule.unless.search(m[0]):
                        continue
                    hits.append(self.hit(rule, src, src.line_of(m.start()), datum=_group(m, "datum")))
                continue
            for number, line in enumerate(src.lines, 1):
                m = rule.match.search(line)
                if m and not (rule.unless and rule.unless.search(src.raw_lines[number - 1])):
                    hits.append(self.hit(rule, src, number, datum=_group(m, "datum")))
        return hits

    def _decl_use(self, rule: Rule) -> list[dict[str, Any]]:
        assert rule.declare is not None and rule.use is not None
        names: dict[str, str] = {}
        local: dict[str, set[tuple[str, str]]] = {}  # a name declared with a `local` group is that file's own
        for src in self.selected(rule, rule.declare_paths):
            for number, line in enumerate(src.lines, 1):
                m = rule.declare.search(line)
                if m:
                    names.setdefault(m["datum"], f"{src.rel}:{number}")
                    if _group(m, "local"):
                        local.setdefault(m["datum"], set()).add((src.tree, src.rel))
                    else:
                        local.setdefault(m["datum"], set()).add(("", ""))
        hits = []
        for src in self.selected(rule):
            for number, line in enumerate(src.lines, 1):
                for m in rule.use.finditer(line):
                    where = local.get(m["datum"])
                    if not where or not ({("", ""), (src.tree, src.rel)} & where):
                        continue
                    if not (rule.unless and rule.unless.search(src.raw_lines[number - 1])):
                        hits.append(self.hit(rule, src, number, datum=m["datum"], declared=names[m["datum"]]))
                        break
        return hits

    def _return_spelling(self, rule: Rule) -> list[dict[str, Any]]:
        by_value: dict[str, tuple[str, int | None, str]] = {}  # name -> (type, parameter count, where)
        other: dict[str, str] = {}  # names defined with any other return spelling
        for src in self.selected(rule, rule.declare_paths):
            for m in _DEF.finditer(src.text):
                name = m["name"]
                if name in _NOT_FUNCTIONS or not rule.callee.fullmatch(name):
                    continue
                words = [w for w in re.split(r"[\s]+", m["head"].replace("*", " * ")) if w and w not in _QUALIFIERS]
                kind = _value_type(words, rule.returns)
                where = f"{src.rel}:{src.line_of(m.start('name'))}"
                if kind:
                    by_value.setdefault(name, (kind, _count_params(m["params"]), where))
                else:
                    other.setdefault(name, where)
        mixed = {n for n in by_value if n in other}
        hits = []
        for src in self.selected(rule):
            own = {(line, name) for line, name, _ in src.definitions()}
            for m in _IDENT_CALL.finditer(src.text):
                name = m[1]
                if name not in by_value or name in mixed or not _is_call(src.text, m.start(1)):
                    continue
                kind, want, where = by_value[name]
                counted = _call_args(src.text, m.end() - 1)
                if counted is None or want is None or counted[0] != want + 1:
                    continue
                line = src.line_of(m.start(1))
                if (line, name) in own:
                    continue
                caller = src.function_at(line)
                hits.append(
                    self.hit(
                        rule,
                        src,
                        line,
                        fn=name,
                        callers=[caller] if caller else None,
                        declared=where,
                        detail=f"{name} returns {kind} by value ({where}); this call passes the result pointer itself",
                    )
                )
            for m in _DECL.finditer(src.text):
                name = m["name"]
                if name in by_value and name not in mixed:
                    kind, want, where = by_value[name]
                    words = [w for w in re.split(r"[\s]+", m["head"].replace("*", " * ")) if w and w not in _QUALIFIERS]
                    if _value_type(words, rule.returns) is None and not m["head"].rstrip().endswith(("=", ",")):
                        hits.append(
                            self.hit(
                                rule,
                                src,
                                src.line_of(m.start("name")),
                                fn=name,
                                declared=where,
                                detail=f"{name} returns {kind} by value ({where}); declared {' '.join(words) or 'int'}",
                            )
                        )
                elif name in other and name not in by_value:
                    words = [w for w in re.split(r"[\s]+", m["head"].replace("*", " * ")) if w and w not in _QUALIFIERS]
                    kind = _value_type(words, rule.returns)
                    if kind and not m["head"].rstrip().endswith(("=", ",")):
                        hits.append(
                            self.hit(
                                rule,
                                src,
                                src.line_of(m.start("name")),
                                fn=name,
                                declared=other[name],
                                detail=(
                                    f"{name} is defined with a result pointer ({other[name]});"
                                    f" declared returning {kind} by value"
                                ),
                            )
                        )
        return hits

    def _word_alias(self, rule: Rule) -> list[dict[str, Any]]:
        assert rule.array is not None and rule.narrow is not None
        hits = []
        for label in rule.trees or sorted(self.trees):
            root = self.trees[label]
            symbols = _symbol_table(root / rule.symbols)
            ranges: list[tuple[int, int, str, str]] = []
            for src in self.selected(rule, rule.declare_paths):
                if src.tree != label:
                    continue
                base_match = re.search(r"([0-9A-Fa-f]{8})", Path(src.rel).name)
                pos = int(base_match[1], 16) if base_match else None
                for text in _kept_lines(src.text, rule.defines):
                    for m in rule.array.finditer(text):
                        size = int(m["count"]) if "count" in m.re.groupindex and m["count"] else 0
                        width = 4
                        at = symbols.get(m["datum"], pos)
                        if at is None or not size:
                            continue
                        ranges.append((at, at + width * size, m["datum"], src.rel))
                        pos = at + width * size
            if not ranges:
                continue
            ranges.sort()
            for src in self.selected(rule):
                if src.tree != label:
                    continue
                for number, line in enumerate(src.lines, 1):
                    m = rule.narrow.search(line)
                    if not m or m["datum"] not in symbols:
                        continue
                    at = symbols[m["datum"]]
                    for lo, hi, array, where in ranges:
                        if lo <= at < hi:
                            hits.append(
                                self.hit(
                                    rule,
                                    src,
                                    number,
                                    datum=m["datum"],
                                    declared=f"{where}:{array}",
                                    detail=f"{m['datum']} at {at:08X} lies inside the word array {array} ({where})",
                                )
                            )
                            break
        return hits

    def _call_arity(self, rule: Rule) -> list[dict[str, Any]]:
        defined: dict[str, set[int | None]] = {}
        where: dict[str, str] = {}
        for src in self.selected(rule, rule.declare_paths):
            for line, name, count in src.definitions():
                if rule.callee.fullmatch(name):
                    defined.setdefault(name, set()).add(count)
                    where.setdefault(name, f"{src.rel}:{line}")
        wanted = {n: next(iter(c)) for n, c in defined.items() if len(c) == 1 and None not in c}
        hits = []
        for src in self.selected(rule):
            own = {(line, name) for line, name, _ in src.definitions()}
            for m in _IDENT_CALL.finditer(src.text):
                name = m[1]
                if name not in wanted or not _is_call(src.text, m.start(1)):
                    continue
                counted = _call_args(src.text, m.end() - 1)
                if counted is None:
                    continue
                got, want = counted[0], wanted[name]
                if got < want if rule.mode == "fewer" else got != want:
                    line = src.line_of(m.start(1))
                    if (line, name) in own:
                        continue
                    caller = src.function_at(line)
                    hits.append(
                        self.hit(
                            rule,
                            src,
                            line,
                            fn=name,
                            callers=[caller] if caller else None,
                            declared=where[name],
                            detail=f"{name} takes {want} parameters ({where[name]}), called with {got}",
                        )
                    )
        return hits


def _group(m: re.Match[str], name: str) -> str | None:
    return m[name] if name in m.re.groupindex else None


def fingerprints(hits: list[dict[str, Any]]) -> list[str]:
    """Each hit's `rule|file|fp` key, the n-th repeat of an identical line numbered, for the baseline file."""
    seen: Counter[str] = Counter()
    out = []
    for h in hits:
        base = f"{h['rule']}\t{h['file']}\t{h['fp']}"
        out.append(f"{base}\t{seen[base]}")
        seen[base] += 1
    return out


def read_baseline(path: Path | None) -> set[str]:
    if path is None:
        return set()
    try:
        return {ln for ln in path.read_text().splitlines() if ln and not ln.startswith("#")}
    except OSError:
        return set()


def write_baseline(path: Path, hits: list[dict[str, Any]]) -> int:
    keys = sorted(fingerprints(hits))
    path.write_text(
        "# decomp sweep baseline: hits accepted at the time; a new hit is one not listed here\n"
        "# (rule, file, fingerprint of the line text, repeat number)\n" + "".join(k + "\n" for k in keys)
    )
    return len(keys)


def run(
    rules: list[Rule],
    trees: dict[str, Path],
    *,
    only: list[str] | None = None,
    baseline: Path | None = None,
) -> dict[str, Any]:
    """Run the rules over the trees: {rules: [{id, kind, hits, new}], hits: [...], wall_seconds, files}. Every hit has
    `new` (true when the baseline lacks it)."""
    chosen = [r for r in rules if not only or r.id in only]
    if only and len(chosen) != len(set(only)):
        raise DecompError("sweep.rule", f"no such rule: {', '.join(sorted(set(only) - {r.id for r in chosen}))}")
    began = time.monotonic()
    engine = Sweep(chosen, trees)
    known = read_baseline(baseline)
    hits: list[dict[str, Any]] = []
    summary = []
    for rule in chosen:
        rows = engine.run_rule(rule)
        for h, key in zip(rows, fingerprints(rows), strict=True):
            h["new"] = key not in known
        hits += rows
        summary.append(
            {
                "id": rule.id,
                "kind": rule.kind,
                "severity": rule.severity,
                "hits": len(rows),
                "new": sum(1 for h in rows if h["new"]),
                "why": rule.why,
            }
        )
    return {
        "rules": summary,
        "hits": hits,
        "files": len(engine.sources),
        "wall_seconds": round(time.monotonic() - began, 1),
    }


def to_findings(result: dict[str, Any], trees: dict[str, Path], batch: str | None = None) -> list[dict[str, Any]]:
    """The hits as findings-feed rows (kind `sweep`, signature = `sweep RULE (TREE)`, evidence = absolute `file:line`).
    `fp` is the hit's baseline key, so identical lines of one file stay distinct findings."""
    rows = []
    why = {r["id"]: r["why"] for r in result["rules"]}
    seen: Counter[str] = Counter()
    for h in result["hits"]:
        seen[h["fp"] + h["rule"]] += 1
        again = seen[h["fp"] + h["rule"]] - 1
        row: dict[str, Any] = {
            "kind": "sweep",
            "movie": None,
            "poll": None,
            "signature": f"sweep {h['rule']} ({h['tree']})",
            "evidence": f"{trees[h['tree']] / h['file']}:{h['line']}",
            "rule": h["rule"],
            "tree": h["tree"],
            "text": h["text"],
            "fp": h["fp"] + (f"~{again}" if again else ""),
            "severity": h["severity"],
            "why": why.get(h["rule"], ""),
        }
        for key_name in ("datum", "fn", "callers", "detail", "declared"):
            if h.get(key_name):
                row[key_name] = h[key_name]
        if h.get("in_fn"):
            row["in_fn"] = h["in_fn"]
        if batch:
            row["batch"] = batch
        rows.append(row)
    return rows


def render(result: dict[str, Any], show: int = 5) -> str:
    lines = [f"sweep: {len(result['hits'])} hits over {result['files']} files in {result['wall_seconds']} s"]
    for r in result["rules"]:
        lines.append(f"  {r['id']} [{r['kind']}, {r['severity']}]: {r['hits']} hits, {r['new']} new")
    for r in result["rules"]:
        rows = [h for h in result["hits"] if h["rule"] == r["id"] and h["new"]][:show]
        for h in rows:
            lines.append(f"    {r['id']}: {h['tree']}/{h['file']}:{h['line']}: {h['text'][:100]}")
    return "\n".join(lines)
