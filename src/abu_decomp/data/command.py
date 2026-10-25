"""The `decomp data` command: census | probe | rewrite | check | gate (arguments and dispatch only)."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from abu_decomp import DecompError
from abu_decomp.data import census as census_mod
from abu_decomp.data import check as check_mod
from abu_decomp.data import config as config_mod
from abu_decomp.data import fntry, stampbuild, trial
from abu_decomp.data import gate as gate_mod
from abu_decomp.data import rewrite as rewrite_mod
from abu_decomp.data import stamp as stamp_mod

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
    t = sub.add_parser(
        "try",
        help="fast loop: build one function's unit (optionally with a candidate file) and compare only that function",
    )
    t.add_argument("--tree", type=Path, required=True)
    t.add_argument(
        "--candidate", type=Path, help="file whose text stands in for the unit during the try (restored after)"
    )
    t.add_argument("--unit", help="the unit path when the map does not name it")
    t.add_argument("--version", action="append", default=[], help="repeat; default the config reference version")
    t.add_argument("-v", "--verbose", action="store_true", help="print the aligned diff hunks")
    t.add_argument("function")
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
    k.add_argument("--version", action="append", default=[], help="repeat; default every version of the config")
    k.add_argument("--unit", action="append", default=[])
    k.add_argument(
        "--baseline", type=Path, help="JSON {version: {unit: first error}}: units failing the same way are preexisting"
    )
    k.add_argument(
        "--write-baseline", action="store_true", help="run on a clean tree: record every failing unit into --baseline"
    )
    k.add_argument(
        "--since", help="REV the diff was committed on top of: judge everything changed since REV, not the index"
    )
    k.add_argument(
        "--reextract",
        action="store_true",
        help="re-extract a tree whose asm predates splat.yaml changes on main (~450 CPU s)",
    )
    k.add_argument("--diff", type=Path, help="the train diff made from TREE: a pass over every version stamps its hash")
    k.add_argument(
        "--stamps", type=Path, help="stamp directory (default $DECOMP_DATA_STAMPS, then <config dir>/stamps, else ~/.local/state/decomp-data/stamps; never the cwd)"
    )
    u = sub.add_parser("units", help="list every source unit that names a data symbol the tree changed since REV")
    u.add_argument("--tree", type=Path, required=True)
    u.add_argument("--since", help="REV the change was committed on top of (default: the working diff)")
    st = sub.add_parser("stamp", help="refuse a diff that touches declarations or data without a passing check stamp")
    st.add_argument("--diff", type=Path, required=True)
    st.add_argument("--stamps", type=Path)
    st.add_argument(
        "--build", action="store_true", help="no stamp yet: build the diff alone on a fresh warm tree, stamp it"
    )
    st.add_argument("--tree", type=Path, help="--build: the shared tree the fresh tree is seeded from")
    st.add_argument("--at", help="--build: the commit the diff applies on (default: the shared tree's branch tip)")
    pl = sub.add_parser(
        "place", help="place a function's literal pool in the unit that compiles it, in every version (unbake sync --place)"
    )
    pl.add_argument("--tree", type=Path, required=True)
    pl.add_argument("--check", action="store_true", help="list the rows and files it would write, write nothing")
    pl.add_argument("function", nargs="+")
    g = sub.add_parser(
        "gate", help="refuse a diff that re-adds a raw member extern or a kind-conflicting decl; tree ratchet"
    )
    g.add_argument("--census", type=Path, required=True)
    g.add_argument("--diff", type=Path)
    g.add_argument("--tree", type=Path)
    g.add_argument("--stamps", type=Path, help="with --diff: also require a check stamp when the diff touches data")
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
    if verb == "try":
        return _try(args, cfg)
    if verb == "place":
        return _place(args)
    if verb == "check":
        b = trial.Builder(cfg, args.tree)
        b.diff_base = args.since
        # The pool's clean tree: a unit whose rebuilt object equals its object there changed no function's bytes, so it is not
        # scored (removing one declaration from a header 439 units include scored every exact function: 552 CPU s, 10-24).
        from abu_decomp import worktree

        pool = worktree.base(args.tree) / "POOL-SRC"
        if pool.is_dir() and pool.resolve() != args.tree.resolve():
            b.reference = pool
        from abu_decomp import cli

        applied = _apply_diff(args)  # a --diff the tree does not hold yet is applied for the check and taken off again
        try:
            return _check(args, cfg, b, cli)
        finally:
            if applied:
                subprocess.run(
                    ["git", "-C", str(args.tree), "apply", "-R", str(args.diff.resolve())], capture_output=True
                )
    if verb == "units":
        syms = check_mod.changed_symbols(cfg, args.tree, base=args.since)
        return {"symbols": syms, "units": check_mod.referencing_units(cfg, args.tree, syms)}, 0, []
    if verb == "stamp" and args.build:
        from abu_decomp import cli

        if not args.tree:
            raise DecompError("data.stamp", "--build needs --tree SHARED")
        return stampbuild.build(
            cfg,
            cli._profile(args.profile),
            args.tree,
            args.diff,
            stamp_mod.stamp_dir(args.stamps, args.config),
            args.at,
        )
    if verb == "stamp":
        text = args.diff.read_text(errors="replace")
        bad = stamp_mod.missing(cfg, stamp_mod.stamp_dir(args.stamps, args.config), text, args.diff)
        return (
            {"violations": bad},
            1 if bad else 0,
            [{"key": "data.stamp", "blocking": True, "message": m} for m in bad],
        )
    return _gate(args, cfg)


def _apply_diff(args: argparse.Namespace) -> bool:
    """Apply --diff to a clean tree (the lane's tree already holds it when it is dirty): True when this call applied it."""
    if not args.diff or args.write_baseline:
        return False
    dirty = subprocess.run(
        ["git", "-C", str(args.tree), "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True
    ).stdout.strip()
    if dirty:
        return False
    done = subprocess.run(
        ["git", "-C", str(args.tree), "apply", str(args.diff.resolve())], capture_output=True, text=True
    )
    if done.returncode:
        held = subprocess.run(  # the diff reversed applies: the tree already holds it, committed (pass --since REV for its base)
            ["git", "-C", str(args.tree), "apply", "--check", "-R", str(args.diff.resolve())], capture_output=True
        )
        if held.returncode == 0:
            return False
        raise DecompError("data.check", f"{args.diff.name} does not apply to {args.tree}: {done.stderr.strip()[:200]}")
    return True


def _check(args: argparse.Namespace, cfg, b, cli) -> Result:
    why = check_mod.reextract(
        cli._profile(args.profile),
        args.tree,
        check_mod.changed_files(args.tree, args.since),
        always=True,
        allow=args.reextract,
    )
    if why:
        return {}, 1, [{"key": "data.extract", "blocking": True, "message": why}]
    known = (
        json.loads(args.baseline.read_text())
        if args.baseline and args.baseline.is_file() and not args.write_baseline
        else {}
    )
    results = [check_mod.check(b, v, args.unit or None, known.get(v)) for v in (args.version or list(cfg.versions))]
    if args.write_baseline:
        if not args.baseline:
            raise DecompError("data.baseline", "--write-baseline needs --baseline FILE")
        args.baseline.write_text(
            json.dumps({r["version"]: r.get("all_failed", {}) for r in results}, indent=1, sort_keys=True)
        )
    findings = [
        {"key": "data.check", "blocking": True, "message": f"{r['version']}: {u} {fn} {why}"}
        for r in results for u, fn, why in r["broken"]
    ] + [
        {"key": "data.build", "blocking": True, "message": f"{r['version']}: {u} does not compile: {why}"}
        for r in results for u, why in r.get("failed", [])
    ] + [
        {"key": "data.build", "blocking": True, "message": f"{r['version']}: build failed: {'; '.join(r.get('errors', []))}"}
        for r in results if not r["built"] and not r.get("failed")
    ]  # fmt: skip
    out: dict[str, Any] = {"results": results}
    if args.diff and not findings:
        out["stamp"] = _stamp(args, cfg)
    return out, 1 if findings else 0, findings


def _try(args: argparse.Namespace, cfg) -> Result:
    versions = args.version or [cfg.reference]
    rows = fntry.try_fn(trial.Builder(cfg, args.tree), args.function, versions, args.candidate, args.unit, args.verbose)
    lines = [
        f"{args.function} {r['version']} {r['unit']} " + ("NO-SCORE" if r["edit"] is None else f"edit={r['edit']}")
        for r in rows
    ]
    bad = [
        {"key": "data.try", "blocking": True, "message": ln + (" " + r["log"][-400:] if r["edit"] is None else "")}
        for ln, r in zip(lines, rows, strict=True) if r["edit"] != 0
    ]  # fmt: skip
    return {"lines": lines, "rows": rows}, 1 if bad else 0, bad


def _place(args: argparse.Namespace) -> Result:
    """`unbake sync --place`, the one tool that edits the generated place rows (splat.yaml pieces, aliases, link script order) for
    every version: each row it adds or junk row it removes is listed, a refusal names the function and the reason."""
    from abu_decomp import cli, tools

    called = tools.invoke(
        "unbake",
        cli._profile(args.profile),
        ["sync", "--place", *args.function, *(["--check"] if args.check else [])],
        project=args.tree,
    )
    document = called.document or {}
    result = document.get("result") or {}
    placed = result.get("placed") or []
    lines = [
        f"{p['function']} {p['version']}: {p['row']} ({p['file']})"
        + (f"; removed junk rows {', '.join(p['removed'])}" if p.get("removed") else "")
        for p in placed
    ]
    findings = [
        {"key": f.get("key", "data.place"), "blocking": True, "message": f.get("message") or f.get("reason") or ""}
        for f in document.get("findings") or []
        if f.get("blocking", True)
    ]
    if not called.ok and not findings:
        findings = [{"key": "data.place", "blocking": True, "message": "unbake sync --place failed"}]
    return {"lines": lines, "placed": placed, "written": result.get("written", []), "next": result.get("next", "")}, (
        1 if findings else 0
    ), findings


def _stamp(args: argparse.Namespace, cfg) -> str:
    """Write the stamp of --diff after a clean check; the reason it was not written otherwise."""
    import subprocess

    if args.version or args.unit:
        return "not stamped: --version/--unit narrowed the check; a stamp needs every version"
    text = args.diff.read_text(errors="replace")
    done = subprocess.run(
        ["git", "-C", str(args.tree), "diff", "--name-only", *([args.since] if args.since else [])],
        capture_output=True,
        text=True,
    )
    if sorted(done.stdout.split()) != stamp_mod.files_of(text):
        # The tree holds a second diff's edits, so this check cannot vouch for the named diff alone: build it alone on a
        # clean slot of the pool (the --build path) instead of leaving the lane stuck (REV1-ONE-16B, 10-24).
        from abu_decomp import cli, worktree

        pool = worktree.base(args.tree) / "POOL-SRC"
        if pool.is_dir() and pool.resolve() != args.tree.resolve():
            result, code, found = stampbuild.build(
                cfg, cli._profile(args.profile), pool, args.diff, stamp_mod.stamp_dir(args.stamps, args.config)
            )
            if code:
                return "not stamped: the tree holds edits of another diff, so the diff was built alone and failed: " + "; ".join(
                    f["message"] for f in found
                )[:400]
            return str(result.get("stamp"))
        return "not stamped: the tree's changed files are not the diff's files (it still holds edits of an earlier diff, or is on another base). Build the diff alone on a clean main tree: decomp data stamp --diff DIFF --build --tree ~/code/wt/POOL-SRC"
    return str(stamp_mod.write(stamp_mod.stamp_dir(args.stamps, args.config), text))


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
    expr, header, member = rewrite_mod.resolve(args.census, args.symbol, args.expr, args.header)
    if member is None and args.census:
        member = rewrite_mod.member_info(json.loads(args.census.read_text()), args.symbol)
    skipped: dict[str, str] = {}
    files = rewrite_mod.run(cfg, args.tree, args.symbol, expr, header, args.only, args.dry, member, skipped)
    result: dict[str, Any] = {"symbol": args.symbol, "expr": expr, "header": header, "files": files, "skipped": skipped}
    findings: list[dict[str, Any]] = []
    if args.mixed and not args.dry:
        census = json.loads(args.census.read_text()) if args.census else {"clusters": [], "lones": []}
        decl = (rewrite_mod.member_info(census, args.symbol) or {}).get("decl") or ["s32"]
        b = trial.Builder(cfg, args.tree)
        version = args.version or cfg.reference
        units = check_mod.expand_units(args.tree, version, files, b.run)
        res = check_mod.mixed_loop(b, version, args.symbol, expr, decl[0], units)
        result["units"] = units
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
        text = args.diff.read_text(errors="replace")
        bad += gate_mod.check_diff(cfg, args.census, text)
        if args.stamps:
            bad += stamp_mod.missing(cfg, args.stamps, text, args.diff)
    if args.tree:
        n = gate_mod.count_tree(cfg, args.census, args.tree)
        result["raw_member_scalar_decls"] = n
        bad += gate_mod.ratchet(n, args.baseline, args.write_baseline)
    findings = [{"key": "data.gate", "blocking": True, "message": b} for b in bad]
    result["violations"] = bad
    return result, 1 if bad else 0, findings
