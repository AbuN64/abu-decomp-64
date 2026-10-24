"""The `decomp data` command: census | probe | rewrite | check | gate (arguments and dispatch only)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from abu_decomp import DecompError
from abu_decomp.data import census as census_mod
from abu_decomp.data import check as check_mod
from abu_decomp.data import config as config_mod
from abu_decomp.data import gate as gate_mod
from abu_decomp.data import rewrite as rewrite_mod
from abu_decomp.data import trial

Result = tuple[dict[str, Any], int, list[dict[str, Any]]]


def arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config", type=Path, help="data config TOML (default $DECOMP_DATA_CONFIG, then <tree>/docs/data.toml)"
    )
    sub = parser.add_subparsers(dest="verb", required=True)
    c = sub.add_parser("census", help="classify every raw global; write census.json, CENSUS.md and the work QUEUE.md")
    c.add_argument("--tree", type=Path, required=True)
    c.add_argument("--out", type=Path, required=True, help="directory for census.json and CENSUS.md")
    c.add_argument(
        "--queue", type=Path, help="QUEUE.md to regenerate (default OUT/QUEUE.md); its status column is carried over"
    )
    c.add_argument("--open", type=Path, help="file of open function names (default: map score below 100)")
    c.add_argument("--probe", type=Path, help="a `data probe` log: its wins become queue evidence")
    c.add_argument("--no-paths", action="store_true", help="skip the offset -> member path resolution")
    p = sub.add_parser("probe", help="per open function: try the in-struct and literal forms of its raw globals")
    p.add_argument("--tree", type=Path, required=True)
    p.add_argument("--census", type=Path, required=True, help="census.json from `data census`")
    p.add_argument("--version")
    p.add_argument(
        "--edit", action="append", default=[], metavar="OLD=>NEW", help="score one hand edit instead (re:PAT=>REPL ok)"
    )
    p.add_argument(
        "--apply-literals", action="store_true", help="write the function's literal pool constants into its draft"
    )
    p.add_argument("function", nargs="+")
    r = sub.add_parser("rewrite", help="a raw symbol becomes its owner member expression in every C file")
    r.add_argument("--tree", type=Path, required=True)
    r.add_argument("--census", type=Path, help="read EXPR and HEADER from this census.json")
    r.add_argument("--only", nargs="*", help="limit to these files (MIXED exceptions)")
    r.add_argument("--dry", action="store_true")
    r.add_argument(
        "--mixed", action="store_true", help="after the rewrite build, check and put broken functions' accesses back"
    )
    r.add_argument("--version")
    r.add_argument("symbol")
    r.add_argument("expr", nargs="?")
    r.add_argument("header", nargs="?")
    k = sub.add_parser("check", help="build the changed units and list every exact function that stopped matching")
    k.add_argument("--tree", type=Path, required=True)
    k.add_argument("--version", action="append", default=[], help="repeat; default the reference version")
    k.add_argument("--unit", action="append", default=[])
    g = sub.add_parser(
        "gate", help="refuse a diff that re-adds a raw member extern or a kind-conflicting decl; tree ratchet"
    )
    g.add_argument("--census", type=Path, required=True)
    g.add_argument("--diff", type=Path)
    g.add_argument("--tree", type=Path)
    g.add_argument("--baseline", type=Path)
    g.add_argument("--write-baseline", action="store_true")


def _cfg(args: argparse.Namespace):
    return config_mod.load(args.config, getattr(args, "tree", None))


def run(args: argparse.Namespace) -> Result:
    cfg = _cfg(args)
    verb = args.verb
    if verb == "census":
        open_fns = set(args.open.read_text().split()) if args.open else None
        stats = census_mod.run(cfg, args.tree, args.out, open_fns, args.probe, args.queue, paths=not args.no_paths)
        return {"stats": stats, "queue": str(args.queue or args.out / "QUEUE.md")}, 0, []
    if verb == "probe":
        return _probe(args, cfg)
    if verb == "rewrite":
        return _rewrite(args, cfg)
    if verb == "check":
        b = trial.Builder(cfg, args.tree)
        results = [check_mod.check(b, v, args.unit or None) for v in (args.version or [cfg.reference])]
        findings = [
            {"key": "data.check", "blocking": True, "message": f"{r['version']}: {u} {fn} {why}"}
            for r in results for u, fn, why in r["broken"]
        ] + [
            {"key": "data.build", "blocking": True, "message": f"{r['version']}: build failed: {'; '.join(r.get('errors', []))}"}
            for r in results if not r["built"]
        ]  # fmt: skip
        return {"results": results}, 1 if findings else 0, findings
    return _gate(args, cfg)


def _probe(args: argparse.Namespace, cfg) -> Result:
    census = json.loads(args.census.read_text())
    b = trial.Builder(cfg, args.tree)
    version = args.version or cfg.reference
    lines = []
    for fn in args.function:
        unit = trial.fn_unit(cfg, args.tree, version, fn)
        if not unit:
            lines.append(f"{fn} NO-UNIT")
        elif args.apply_literals:
            lines.append(_apply_literals(cfg, args.tree, census, fn, unit))
        elif args.edit:
            lines.append(trial.try_edits(b, version, fn, unit, [tuple(e.split("=>", 1)) for e in args.edit]))
        else:
            lines.append(trial.probe_fn(b, version, fn, unit, census))
    return {"lines": lines}, 0, []


def _apply_literals(cfg, tree: Path, census: dict, fn: str, unit: str) -> str:
    import re

    from abu_decomp.data import literals, source

    info = {m["sym"]: m for c in census["clusters"] for m in c["members"]}
    info.update({m["sym"]: m for m in census["lones"]})
    path = tree / unit
    src = path.read_text()
    sp = source.body_span(src, fn)
    if not sp:
        return f"{fn} NO-BODY"
    use = re.compile(r"(?<![\w&.>])(%s)\b(?!\s*\[)(?!\s*\()" % cfg.symbol_token)
    body = src[sp[0] : sp[1]]
    lits = {s: literals.literal_for(info.get(s)) for s in set(m.group(1) for m in use.finditer(body))}
    lits = {k: v for k, v in lits.items() if v}
    new = src[: sp[0]] + literals.apply(body, lits, use) + src[sp[1] :]
    path.write_text(literals.drop_dead_externs(new, sorted(lits)))
    return f"{fn} literals {len(lits)} {sorted(lits.items())[:3]}"


def _rewrite(args: argparse.Namespace, cfg) -> Result:
    expr, header, _m = rewrite_mod.resolve(args.census, args.symbol, args.expr, args.header)
    files = rewrite_mod.run(cfg, args.tree, args.symbol, expr, header, args.only, args.dry)
    result: dict[str, Any] = {"symbol": args.symbol, "expr": expr, "header": header, "files": files}
    findings: list[dict[str, Any]] = []
    if args.mixed and not args.dry:
        census = json.loads(args.census.read_text()) if args.census else {"clusters": [], "lones": []}
        decl = (rewrite_mod.member_info(census, args.symbol) or {}).get("decl") or ["s32"]
        b = trial.Builder(cfg, args.tree)
        res = check_mod.mixed_loop(b, args.version or cfg.reference, args.symbol, expr, decl[0], files)
        result["check"] = res
        findings = [
            {"key": "data.check", "blocking": True, "message": f"{u} {fn} {why}"} for u, fn, why in res["broken"]
        ]
    return result, 1 if findings else 0, findings


def _gate(args: argparse.Namespace, cfg) -> Result:
    if not args.diff and not args.tree:
        raise DecompError("data.gate", "gate needs --diff DIFF or --tree TREE")
    bad: list[str] = []
    result: dict[str, Any] = {}
    if args.diff:
        bad += gate_mod.check_diff(cfg, args.census, args.diff.read_text(errors="replace"))
    if args.tree:
        n = gate_mod.count_tree(cfg, args.census, args.tree)
        result["raw_member_scalar_decls"] = n
        bad += gate_mod.ratchet(n, args.baseline, args.write_baseline)
    findings = [{"key": "data.gate", "blocking": True, "message": b} for b in bad]
    result["violations"] = bad
    return result, 1 if bad else 0, findings
