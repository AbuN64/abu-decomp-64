"""`decomp cleanup preflight`: every bulk generator of the cleanup plan, previewed on one pinned revision, one refusal
list. Each generator runs through its public command in a lane worktree of the revision (nothing is applied, nothing
is written into the project); a refusal is grouped by (tool, code) with a count and three example keys, and every group
is classified as a named defect of a stage S card or an expected residue class. A group no rule matches is
`unclassified`: the run exits 1 and the table below is extended."""

import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

EXAMPLES = 3
GENERATORS = (
    "dead-decls.include",
    "dead-decls.src",
    "owners.include",
    "owners.units",
    "slices",
    "name.auto",
    "name.T",
    "data.place",
    "structure.map",
    "record.rename",
)
# (tool, code regex, class, reference): the first match classifies a group. `defect` names the stage S card that fixes
# it, `residue` the class the Codex wave takes over; the codes are normalised (paths and names become <path>/<key>).
RULES: tuple[tuple[str, str, str, str], ...] = (
    (
        "owners.units",
        r"^no header for (<path>|any module)$",
        "residue",
        "unit-local extern whose module has no header: the Codex wave creates it",
    ),
    (
        "owners.units",
        r"^declared together with other names$",
        "residue",
        "declarations sharing one statement: the Codex wave splits them",
    ),
    (
        "owners.units",
        r"^(declarations conflict|declaration parameter count differs from the definition|definitions differ)$",
        "residue",
        "conflicting prototypes: the S4 residue TSV",
    ),
    (
        "owners.units",
        r"^(include|name) clashes in <path>$",
        "residue",
        "hoist would clash with an include or a name: the S4 residue TSV",
    ),
    ("owners.units", r"^type not visible from <path>$", "defect", "S4: hoist the type with its extern"),
    ("name.auto", r"^strings (unverifiable|duplicate|banned)$", "residue", "string names a reader decides"),
    (
        "name.T",
        r"^(already the original name|named here, not invented: a reader decides"
        r"|<name> is already a name in the project)$",
        "residue",
        "settled or a reader decides",
    ),
    (
        "name.T",
        r"^(member <name> is named in words, the catalog says <name> there: a reader decides"
        r"|not every member is a whole original member, or a pointer is not one)$",
        "residue",
        "member names a reader decides",
    ),
    (
        "name.T",
        r"^accesses of a field have a base of no known record type$",
        "defect",
        "S5.1: scoped record rename by site (0evAlAtaxw2p) types the unknown bases",
    ),
    (
        "data.place",
        r"^no read-only data of its own to place in any version$",
        "defect",
        "S5.3: literal-pool consumers own no data `data place` can move",
    ),
    (
        "structure.map",
        r"^tree not built: <path>$",
        "defect",
        "S1: structure --map needs a built warm tree (build/<v>/link.ld)",
    ),
    (
        "record.rename",
        r"^no scoped record rename command$",
        "defect",
        "S5.1: scoped record rename by site (0evAlAtaxw2p)",
    ),
    ("*", r"^fault: ", "defect", "S0: the generator itself failed"),
)

# Refusal texts that carry a varying tail: cut to their shape so one cause is one group.
SHAPES: tuple[tuple[str, str], ...] = (
    (r"^(type not visible from <path>).*$", r"\1"),
    (
        r"^\d+ of \d+ accesses of \S+ have a base of no known record type.*$",
        "accesses of a field have a base of no known record type",
    ),
    (r"^\S+ is already a name in the project$", "<name> is already a name in the project"),
    (
        r"^member \S+ is named in words, the catalog says \S+ there: a reader decides$",
        "member <name> is named in words, the catalog says <name> there: a reader decides",
    ),
)


@dataclass
class Refusal:
    tool: str
    code: str
    key: str


@dataclass
class Report:
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    refusals: list[Refusal] = field(default_factory=list)


def normal(text: str, key: str = "") -> str:
    """A refusal text without its key: paths and the key itself become placeholders, runs of digits stay."""
    if key:
        text = text.replace(key, "<key>")
    text = re.sub(r"\S*/\S*", "<path>", text)
    text = re.sub(r"<path>( <path>)+", "<path>", text)
    text = re.sub(r"\s+", " ", text).strip()
    for pattern, shape in SHAPES:
        text = re.sub(pattern, shape, text)
    return text


def classify(tool: str, code: str) -> tuple[str, str]:
    for rule_tool, pattern, kind, ref in RULES:
        if rule_tool in (tool, "*") and re.search(pattern, code):
            return kind, ref
    return "unclassified", ""


def groups(refusals: Sequence[Refusal]) -> list[dict[str, Any]]:
    found: dict[tuple[str, str], list[str]] = {}
    for r in refusals:
        found.setdefault((r.tool, r.code), []).append(r.key)
    out = []
    for (tool, code), keys in sorted(found.items()):
        kind, ref = classify(tool, code)
        out.append(
            {"tool": tool, "code": code, "class": kind, "ref": ref, "count": len(keys), "examples": keys[:EXAMPLES]}
        )
    return out


