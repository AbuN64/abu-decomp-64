"""`decomp data gate`: no new raw mis-typed globals.

--diff D   every added `extern ... SYM` line of a C/header file in D is checked against the census:
  MEMBER   SYM is a member of a declared owner: write OWNER.member, not an extern (a `MIXED SYM` comment on the same line,
           or an existing decl of SYM that the diff retypes, is a logged exception)
  KIND     the new decl's kind (int/float) contradicts how the assembly accesses SYM
  LITERAL  `extern const f32/f64 SYM` of TU-local rodata, in a diff that drops NON_MATCHING
--tree T   the ratchet: raw member scalar decls in T may not exceed the baseline (a DONE row lowers it)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from abu_decomp.data import cdecl
from abu_decomp.data.config import DataConfig


def load_census(path: Path) -> tuple[dict, dict, set]:
    """(member: sym -> (owner+offset, usable owner expression or ''), access kinds, literal syms)."""
    d = json.loads(path.read_text())
    member: dict[str, tuple[str, str]] = {}
    acc: dict[str, set] = {}
    lit: set[str] = set()
    for c in d["clusters"]:
        for m in c["members"]:
            acc[m["sym"]] = {x.split(":")[1][0] == "f" for x in m["access"]}
            if c["class"] in ("SCALAR_MEMBER", "STRUCT_ALIAS") and m.get("inner"):
                usable = m.get("path", "") if m.get("path_note", "") in ("", "union") else ""
                member[m["sym"]] = (m["inner"], usable)
    for m in d["lones"]:
        acc[m["sym"]] = {x.split(":")[1][0] == "f" for x in m["access"]}
        if m["class"] == "LITERAL":
            lit.add(m["sym"])
    return member, acc, lit


def kind(t: str) -> str:
    return next(iter(cdecl.decl_kind([t])))


def violations(sym: str, typ: str, member: dict, acc: dict, lit: set, line: str, exact: bool) -> list[str]:
    out = []
    k = kind(typ)
    if (
        sym in member and "MIXED" not in line and member[sym][1]
    ):  # no usable owner form (padding, unverified): not refused
        out.append(f"MEMBER {sym} is {member[sym][1]}: write that, not `extern {typ}`")
    a = acc.get(sym)
    if a and k in ("i", "f") and len(a) == 1 and (k == "f") != (True in a):
        out.append(f"KIND {sym} declared {typ} but the ROM accesses it as {'float' if True in a else 'int'}")
    if sym in lit and k == "f" and exact:
        out.append(f"LITERAL {sym} is TU-local rodata: write the literal and delete its asm data row")
    return out


def check_diff(cfg: DataConfig, census: Path, diff_text: str) -> list[str]:
    member, acc, lit = load_census(census)
    token = re.compile(r"\b" + cfg.symbol_token)
    exact = "-#ifdef NON_MATCHING" in diff_text
    removed: dict[str, set] = {}
    cur = None
    bad = []
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("-") and not line.startswith("---") and cur:
            removed.setdefault(cur, set()).update(s for s, _ in _decls(line[1:], token))
    cur = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else line[4:]
            continue
        if (
            not cur
            or not cur.lower().endswith((".c", ".cpp", ".h"))
            or not line.startswith("+")
            or line.startswith("+++")
        ):
            continue
        for sym, typ in _decls(line[1:], token):
            text = line + (
                " MIXED" if sym in removed.get(cur, ()) else ""
            )  # a retyped existing decl: only KIND/LITERAL apply
            bad += [f"{cur}: {v}" for v in violations(sym, typ, member, acc, lit, text, exact)]
    return bad


def _decls(line: str, token: re.Pattern[str]):
    for m in cdecl.CDECL.finditer(cdecl.strip_comments(line)):
        yield from cdecl.declarators(m.group(1), token)


def count_tree(cfg: DataConfig, census: Path, tree: Path) -> int:
    member, _acc, _lit = load_census(census)
    token = re.compile(r"\b" + cfg.symbol_token)
    n = 0
    for f in cdecl.source_files(tree, cfg):
        t = f.read_text(errors="replace")
        if cfg.data_prefix not in t:
            continue
        for m in cdecl.CDECL.finditer(cdecl.strip_comments(t)):
            for sym, typ in cdecl.declarators(m.group(1), token):
                n += sym in member and kind(typ) in ("i", "f", "p")
    return n


def ratchet(count: int, baseline: Path | None, write: bool) -> list[str]:
    if write and baseline:
        baseline.write_text(json.dumps({"raw_member_scalar_decls": count}) + "\n")
        return []
    if baseline and baseline.is_file():
        b = json.loads(baseline.read_text())["raw_member_scalar_decls"]
        if count > b:
            return [f"RATCHET raw member scalar decls {count} > baseline {b}"]
    return []
