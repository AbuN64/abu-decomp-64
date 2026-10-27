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
import difflib
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
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True)
    for name in sorted(n for n in listed.stdout.decode().split("\0") if n and deaddecls.under(n, folder)):
        if not name.lower().endswith(deaddecls.C_SUFFIXES):
            continue
        text = (root / name).read_text(encoding="latin-1")
        guard = next(iter(DEFINE.findall(text)), "")
        for c in deaddecls.split(deaddecls.strip_comments(text)):
            deaddecls.analyse(c)
            if c.opaque:
                m = DEFINE.match(c.text.strip())
                if m and m.group(1) != guard:
                    rows.append(
                        {
                            "header": name,
                            "line": c.first + 1,
                            "last": c.last + 1,
                            "kind": "macro",
                            "names": {m.group(1)},
                        }
                    )
                continue
            rows.append(
                {"header": name, "line": c.first + 1, "last": c.last + 1, "kind": kind_of(c), "names": set(c.defs)}
            )
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
                "last": r["last"],
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
        result["move_packs"] = move_packs(root, moves, packs)
    return result


def include_path(header: str) -> str:
    """The `#include` spelling of a header path of the tree (the include/ folder is the search root)."""
    return header.removeprefix("include/")


def move_packs(root: Path, moves: list[dict[str, object]], packs: Path) -> list[dict[str, object]]:
    """One diff per owning module (`moves-NN-<module>.diff`, biggest first), each over the tree the earlier ones leave
    (apply them in order): the owned declarations leave their COMMON header, a `#include` of the module header takes the
    place of the first one removed from that header (an includer sees the same names in the same order), and the
    declarations are appended to the module header before its closing `#endif`. A module whose header the tree does
    not have yet is skipped (`recipe structure --map` writes the module headers first). The packs only move text; the
    train's check and the five-ROM proof decide whether the new header order still compiles."""
    texts: dict[str, list[str]] = {}

    def lines(path: str) -> list[str]:
        if path not in texts:
            texts[path] = (root / path).read_text(encoding="latin-1").splitlines(keepends=True)
        return texts[path]

    by_owner: dict[str, list[dict[str, object]]] = {}
    for m in moves:
        if (root / str(m["to"])).is_file() and m["header"] != m["to"]:
            by_owner.setdefault(str(m["owner"]), []).append(m)
    written: list[dict[str, object]] = []
    for number, (owner, mine) in enumerate(sorted(by_owner.items(), key=lambda kv: (-len(kv[1]), kv[0])), 1):
        before = {path: list(text) for path, text in texts.items()}
        touched: set[str] = set()
        dest = str(mine[0]["to"])
        moved: list[str] = []
        by_header: dict[str, list[dict[str, object]]] = {}
        for m in mine:
            by_header.setdefault(str(m["header"]), []).append(m)
        for header, parts in sorted(by_header.items()):
            text = lines(header)
            before.setdefault(header, list(text))
            spans = sorted((int(m["line"]) - 1, int(m["last"])) for m in parts)
            moved += ["".join(text[a:b]) for a, b in spans]
            keep: list[str] = []
            at = 0
            for a, b in spans:
                keep += text[at:a]
                if at == 0 or not any(f'#include "{include_path(dest)}"' in line for line in keep):
                    keep.append(f'#include "{include_path(dest)}"\n')
                at = b
            keep += text[at:]
            texts[header] = keep
            touched.add(header)
        target = lines(dest)
        before.setdefault(dest, list(target))
        end = max((i for i, line in enumerate(target) if line.lstrip().startswith("#endif")), default=len(target))
        texts[dest] = [*target[:end], "\n", *("".join(moved).splitlines(keepends=True)), *target[end:]]
        touched.add(dest)
        diff = []
        for path in sorted(touched):
            diff += difflib.unified_diff(before[path], texts[path], f"a/{path}", f"b/{path}")
        name = f"moves-{number:02d}-{re.sub(r'[^A-Za-z0-9_.-]', '_', owner)}.diff"
        (packs / name).write_text("".join(diff), encoding="latin-1")
        written.append(
            {"pack": name, "owner": owner, "to": dest, "declarations": len(mine), "headers": len(touched) - 1}
        )
    return written