def tsv(rows: Sequence[Mapping[str, Any]]) -> str:
    lines = ["tool\tcode\tclass\tref\tcount\texample1\texample2\texample3"]
    for g in rows:
        ex = [*g["examples"], "", "", ""][:EXAMPLES]
        lines.append("\t".join([g["tool"], g["code"], g["class"], g["ref"], str(g["count"]), *ex]))
    return "\n".join(lines) + "\n"


# -- the generators: each returns (stats, refusals) from the document its public command printed ------------------


def _dead(doc: Mapping[str, Any]) -> tuple[dict[str, Any], list[Refusal]]:
    r = doc.get("result") or {}
    return {"dead": r.get("dead"), "headers": r.get("headers"), "layers": len(r.get("layers", []))}, []


def _owners_include(doc: Mapping[str, Any]) -> tuple[dict[str, Any], list[Refusal]]:
    r = doc.get("result") or {}
    keep = ("declarations", "headers", "owned", "shared", "no_header", "unused")
    return {k: r.get(k) for k in keep}, []


def _slices(doc: Mapping[str, Any]) -> tuple[dict[str, Any], list[Refusal]]:
    r = doc.get("result") or {}
    return {"slices": r.get("slices"), "summary": r.get("summary")}, []


def _auto(doc: Mapping[str, Any]) -> tuple[dict[str, Any], list[Refusal]]:
    stats = {"rows": doc.get("rows"), "by_kind": doc.get("by_kind")}
    found: list[Refusal] = []
    for kind, reasons in (doc.get("refused") or {}).items():
        for reason, count in reasons.items():
            shown = (doc.get("examples") or {}).get(f"{kind} {reason}", [])
            keys = [str(e).split(" ")[0] for e in shown][:EXAMPLES]
            found += [Refusal("name.auto", f"{kind} {reason}", k) for k in keys]
            found += [Refusal("name.auto", f"{kind} {reason}", "") for _ in range(int(count) - len(keys))]
    return stats, found


def _step_t(doc: Mapping[str, Any]) -> tuple[dict[str, Any], list[Refusal]]:
    stats = {k: doc.get(k) for k in ("read", "targets", "files", "moves")}
    keys: dict[str, list[str]] = {}
    for skip in doc.get("skipped") or []:
        keys.setdefault(str(skip.get("why")), []).append(f"{skip.get('tag')}.{skip.get('subject')}")
    found: list[Refusal] = []
    for reason, count in (doc.get("reasons") or {}).items():
        shown = (keys.get(reason, []) + [""] * int(count))[: int(count)]
        found += [Refusal("name.T", normal(reason), key) for key in shown]
    # a skip the reasons table does not count (a field with accesses of no known record type) is a refusal of its own
    found += [
        Refusal("name.T", normal(why), f"{skip.get('tag')}.{skip.get('subject')}")
        for skip in doc.get("skipped") or []
        if (why := str(skip.get("why"))) not in (doc.get("reasons") or {})
    ]
    return stats, found


def _place(doc: Mapping[str, Any]) -> tuple[dict[str, Any], list[Refusal]]:
    """A finding names the functions it refuses in front of its reason ("A B C has no read-only data ...")."""
    findings = doc.get("findings", [])
    found: list[Refusal] = []
    for f in findings:
        names, _, why = str(f.get("message", "")).partition(" has no ")
        found += [Refusal("data.place", normal("no " + why), n) for n in names.split()]
    return {"functions": len((doc.get("result") or {}).get("lines", [])), "findings": len(findings)}, found


def _structure(doc: Mapping[str, Any]) -> tuple[dict[str, Any], list[Refusal]]:
    found = [
        Refusal("structure.map", normal(str(r.get("reason", ""))), str(r.get("module", "")))
        for r in doc.get("refused", [])
    ]
    return {k: doc.get(k) for k in ("moves", "files")} | {"deferred": len(doc.get("deferred", []))}, found


def _units(root: Path) -> tuple[dict[str, Any], list[Refusal]]:
    from abu_decomp import externs

    plan = externs.plan(root, None, None, 300.0, 0.25, externs.stamp_gate(root))
    stats = {k: v for k, v in plan.items() if k not in ("refused", "batches", "gate_unattributed")}
    found = [
        Refusal("owners.units", normal(str(r["reason"])), str(r["id"]))
        for r in cast(list[dict[str, Any]], plan["refused"])
    ]
    return stats, found


def _call(argv: Sequence[str], cwd: Path) -> tuple[dict[str, Any], str]:
    """Run one public command; its JSON document, or a fault line when it printed none."""
    done = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True, check=False)
    try:
        doc = json.loads(done.stdout)
    except ValueError:
        tail = (done.stderr.strip().splitlines() or ["no output"])[-1][:200]
        return {}, f"fault: exit {done.returncode}: {tail}"
    if not isinstance(doc, dict):
        return {}, "fault: output is not a document"
    return doc, ""


