"""One census of every function's parameter list: its definition, every prototype and call site in the source, and
what the assembly of the original reads.

A function defined with one parameter list and declared or called with another makes the port pass wrong arguments
and fault far from the cause. The census reads the C sources once per file content (a content-keyed cache in the state
directory, so a repeat run only reads the files that changed) and the original's assembly once per file (a0-a3, f12/f14
and stack words read before they are written, along every path). Per symbol it says `agreed`, `disagree` (which sites)
or `asm` (the sites agree but the assembly reads more arguments than the definition has), and the gate refuses a
change that adds a disagreement or touches a disagreeing function without reducing it.

Limits, stated so a verdict is read for what it is: the asm count is a lower bound (a function may ignore an argument,
a tail call passes registers on, a jump table ends a path); call sites are counted textually, so a macro that expands
to several arguments is one; functions already matched have no assembly file, so they carry no asm count.
"""

from __future__ import annotations

import collections
import hashlib
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from abu_decomp import DecompError
from abu_decomp.behave import prototype

DEFAULTS: dict[str, Any] = {
    "dirs": ["src", "include"],
    "suffixes": (".c", ".cpp", ".h"),
    "symbols": r"func_[0-9A-Fa-f]{8}\w*",
    "asm": "asm/us-rev1/nonmatchings",
}
VERSION = 2
KEYWORDS = frozenset(
    {"return", "else", "do", "goto", "case", "sizeof", "if", "while", "for", "switch", "typedef", "define"}
)
_LEX = re.compile(r"/\*.*?\*/|//[^\n]*|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'", re.S)
_PRE_TYPE = re.compile(r"^[\w\s\*]+$")


class ArityError(DecompError):
    pass


# --- source scan -------------------------------------------------------------------------------------------------


def _blank(match: re.Match[str]) -> str:
    return re.sub(r"[^\n]", " ", match.group(0))


def clean(text: str) -> str:
    """The text with comments, strings and preprocessor lines blanked (offsets and newlines kept)."""
    text = _LEX.sub(_blank, text)
    out, continued = [], False
    for line in text.split("\n"):
        if continued or line.lstrip().startswith("#"):
            continued = line.rstrip().endswith("\\")
            out.append(" " * len(line))
        else:
            out.append(line)
    return "\n".join(out)


def _args(inner: str) -> list[str]:
    rows = prototype.split_arguments(inner)
    return [] if len(rows) == 1 and rows[0] in ("", "void") else rows


def rows_of(text: str, symbols: re.Pattern[str]) -> list[list[Any]]:
    """[symbol, 'd'|'p'|'c', line, argument count, parameter texts, body digest] for each use of a matching name."""
    flat = clean(text)
    rows: list[list[Any]] = []
    pattern = re.compile(rf"(?<![\w.>$])({symbols.pattern})\s*\(")
    for match in pattern.finditer(flat):
        depth, end = 1, match.end()
        while end < len(flat) and depth and end - match.end() < 6000:
            char = flat[end]
            if char in ";{}" and depth:
                depth = -1
                break
            depth += {"(": 1, ")": -1}.get(char, 0)
            end += 1
        if depth:
            continue
        inner = flat[match.end() : end - 1]
        args = _args(inner)
        line = flat.count("\n", 0, match.start()) + 1
        start = max(
            flat.rfind(";", 0, match.start()), flat.rfind("{", 0, match.start()), flat.rfind("}", 0, match.start())
        )
        pre = flat[start + 1 : match.start()].strip()
        after = flat[end:].lstrip()
        typed = (
            bool(pre)
            and _PRE_TYPE.match(pre) is not None
            and not (set(pre.split()) & KEYWORDS)
            and any(re.match(r"[A-Za-z_]", word) for word in pre.replace("*", " ").split())
        )
        name = match.group(1)
        if typed and after.startswith("{"):
            body = _body(flat, end + (len(flat[end:]) - len(after)))
            rows.append(
                [name, "d", line, len(args), args, hashlib.sha1(re.sub(r"\s+", " ", body).encode()).hexdigest()[:12]]
            )
        elif typed and (after[:1] in (";", ",") or after.startswith("__attribute__")):
            rows.append([name, "p", line, len(args), args, ""])
        else:
            rows.append([name, "c", line, len(args), [], ""])
    return rows


