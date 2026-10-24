"""`decomp data rewrite`: one raw symbol becomes its owner expression in every C file (externs deleted, uses rewritten,
owner header included). The expression is given, or read from the census (`path` of a member inside a declared owner)."""

from __future__ import annotations

import json
from pathlib import Path

from abu_decomp import DecompError
from abu_decomp.data import cdecl, source
from abu_decomp.data.config import DataConfig


def member_info(census: dict, sym: str) -> dict | None:
    for c in census["clusters"]:
        for m in c["members"]:
            if m["sym"] == sym:
                return m
    return next((m for m in census["lones"] if m["sym"] == sym), None)


def owner_header(census: dict, owner: str) -> str:
    """The include path (below include/) of the header that declares OWNER."""
    m = member_info(census, owner)
    files = [f for f in (m or {}).get("decl_files", []) if f.startswith("include/")]
    return files[0][len("include/") :] if files else ""


def resolve(census_path: Path | None, sym: str, expr: str | None, header: str | None) -> tuple[str, str, dict | None]:
    if expr and header:
        return expr, header, None
    if census_path is None:
        raise DecompError(
            "data.rewrite",
            "give EXPR and HEADER, or --census census.json to read them",
            next=f"decomp data rewrite --census CENSUS.json {sym}",
        )
    census = json.loads(census_path.read_text())
    m = member_info(census, sym)
    if not m or not m.get("path"):
        raise DecompError(
            "data.rewrite",
            f"{sym}: the census has no resolved member path ({(m or {}).get('path_note', 'not a member')})",
        )
    owner = m["path"].split(".")[0].split("[")[0]
    return expr or m["path"], header or owner_header(census, owner), m


def run(
    cfg: DataConfig,
    tree: Path,
    sym: str,
    expr: str,
    header: str,
    only: list[str] | None = None,
    dry: bool = False,
    member: dict | None = None,
    skipped: dict[str, str] | None = None,
) -> list[str]:
    """Files rewritten. With the census MEMBER the per-file safety rules apply: a file they refuse keeps the raw symbol and
    is named with its reason in SKIPPED (the MIXED candidates)."""
    files = [tree / f for f in only] if only else cdecl.source_files(tree, cfg)
    touched = []
    for f in files:
        text = f.read_text(errors="replace")
        new, why = source.plan_file(text, sym, expr, header, f.name == Path(header).name, member)
        if why and skipped is not None:
            skipped[str(f.relative_to(tree))] = why
        if new != text:
            touched.append(str(f.relative_to(tree)))
            if not dry:
                f.write_text(new)
    return touched