def _commands(
    tree: Path, names: Path, scratch: Path, functions: list[str]
) -> dict[str, tuple[list[str], Callable[..., Any]]]:
    decomp = [sys.executable, "-m", "abu_decomp"]
    recipe = [str(Path(sys.executable).parent / "recipe"), "--project", str(tree)]
    cleanup = [*decomp, "cleanup", "--project", str(tree)]
    out = {
        "dead-decls.include": ([*cleanup, "dead-decls", "--dir", "include"], _dead),
        "dead-decls.src": ([*cleanup, "dead-decls", "--dir", "src"], _dead),
        "owners.include": ([*cleanup, "owners", "--dir", "include"], _owners_include),
        "slices": ([*cleanup, "slices", "--names", str(names)], _slices),
        "name.auto": ([*recipe, "name", "--json", "auto", "--out", str(scratch / "auto-rows.jsonl")], _auto),
        "name.T": ([*recipe, "name", "--json", "--step", "T", "next"], _step_t),
        "structure.map": ([*recipe, "structure", "--json", "--map", "docs/MODULES.tsv"], _structure),
    }
    if functions:
        out["data.place"] = ([*decomp, "data", "place", "--tree", str(tree), "--check", *functions], _place)
    return out


def literal_functions(tree: Path, scratch: Path) -> tuple[list[str], str]:
    """The functions that read a literal pool, from one data census of the tree."""
    done = subprocess.run(
        [sys.executable, "-m", "abu_decomp", "data", "census", "--tree", str(tree), "--out", str(scratch / "census")],
        capture_output=True,
        text=True,
        check=False,
    )
    path = scratch / "census" / "census.json"
    if done.returncode or not path.is_file():
        tail = (done.stderr.strip().splitlines() or ["no output"])[-1][:160]
        return [], f"fault: data census exit {done.returncode}: {tail}"
    found = json.loads(path.read_text())
    names = {
        c
        for k in found["clusters"]
        if k.get("class") == "LITERAL_POOL"
        for m in k["members"]
        for c in m.get("consumers", [])
    }
    return sorted(names), ""


def run(tree: Path, scratch: Path) -> Report:
    """Every generator over TREE (a lane worktree of the pinned revision), in preview. SCRATCH holds their files."""
    scratch.mkdir(parents=True, exist_ok=True)
    report = Report()

    def record(tool: str, stats: dict[str, Any], found: list[Refusal], started: float) -> None:
        report.tools[tool] = {"stats": stats, "refusals": len(found), "wall_s": round(time.monotonic() - started, 1)}
        report.refusals += found

    functions, why = literal_functions(tree, scratch)
    if why:
        report.tools["data.place"] = {"stats": {}, "refusals": 1, "wall_s": 0.0}
        report.refusals.append(Refusal("data.place", why, "data census"))
    started = time.monotonic()
    has = subprocess.run([sys.executable, "-m", "abu_decomp", "record", "--help"], capture_output=True, check=False)
    record(
        "record.rename",
        {},
        [] if not has.returncode else [Refusal("record.rename", "no scoped record rename command", "0evAlAtaxw2p")],
        started,
    )
    started = time.monotonic()
    try:
        stats, found = _units(tree)
    except Exception as e:  # a generator that crashes is a refusal of the preflight, not of the run
        stats, found = {}, [Refusal("owners.units", f"fault: {type(e).__name__}: {str(e)[:140]}", "")]
    record("owners.units", stats, found, started)
    for tool, (argv, parse) in _commands(tree, scratch / "names", scratch, functions).items():
        started = time.monotonic()
        doc, why = _call(argv, tree)
        if why:
            record(tool, {}, [Refusal(tool, why, "")], started)
            continue
        stats, found = parse(doc)
        if not found and doc.get("ok") is False:  # a refusal no parser reads: its first finding is the group
            first = (doc.get("findings") or [{}])[0]
            found = [
                Refusal(
                    tool, "fault: " + normal(str(first.get("reason") or first.get("message") or "refused"))[:160], ""
                )
            ]
        record(tool, stats, found, started)
    return report


def document(rev: str, sha: str, report: Report) -> dict[str, Any]:
    rows = groups(report.refusals)
    return {
        "rev": rev,
        "sha": sha,
        "command": f"decomp cleanup preflight --rev {rev}",
        "tools": report.tools,
        "groups": rows,
        "refusals": len(report.refusals),
        "unclassified": sum(g["count"] for g in rows if g["class"] == "unclassified"),
    }


def write(out: Path, rev: str, doc: Mapping[str, Any]) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = [out / f"{rev}.json", out / f"{rev}.tsv"]
    paths[0].write_text(json.dumps(doc, indent=1, sort_keys=False) + "\n")
    paths[1].write_text(tsv(doc["groups"]))
    return paths


__all__ = ["Refusal", "Report", "classify", "document", "groups", "normal", "run", "tsv", "write"]
