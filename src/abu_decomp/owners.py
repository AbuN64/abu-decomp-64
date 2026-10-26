"""`decomp cleanup owners --dir include/COMMON`: the owning module of every declaration of one header folder.

A shared header folder (a dumping ground such as COMMON) dissolves into the headers of the modules that own its
declarations. The owner of a declaration is computed, never claimed:
- a value declaration (an extern, a prototype) belongs to the module of the unit that DEFINES the name (the majority
  module when units of several modules define it, as a twin version does);
- a type, an enum constant, a macro or an inline function belongs to the module whose units spell its names most (the
  MAJORITY USER); a declaration used by units of more than `shared` modules is SHARED and stays in the folder (the
  truly common base types).
The module of a unit is the module map's (`unit` -> `module`, `header`). The result is a table (header, line, kind,
names, owner module, destination header, reason, users); `--packs DIR` writes it as `owners.tsv` and `owners.json`, the
move list a P2b files lane applies with the module headers `recipe structure --map` writes. The reader is deaddecls'
declaration splitter, so it is conservative: a chunk it does not understand is not listed."""

from __future__ import annotations

import csv
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

from abu_decomp import deaddecls

SOURCE_SUFFIXES = (".c", ".cpp", ".cc")
SHARED = 3  # a declaration used by more modules than this stays in the shared folder
DEFINE = re.compile(r"#\s*define\s+(\w+)")


def read_map(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """(unit -> module, module -> header) from a module map file (`unit`, `module`, `header` columns)."""
    unit_module: dict[str, str] = {}
    module_header: dict[str, str] = {}
    with open(path, encoding="utf-8", newline="") as handle:
        lines = (line for line in handle if line.strip() and not line.startswith("#"))
        for row in csv.DictReader(lines, delimiter="\t"):
            if row.get("unit") and row.get("module"):
                unit_module[row["unit"]] = row["module"]
                if row.get("header") and row["header"] != "-":
                    module_header.setdefault(row["module"], row["header"])
    return unit_module, module_header


def sources(root: Path) -> list[str]:
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", "src"], capture_output=True, check=True)
    return sorted(n for n in listed.stdout.decode().split("\0") if n.lower().endswith(SOURCE_SUFFIXES))


def kind_of(chunk: deaddecls.Chunk) -> str:
    """value (extern, prototype, variable), type (typedef, struct, union, enum) or inline (a function body)."""
    text = chunk.text.strip()
    if re.match(r"(typedef|struct|union|enum)\b", text):
        return "type"
    if "{" in text and not re.match(r"extern\b", text):
        return "inline"
    return "value"


def is_definition(chunk: deaddecls.Chunk) -> bool:
    """Whether a source chunk defines (not only declares) its names: a function body or an unqualified variable."""
    text = chunk.text.strip()
    if chunk.opaque or re.match(r"(extern|typedef|struct\s+\w+\s*;|union\s+\w+\s*;|enum\s+\w+\s*;)", text):
        return False
    if "{" in text:
        return not re.match(
            r"(struct|union|enum)\b[^=;]*\{[^}]*\}\s*;", text
        )  # a function, or a variable with an initializer
    head = text.split("=", 1)[0]
    return "=" in text or "(" not in head.replace("(*", "")  # a bare prototype has parentheses, a variable has none


def declarations(root: Path, folder: str) -> list[dict[str, object]]:
    """One row per declaration (and macro) of the folder's headers: header, line, kind, names, the chunk."""
    rows: list[dict[str, object]] = []
    for name in deaddecls.header_files(root, folder):
        text = (root / name).read_text(encoding="latin-1")
        guard = next(iter(DEFINE.findall(text)), "")
        for c in deaddecls.split(deaddecls.strip_comments(text)):
            deaddecls.analyse(c)
            if c.opaque:
                m = DEFINE.match(c.text.strip())
                if m and m.group(1) != guard:
                    rows.append({"header": name, "line": c.first + 1, "kind": "macro", "names": {m.group(1)}})
                continue
            rows.append({"header": name, "line": c.first + 1, "kind": kind_of(c), "names": set(c.defs)})
    return rows


def run(
    root: Path, folder: str, map_path: Path | None = None, packs: Path | None = None, shared: int = SHARED
) -> dict[str, object]:
    unit_module, module_header = read_map(map_path or root / "docs" / "MODULES.tsv")
    rows = declarations(root, folder)
    want = {n for r in rows for n in r["names"]}  # type: ignore[attr-defined]
    value_names = {n for r in rows if r["kind"] == "value" for n in r["names"]}  # type: ignore[attr-defined]
    users: dict[str, set[str]] = {}
    definers: dict[str, set[str]] = {}
    for unit in sources(root):
        text = deaddecls.strip_comments((root / unit).read_bytes().decode("latin-1"))
        words = set(deaddecls.IDENT.findall(text)) & want
        if not words:
            continue
        for w in words:
            users.setdefault(w, set()).add(unit)
        if words & value_names:
            for c in deaddecls.split(text):
                deaddecls.analyse(c)
                if is_definition(c):
                    for n in c.defs & value_names:
                        definers.setdefault(n, set()).add(unit)
    out: list[dict[str, object]] = []
    for r in rows:
        names = sorted(r["names"])  # type: ignore[arg-type]
        defining = {u for n in names for u in definers.get(n, ())}
        using = {u for n in names for u in users.get(n, ())}
        by = defining if r["kind"] == "value" and defining else using
        reason = "definer" if by is defining and defining else "majority user"
        count = Counter(unit_module.get(u, "?") for u in by)
        modules = len(count)
        if not count:
            owner, reason = "", "unused"
        elif modules > shared and reason != "definer":
            owner, reason = "", f"shared by {modules} modules"
        else:
            owner = sorted(count.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        out.append(
            {
                "header": r["header"],
                "line": r["line"],
                "kind": r["kind"],
                "names": names[:6],
                "owner": owner,
                "to": module_header.get(owner, ""),
                "reason": reason,
                "users": len(using),
                "modules": modules,
            }
        )
    moves = [o for o in out if o["owner"] and o["to"]]
    result: dict[str, object] = {
        "folder": folder,
        "headers": len({o["header"] for o in out}),
        "declarations": len(out),
        "owned": len(moves),
        "no_header": sum(1 for o in out if o["owner"] and not o["to"]),
        "shared": sum(1 for o in out if str(o["reason"]).startswith("shared")),
        "unused": sum(1 for o in out if o["reason"] == "unused"),
        "by_owner": dict(Counter(str(o["owner"]) for o in moves).most_common()),
        "rows": out,
    }
    if packs is not None:
        packs.mkdir(parents=True, exist_ok=True)
        with open(packs / "owners.tsv", "w", encoding="utf-8", newline="") as handle:
            w = csv.writer(handle, delimiter="\t", lineterminator="\n")
            w.writerow(["header", "line", "kind", "names", "owner", "to", "reason", "users", "modules"])
            for o in out:
                w.writerow(
                    [
                        o["header"],
                        o["line"],
                        o["kind"],
                        ",".join(o["names"]),
                        o["owner"],
                        o["to"],
                        o["reason"],
                        o["users"],
                        o["modules"],
                    ]
                )  # type: ignore[arg-type]
        (packs / "owners.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    return result