def _body(flat: str, brace: int) -> str:
    depth, i = 0, brace
    while i < len(flat):
        depth += {"{": 1, "}": -1}.get(flat[i], 0)
        i += 1
        if depth == 0:
            break
    return flat[brace:i]


# --- assembly ----------------------------------------------------------------------------------------------------

_INSN = re.compile(r"^\s*/\*.*?\*/\s+(\w[\w.]*)\s*(.*?)\s*(?:#.*)?$")
_LABEL = re.compile(r"^\s*(\.L\w+|jtbl\w*):\s*$")
_ARGREG = {"$a0": 0, "$a1": 1, "$a2": 2, "$a3": 3}
_FARG = {"$f12": "f12", "$f13": "f12", "$f14": "f14", "$f15": "f14"}
_STORE = frozenset({"sb", "sh", "sw", "sd", "swl", "swr", "sc", "swc1", "sdc1", "swc2", "sdc2", "sdl", "sdr"})
_LOAD = frozenset(
    {"lb", "lbu", "lh", "lhu", "lw", "lwu", "ld", "lwl", "lwr", "ll", "ldl", "ldr", "lwc1", "ldc1", "lwc2"}
)
_BRANCH = frozenset(
    {"beq", "bne", "beql", "bnel", "blez", "bgtz", "bltz", "bgez", "blezl", "bgtzl", "bltzl", "bgezl", "beqz", "bnez"}
    | {"bc1f", "bc1t", "bc1fl", "bc1tl"}
)
_LIKELY = frozenset({"beql", "bnel", "blezl", "bgtzl", "bltzl", "bgezl", "bc1fl", "bc1tl"})
_READ_ALL = frozenset(
    {"mult", "multu", "div", "divu", "dmult", "dmultu", "ddiv", "ddivu", "mtlo", "mthi", "mtc1", "dmtc1", "ctc1"}
)
_REGS = re.compile(r"\$\w+")
_MEM = re.compile(r"^(-?\w+)\((\$\w+)\)$")


def _split(operands: str) -> list[str]:
    return [o.strip() for o in operands.split(",") if o.strip()]


def _effect(op: str, ops: list[str]) -> tuple[list[str], list[str], int | None]:
    """(registers read, registers written, offset from $sp when the access is a load off the stack pointer)."""
    reads: list[str] = []
    writes: list[str] = []
    stack: int | None = None

    def reg(token: str) -> list[str]:
        mem = _MEM.match(token)
        return [mem.group(2)] if mem else _REGS.findall(token)

    if op in _STORE:
        for token in ops:
            reads += reg(token)
    elif op in _LOAD:
        if ops:
            writes += reg(ops[0])
        for token in ops[1:]:
            reads += reg(token)
            mem = _MEM.match(token)
            if mem and mem.group(2) == "$sp":
                try:
                    stack = int(mem.group(1), 0)
                except ValueError:
                    stack = None
    elif op in _BRANCH or op in _READ_ALL:
        for token in ops:
            reads += reg(token) if not token.startswith(".") else []
        if op in ("mtc1", "dmtc1", "ctc1") and len(ops) > 1:
            reads, writes = reg(ops[0]), reg(ops[1])
    elif op in ("mflo", "mfhi", "mfc1", "dmfc1", "cfc1", "lui", "li", "la"):
        if ops:
            writes += reg(ops[0])
        for token in ops[1:]:
            reads += reg(token)
    elif op.startswith("c.") and "." in op[2:]:
        for token in ops:
            reads += reg(token)
    elif ops:
        writes += reg(ops[0])
        for token in ops[1:]:
            reads += reg(token)
    return reads, writes, stack


