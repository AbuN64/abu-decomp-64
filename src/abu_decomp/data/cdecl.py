"""C declarations of raw symbols: extern lines (any file), unit-local definitions, uses, and the kind of a type text."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from abu_decomp.data.config import DataConfig

CDECL = re.compile(r"\bextern\s+([^;(){}]*?);", re.S)
# a top-level definition or tentative definition: `[static] [const] TYPE NAME[dims] [= ...];` starting a line
LOCALDEF = r"^(?:static\s+)?(?:const\s+)?((?:struct\s+|union\s+)?\w[\w ]*?\s*\**)\s+(%s)\s*((?:\[[^\]]*\])*)\s*(?:=|;)"
BASE_INT = r"\b(char|s8|u8|s16|u16|s32|u32|int|short|long|s64|u64|unsigned|signed)\b"
BASE_FLT = r"\b(f32|f64|float|double)\b"


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def decl_kind(types: list[str]) -> set[str]:
    """p pointer, a array, f float, i integer, s struct/typedef."""
    ks: set[str] = set()
    for t in types:
        if "*" in t:
            ks.add("p")
        elif "[" in t:
            ks.add("a")
        elif re.search(BASE_FLT, t):
            ks.add("f")
        elif re.search(BASE_INT, t):
            ks.add("i")
        else:
            ks.add("s")
    return ks


def declarators(body: str, token: re.Pattern[str]) -> list[tuple[str, str]]:
    """(symbol, type text) for each declarator of an extern body (the text between `extern` and `;`)."""
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    toks = list(token.finditer(body))
    if not toks:
        return []
    typ = body[: toks[0].start()].strip().rstrip("*").strip()
    out = []
    for i, t in enumerate(toks):
        end = toks[i + 1].start() if i + 1 < len(toks) else len(body)
        pre = body[toks[i - 1].end() : t.start()] if i else body[: t.start()]
        stars = pre.count("*") - (pre.count("*") if i == 0 and "(" in pre else 0)
        suf = body[t.end() : end].split(",")[0].strip()
        out.append((t.group(0), (typ + " " + "*" * stars).strip() + (suf if suf.startswith("[") else "")))
    return out


def source_files(tree: Path, cfg: DataConfig) -> list[Path]:
    out = []
    for d in cfg.source_dirs:
        out += [f for f in sorted((tree / d).rglob("*")) if f.suffix.lower() in cfg.source_suffixes and f.is_file()]
    return out


def load_cdecls(tree: Path, cfg: DataConfig):
    """(decl: sym -> [(file, type)], uses: sym -> {file}, localdef: sym -> [(file, type)]). localdef holds the
    unit-local definitions (`Type D_x;` at file scope): they are not extern decls but their sizeof matters."""
    token = re.compile(r"\b" + cfg.symbol_token)
    decl: dict[str, list] = defaultdict(list)
    uses: dict[str, set] = defaultdict(set)
    localdef: dict[str, list] = defaultdict(list)
    ldef = re.compile(LOCALDEF % cfg.symbol_token, re.M)
    for f in source_files(tree, cfg):
        text = f.read_text(errors="replace")
        if "D_" not in text:
            continue
        rel = str(f.relative_to(tree))
        text = strip_comments(text)
        for t in set(token.findall(text)):
            uses[t].add(rel)
        for m in CDECL.finditer(text):
            for sym, typ in declarators(m.group(1), token):
                decl[sym].append((rel, typ))
        for m in ldef.finditer(text):
            if m.group(1).strip().split()[0] in ("return", "extern", "typedef"):
                continue
            localdef[m.group(2)].append((rel, m.group(1).strip() + m.group(3)))
    return decl, uses, localdef
