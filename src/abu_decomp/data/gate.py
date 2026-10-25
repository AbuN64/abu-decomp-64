"""`decomp data gate`: no new raw mis-typed globals.

--diff D   every added `extern ... SYM` line of a C/header file in D is checked against the census:
  MEMBER   SYM is a member of a declared owner: write OWNER.member, not an extern (a `MIXED SYM` comment on the same line,
           or an existing decl of SYM that the diff retypes, is a logged exception)
  KIND     the new decl's kind (int/float) contradicts how the assembly accesses SYM
  LITERAL  `extern const f32/f64 SYM` of TU-local rodata, in a diff that drops NON_MATCHING
--tree T   the ratchet: raw member scalar decls in T may not exceed the baseline (a DONE row lowers it)."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path

from abu_decomp.data import cdecl
from abu_decomp.data.config import DataConfig


def load_census(path: Path) -> tuple[dict, dict, set]:
    """(member: sym -> (owner+offset, usable owner expression or ''), access kinds, literal syms)."""
    return _load(path)[:3]


def _load(path: Path) -> tuple[dict, dict, set, dict]:
    """load_census plus `labels`: member sym -> every symbol its cluster is told apart by (base, owner, the members)."""
    d = json.loads(path.read_text())
    labels: dict[str, frozenset] = {}
    member: dict[str, tuple[str, str]] = {}
    acc: dict[str, set] = {}
    lit: set[str] = set()
    for c in d["clusters"]:
        names = {c["base"], *(m["sym"] for m in c["members"])}
        names |= {m["inner"].split("+")[0] for m in c["members"] if m.get("inner")}
        for m in c["members"]:
            labels[m["sym"]] = frozenset(names)
            acc[m["sym"]] = {x.split(":")[1][0] == "f" for x in m["access"]}
            if c["class"] in ("SCALAR_MEMBER", "STRUCT_ALIAS") and m.get("inner"):
                usable = m.get("path", "") if m.get("path_note", "") in ("", "union") else ""
                member[m["sym"]] = (m["inner"], usable)
    for m in d["lones"]:
        acc[m["sym"]] = {x.split(":")[1][0] == "f" for x in m["access"]}
        if m["class"] == "LITERAL":
            lit.add(m["sym"])
    return member, acc, lit, labels


_LUI = re.compile(r"\blui\s+\$(\w+),\s*%hi\(([A-Za-z_]\w*)")
_MEM = re.compile(
    r"\b(?:[ls][bhwd]u?|[ls]wc1|[ls]dc1|l[wd]l|l[wd]r|s[wd]l|s[wd]r)\s+\$\w+,\s*%lo\(([A-Za-z_]\w*)[^)]*\)\(\$(\w+)\)"
)


def rom_evidence(text: str, sym: str, labels: frozenset) -> str:
    """How one function's assembly reaches `sym`: 'separate' when the ROM gave it its own lui and the function also loads
    another label of its cluster (or `sym` again) with its own lui (a struct base is loaded once and offset: gcc shares it),
    'offset' when a load or store names `sym` through a base register loaded for a different label (owner + offset),
    '' when this function says nothing."""
    base: dict[str, str] = {}
    own = 0
    offset = has_own = False
    for line in text.splitlines():
        m = _LUI.search(line)
        if m:
            base[m.group(1)] = m.group(2)
            own += m.group(2) in labels
            has_own |= m.group(2) == sym
            continue
        m = _MEM.search(line)
        if m and m.group(1) == sym and base.get(m.group(2), sym) != sym:
            offset = True
    if offset:
        return "offset"
    return "separate" if has_own and own >= 2 else ""


class Evidence:
    """Reads the reference version's function assembly of an evidence tree (config `evidence_tree`)."""

    def __init__(self, cfg: DataConfig, labels: dict):
        self.labels = labels
        tree = os.path.expanduser(cfg.evidence_tree) if cfg.evidence_tree else ""
        self.root = Path(tree) / cfg.path(cfg.fn_asm, cfg.reference) if tree else None
        self._index: dict[str, list[Path]] | None = None

    def _files_of(self, sym: str) -> list[Path]:
        """The functions that name `sym`; one pass over the assembly indexes every symbol for the whole gate run."""
        if self._index is None:
            token = re.compile(rb"%(?:hi|lo)\(([A-Za-z_]\w*)")
            self._index = {}
            for f in sorted(self.root.glob("*/*.s")):
                for name in set(token.findall(f.read_bytes())):
                    self._index.setdefault(name.decode(), []).append(f)
        return self._index.get(sym, [])

    def separate(self, sym: str) -> bool:
        """True when the ROM addresses `sym` directly in some function and as owner + offset in none."""
        if self.root is None or sym not in self.labels or not self.root.is_dir():
            return False
        seen = {rom_evidence(f.read_text(errors="replace"), sym, self.labels[sym]) for f in self._files_of(sym)}
        return "separate" in seen and "offset" not in seen


def kind(t: str) -> str:
    return next(iter(cdecl.decl_kind([t])))


def violations(
    sym: str,
    typ: str,
    member: dict,
    acc: dict,
    lit: set,
    line: str,
    exact: bool,
    separate: Callable[[str], bool] = lambda s: False,
) -> list[str]:
    out = []
    k = kind(typ)
    if (
        sym in member and "MIXED" not in line and member[sym][1] and not separate(sym)
    ):  # no usable owner form (padding, unverified), or the ROM gives it its own lui (separate in the original): not refused
        out.append(f"MEMBER {sym} is {member[sym][1]}: write that, not `extern {typ}`")
    a = acc.get(sym)
    if a and k in ("i", "f") and len(a) == 1 and (k == "f") != (True in a):
        out.append(f"KIND {sym} declared {typ} but the ROM accesses it as {'float' if True in a else 'int'}")
    if sym in lit and k == "f" and exact:
        out.append(f"LITERAL {sym} is TU-local rodata: write the literal and delete its asm data row")
    return out


def check_diff(cfg: DataConfig, census: Path, diff_text: str) -> list[str]:
    member, acc, lit, labels = _load(census)
    evidence = Evidence(cfg, labels)
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
            bad += [f"{cur}: {v}" for v in violations(sym, typ, member, acc, lit, text, exact, evidence.separate)]
    return bad


def _decls(line: str, token: re.Pattern[str]):
    for m in cdecl.CDECL.finditer(cdecl.strip_comments(line)):
        yield from cdecl.declarators(m.group(1), token)


def count_tree(cfg: DataConfig, census: Path, tree: Path) -> int:
    member, _acc, _lit, labels = _load(census)
    evidence = Evidence(cfg, labels)
    token = re.compile(r"\b" + cfg.symbol_token)
    n = 0
    for f in cdecl.source_files(tree, cfg):
        t = f.read_text(errors="replace")
        if cfg.data_prefix not in t:
            continue
        for m in cdecl.CDECL.finditer(cdecl.strip_comments(t)):
            for sym, typ in cdecl.declarators(m.group(1), token):
                n += sym in member and kind(typ) in ("i", "f", "p") and not (member[sym][1] and evidence.separate(sym))
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