def parse_asm(text: str) -> dict[str, Any]:
    """{"words": n, "regs": [...], "stack": [...], "tail": bool, "indirect": bool}: the argument words the function
    reads before it writes them, along every path (a lower bound: see the module note)."""
    ins: list[tuple[str, list[str]]] = []
    labels: dict[str, int] = {}
    for line in text.splitlines():
        found = _INSN.match(line)
        if found:
            ins.append((found.group(1), _split(found.group(2))))
            continue
        label = _LABEL.match(line)
        if label:
            labels[label.group(1)] = len(ins)
    frame = 0
    for op, ops in ins:
        if op == "addiu" and len(ops) == 3 and ops[0] == "$sp" and ops[1] == "$sp":
            try:
                value = int(ops[2], 0)
            except ValueError:
                continue
            if value < 0:
                frame = -value
                break
    used: set[str] = set()
    stack_words: set[int] = set()
    flags = {"tail": False, "indirect": False}
    seen: set[tuple[int, frozenset[str]]] = set()
    work: list[tuple[int, frozenset[str]]] = [(0, frozenset())]

    def step(index: int, defined: set[str]) -> None:
        op, ops = ins[index]
        reads, writes, stack = _effect(op, ops)
        for r in reads:
            key = f"a{_ARGREG[r]}" if r in _ARGREG else _FARG.get(r)
            if key and key not in defined:
                used.add(key)
        if stack is not None and stack >= frame + 0x10:
            stack_words.add((stack - frame) // 4)
        for w in writes:
            key = f"a{_ARGREG[w]}" if w in _ARGREG else _FARG.get(w)
            if key:
                defined.add(key)

    while work:
        pc, defined_in = work.pop()
        while 0 <= pc < len(ins):
            state = (pc, defined_in)
            if state in seen:
                break
            seen.add(state)
            defined = set(defined_in)
            op, ops = ins[pc]
            is_branch = op in _BRANCH or op in ("j", "jal", "jr", "jalr", "b", "bal")
            if not is_branch:
                step(pc, defined)
                defined_in = frozenset(defined)
                pc += 1
                continue
            step(pc, defined)  # the branch's own reads (its register operands)
            target = ops[-1] if ops else ""
            after = set(defined)
            if pc + 1 < len(ins):
                step(pc + 1, after)  # the delay slot runs before the transfer
            fall = pc + 2
            if op in ("jal", "jalr", "bal"):
                after |= {"a0", "a1", "a2", "a3", "f12", "f14"}
                defined_in = frozenset(after)
                pc = fall
                continue
            if op == "jr":
                if ops and ops[0] != "$ra":
                    flags["indirect"] = True
                break
            if op in ("j", "b"):
                if target in labels:
                    pc, defined_in = labels[target], frozenset(after)
                    continue
                flags["tail"] = True
                break
            # conditional branch: taken edge with the delay slot, fall-through edge (likely: without it)
            if target in labels:
                work.append((labels[target], frozenset(after)))
            defined_in = frozenset(defined if op in _LIKELY else after)
            pc = fall
    words = 0
    for i in range(4):
        if f"a{i}" in used:
            words = i + 1
    if "f12" in used:
        words = max(words, 1)
    if "f14" in used:
        words = max(words, 2)
    if stack_words:
        words = max(words, max(stack_words) + 1)
    regs = sorted(used)
    return {"words": words, "regs": regs, "stack": sorted(stack_words), **flags}


# --- cache -------------------------------------------------------------------------------------------------------


class Cache:
    """Per-file rows keyed by content digest, a stat index to skip reading unchanged files, and asm results."""

    def __init__(self, folder: Path | None) -> None:
        self.path = folder / "census.json" if folder else None
        self.stat: dict[str, list[Any]] = {}
        self.rows: dict[str, list[list[Any]]] = {}
        self.asm: dict[str, list[Any]] = {}
        self.dirty = False
        if self.path and self.path.is_file():
            try:
                data = json.loads(self.path.read_text())
                if data.get("v") == VERSION and data.get("symbols") is not None:
                    self.stat, self.rows, self.asm = data["stat"], data["rows"], data["asm"]
                    self.symbols = data["symbols"]
            except (OSError, ValueError, KeyError):
                self.stat, self.rows, self.asm = {}, {}, {}
        self.symbols = getattr(self, "symbols", None)

    def check(self, symbols: str) -> None:
        if self.symbols != symbols:
            self.stat, self.rows, self.asm, self.symbols, self.dirty = {}, {}, {}, symbols, True

    def save(self) -> None:
        if not self.path or not self.dirty:
            return
        keep = {sha for _, _, sha in self.stat.values()}
        rows = {sha: r for sha, r in self.rows.items() if sha in keep}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps({"v": VERSION, "symbols": self.symbols, "stat": self.stat, "rows": rows, "asm": self.asm})
        )
        tmp.replace(self.path)
        self.dirty = False

    def for_text(self, text: str, symbols: re.Pattern[str]) -> list[list[Any]]:
        sha = hashlib.sha1(text.encode("utf-8", "surrogateescape")).hexdigest()
        if sha not in self.rows:
            self.rows[sha] = rows_of(text, symbols)
            self.dirty = True
        return self.rows[sha]


def settings(given: dict[str, Any] | None) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(given or {})
    cfg["suffixes"] = tuple(s.lower() for s in cfg["suffixes"])
    return cfg


def tree_rows(project: Path, cfg: dict[str, Any], cache: Cache) -> dict[str, list[list[Any]]]:
    """relative path -> rows, for every source file under the configured directories."""
    symbols = re.compile(cfg["symbols"])
    cache.check(cfg["symbols"])
    out: dict[str, list[list[Any]]] = {}
    for directory in cfg["dirs"]:
        for base, _, names in os.walk(project / directory):
            for name in names:
                if not name.lower().endswith(cfg["suffixes"]):
                    continue
                full = Path(base) / name
                relative = str(full.relative_to(project))
                info = full.stat()
                known = cache.stat.get(relative)
                if known and known[0] == info.st_size and known[1] == info.st_mtime_ns and known[2] in cache.rows:
                    out[relative] = cache.rows[known[2]]
                    continue
                text = full.read_text(errors="surrogateescape")
                rows = cache.for_text(text, symbols)
                cache.stat[relative] = [
                    info.st_size,
                    info.st_mtime_ns,
                    hashlib.sha1(text.encode("utf-8", "surrogateescape")).hexdigest(),
                ]
                cache.dirty = True
                out[relative] = rows
    return out


def asm_counts(project: Path, cfg: dict[str, Any], cache: Cache, wanted: set[str]) -> dict[str, dict[str, Any]]:
    root = project / cfg["asm"]
    out: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return out
    for base, _, names in os.walk(root):
        for name in names:
            if not name.endswith(".s") or name[:-2] not in wanted:
                continue
            full = Path(base) / name
            info = full.stat()
            key = str(full.relative_to(project))
            known = cache.asm.get(key)
            if known and known[0] == info.st_size and known[1] == info.st_mtime_ns:
                out[name[:-2]] = known[2]
                continue
            result = parse_asm(full.read_text(errors="replace"))
            cache.asm[key] = [info.st_size, info.st_mtime_ns, result]
            cache.dirty = True
            out[name[:-2]] = result
    return out


def aliases_of(project: Path, cfg: dict[str, Any]) -> dict[str, str]:
    texts = []
    for base, _, names in os.walk(project / "include"):
        for name in names:
            if name.lower().endswith((".h",)):
                texts.append((Path(base) / name).read_text(errors="replace"))
    return prototype.scan_aliases(texts)


# --- census ------------------------------------------------------------------------------------------------------


def argument_words(kinds: list[str]) -> int:
    """The argument words a parameter list takes under O32: a 64-bit value takes two words and starts at an even
    word, so one after an odd count leaves a hole (s32, s64 is 4 words, not 3: the asm reads a0 and a2/a3)."""
    words = 0
    for kind in kinds:
        if kind in ("int64", "uint64", "double"):
            words += words % 2 + 2
        else:
            words += 1
    return words


def build(rows: dict[str, list[list[Any]]], asm: dict[str, dict[str, Any]], aliases: dict[str, str]) -> dict[str, Any]:
    """The per-symbol verdicts from per-file rows."""
    table: dict[str, dict[str, Any]] = {}
    for file in sorted(rows):
        for name, kind, line, count, params, digest in rows[file]:
            entry = table.setdefault(name, {"defs": [], "protos": [], "calls": []})
            site = {"file": file, "line": line, "n": count}
            if kind == "d":
                kinds = [prototype._param(p, i, aliases).kind for i, p in enumerate(params)]
                words = argument_words(kinds)
                entry["defs"].append({**site, "words": words, "blind": "struct" in kinds, "digest": digest})
            elif kind == "p":
                entry["protos"].append(site)
            else:
                entry["calls"].append(site)
    for name, entry in table.items():
        entry["asm"] = asm.get(name)
        definition = entry["defs"][0] if entry["defs"] else None
        entry["def"] = definition
        mismatches: list[dict[str, Any]] = []
        if definition:
            for other in entry["defs"][1:]:
                if other["n"] != definition["n"]:
                    mismatches.append({**other, "kind": "definition"})
            for kind, key in (("prototype", "protos"), ("call", "calls")):
                for site in entry[key]:
                    if site["n"] != definition["n"]:
                        mismatches.append(
                            {**site, "kind": kind, "direction": "fewer" if site["n"] < definition["n"] else "more"}
                        )
        entry["mismatches"] = mismatches
        short = bool(
            definition and entry["asm"] and not definition["blind"] and entry["asm"]["words"] > definition["words"]
        )
        entry["asm_short"] = short
        entry["score"] = len(mismatches) + (1 if short else 0)
        entry["verdict"] = "nodef" if not definition else "disagree" if mismatches else "asm" if short else "agreed"
    return table


def totals(table: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {"symbols": len(table), "agreed": 0, "disagree": 0, "asm": 0, "nodef": 0}
    for entry in table.values():
        counts[entry["verdict"]] += 1
    counts["under_arity_calls"] = sum(
        1 for e in table.values() for m in e["mismatches"] if m["kind"] == "call" and m["direction"] == "fewer"
    )
    return counts


class Census:
    def __init__(self, project: Path, given: dict[str, Any] | None = None, state: Path | None = None) -> None:
        self.project, self.cfg = project, settings(given)
        self.cache = Cache(state)
        self.rows = tree_rows(project, self.cfg, self.cache)
        self.aliases = aliases_of(project, self.cfg)

    def table(self, rows: dict[str, list[list[Any]]] | None = None) -> dict[str, Any]:
        rows = self.rows if rows is None else rows
        names = {row[0] for file_rows in rows.values() for row in file_rows}
        return build(rows, asm_counts(self.project, self.cfg, self.cache, names), self.aliases)

    def close(self) -> None:
        self.cache.save()


def describe_site(site: dict[str, Any]) -> str:
    kind = site.get("kind", "definition")
    return f"{site['file']}:{site['line']} {kind} with {site['n']}"


def explain(name: str, entry: dict[str, Any]) -> str:
    definition = entry.get("def")
    asm = entry.get("asm")
    head = (
        f"{name}: defined with {definition['n']} at {definition['file']}:{definition['line']}"
        if definition
        else f"{name}: no definition"
    )
    sites = "; ".join(describe_site(m) for m in entry["mismatches"][:6])
    more = f" (+{len(entry['mismatches']) - 6} more)" if len(entry["mismatches"]) > 6 else ""
    tail = f"; sites that disagree: {sites}{more}" if sites else ""
    reads = (
        f"; asm reads {asm['words']} argument word(s) ({', '.join(asm['regs']) or 'none'})"
        + (" and passes registers on by a tail call" if asm.get("tail") else "")
        if asm
        else "; no asm (the function is already matched)"
    )
    return head + tail + reads


def next_command(profile_file: Path | str | None, name: str, kinds: list[str]) -> str:
    """One command to start the fix: read what the function's callers and its assembly disagree on."""
    profile = f" --profile {profile_file}" if profile_file else ""
    return f"decomp{profile} arity show {name}"


# --- gate --------------------------------------------------------------------------------------------------------


def judge(
    base: dict[str, Any], new: dict[str, Any], touched: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(refused, checked): a touched symbol is refused when its disagreement grew, or stays and was not reduced."""
    refused: list[dict[str, Any]] = []
    checked: list[dict[str, Any]] = []
    for name in sorted(touched):
        after = new.get(name)
        if after is None:
            continue
        before = base.get(name)
        was = before["score"] if before else 0
        now = after["score"]
        row = {"function": name, "base": was, "new": now}
        if now > was:
            row["why"] = f"adds {now - was} disagreement(s) between definition, declarations, calls and assembly"
        elif now and now >= was:
            row["why"] = "touches a function whose sites disagree without reducing them"
        else:
            checked.append(row)
            continue
        row["detail"] = explain(name, after)
        row["sites"] = [describe_site(m) for m in after["mismatches"]]
        row["site_files"] = [m["file"] for m in after["mismatches"]]
        row["kinds"] = sorted({m["kind"] for m in after["mismatches"]})
        refused.append(row)
    return refused, checked


def touched_symbols(
    base_rows: dict[str, list[list[Any]]],
    new_rows: dict[str, list[list[Any]]],
    changed: list[str],
    new_table: dict[str, Any],
) -> set[str]:
    """Symbols whose definition body, or any site that disagrees, was added or changed in the changed files."""
    touched: set[str] = set()
    for file in changed:
        old = collections.Counter(_identity(r) for r in base_rows.get(file, []))
        now = collections.Counter(_identity(r) for r in new_rows.get(file, []))
        for row in (old - now) + (now - old):
            name, kind = row[0], row[1]
            if kind == "d":
                touched.add(name)
                continue
            entry = new_table.get(name)
            definition = entry and entry["def"]
            if definition is None or row[2] != definition["n"]:
                touched.add(name)
    return touched


def _identity(row: list[Any]) -> tuple[Any, ...]:
    """A row without its line number and parameter text: moving a use is not changing it."""
    return (row[0], row[1], row[3], row[5])


def check_change(
    project: Path,
    given: dict[str, Any] | None,
    changed: list[str],
    base_text: Callable[[str], str | None],
    state: Path | None = None,
) -> dict[str, Any]:
    """Compare the tree at `project` with its base for the `changed` files (the base's text from `base_text`)."""
    census = Census(project, given, state)
    try:
        symbols = re.compile(census.cfg["symbols"])
        suffixes = census.cfg["suffixes"]
        base_rows = dict(census.rows)
        for file in changed:
            if not file.lower().endswith(suffixes):
                continue
            text = base_text(file)
            if text is None:
                base_rows.pop(file, None)
            else:
                base_rows[file] = census.cache.for_text(text, symbols)
        new_table, base_table = census.table(), census.table(base_rows)
        tracked = [f for f in changed if f.lower().endswith(suffixes)]
        original = {f: base_rows.get(f, []) for f in tracked}
        touched = touched_symbols(original, census.rows, tracked, new_table)
        refused, checked = judge(base_table, new_table, touched)
        for row in refused:
            sites = set(row.pop("site_files"))
            row["file"] = next(
                (f for f in tracked if f in sites),
                next((f for f in tracked if any(r[0] == row["function"] for r in original[f])), tracked[0]),
            )
        return {"refused": refused, "checked": checked, "counts": totals(new_table), "base_counts": totals(base_table)}
    finally:
        census.close()


# --- the command ---------------------------------------------------------------------------------------------------


def census_for(profile: Any, project: Path | None = None) -> Census:
    """The census of the profile's project (or `project`), configured by `land.arity` when the seat has it."""
    from abu_decomp import tools
    from abu_decomp.behave.run import configuration, work_dir

    root = tools.project_dir(profile, project)
    try:
        given = profile.section("land").get("arity")
    except DecompError:
        given = None
    return Census(root, given, work_dir(configuration(profile)) / "arity")


def run(profile: Any, verb: str, name: str | None, project: Path | None, base: str | None, top: int) -> dict[str, Any]:
    census = census_for(profile, project)
    try:
        if verb == "gate":
            return gate_worktree(census, base or "HEAD")
        table = census.table()
        if verb == "show":
            entry = table.get(name or "")
            if entry is None:
                raise ArityError(
                    "arity.unknown", f"{name}: no definition, declaration or call of that name in {census.project}"
                )
            kinds = sorted({m["kind"] for m in entry["mismatches"]})
            return {
                "function": name,
                "verdict": entry["verdict"],
                "summary": explain(name or "", entry),
                "definition": entry["def"],
                "asm": entry["asm"],
                "prototypes": entry["protos"],
                "calls": entry["calls"],
                "mismatches": entry["mismatches"],
                "next": next_command(profile.source_file, name or "", kinds) if entry["score"] else None,
            }
        counts = totals(table)
        worst = sorted((e["score"], n) for n, e in table.items() if e["score"])[::-1][:top]
        return {
            "counts": counts,
            "project": str(census.project),
            "worst": [
                {"function": n, "verdict": table[n]["verdict"], "disagreements": s, "summary": explain(n, table[n])}
                for s, n in worst
            ],
        }
    finally:
        census.close()


def gate_worktree(census: Census, base: str) -> dict[str, Any]:
    """The land gate on the working tree: files that differ from `base` (a git revision) against their base text."""
    import subprocess

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-c", "core.quotepath=off", *args], cwd=census.project, capture_output=True, text=True, check=False
        )

    listing = git("diff", base, "--name-only", "--no-renames")
    others = git("ls-files", "--others", "--exclude-standard")
    changed = sorted(set(listing.stdout.split("\n")) | set(others.stdout.split("\n")) - {""})
    changed = [f for f in changed if f and f.lower().endswith(census.cfg["suffixes"])]
    census.close()
    report = check_change(
        census.project,
        census.cfg,
        changed,
        lambda f: (lambda p: p.stdout if p.returncode == 0 else None)(git("show", f"{base}:{f}")),
        census.cache.path.parent if census.cache.path else None,
    )
    return {"base": base, "changed": len(changed), "pass": not report["refused"], **report}


# --- triage --------------------------------------------------------------------------------------------------------

_FUNCTION = re.compile(r"\bfunc_[0-9A-Fa-f]{8}\w*")


def notes(profile: Any, names: list[str], project: Path | None = None) -> list[dict[str, Any]]:
    """One row per name whose definition, declarations, calls and assembly disagree (never raises)."""
    try:
        census = census_for(profile, project)
        try:
            table = census.table()
        finally:
            census.close()
    except (DecompError, OSError, ValueError, KeyError):
        return []
    rows = []
    for name in dict.fromkeys(names):
        entry = table.get(name)
        if entry and entry["score"]:
            kinds = sorted({m["kind"] for m in entry["mismatches"]})
            rows.append(
                {
                    "function": name,
                    "verdict": entry["verdict"],
                    "summary": explain(name, entry),
                    "first": "fix this parameter-list disagreement first: a wrong argument faults far from the cause",
                    "next": next_command(profile.source_file, name, kinds),
                }
            )
    return rows


def frame_names(paths: list[Path | str], limit: int = 12) -> list[str]:
    """The first function names in gdb stack files (the failing stack first)."""
    names: list[str] = []
    for path in paths:
        try:
            text = Path(path).read_text(errors="replace")
        except OSError:
            continue
        names += _FUNCTION.findall(text)[:limit]
    return names


def definition_file(profile: Any, name: str) -> Path | None:
    """The source file that defines `name` in the profile's project (None when unknown)."""
    try:
        census = census_for(profile)
        try:
            for file, rows in census.rows.items():
                if any(r[0] == name and r[1] == "d" for r in rows):
                    return census.project / file
        finally:
            census.close()
    except (DecompError, OSError, ValueError, KeyError):
        return None
    return None
