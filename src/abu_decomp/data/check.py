"""`decomp data check`: after a type rewrite, build only the changed units and list every exact function in them that
stopped matching (the MIXED finder, seconds instead of a ROM bisect); `--mixed` puts those functions' accesses back."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from abu_decomp.data import source
from abu_decomp.data.config import DataConfig
from abu_decomp.data.trial import Builder, fn_unit  # noqa: F401  re-export for callers


def header_dependents(tree: Path, version: str, headers: list[str], run=subprocess.run) -> list[str]:
    """Units whose dependency file (build/<version>/<unit>.d of an earlier build) names one of HEADERS."""
    if not headers:
        return []
    cmd = ["grep", "-rlF", "--include=*.d", *[a for h in headers for a in ("-e", h)], f"build/{version}"]
    out = run(cmd, cwd=tree, capture_output=True, text=True).stdout.split()
    prefix = f"build/{version}/"
    return sorted(p[len(prefix) : -2] for p in out if p.startswith(prefix) and p.endswith(".d"))


def expand_units(tree: Path, version: str, files: list[str], run=subprocess.run) -> list[str]:
    """The units to rebuild for changed FILES: the changed units themselves, and every unit including a changed header."""
    units = [f for f in files if f.startswith("src/") and not f.lower().endswith(".h")]
    heads = [f for f in files if f.startswith("include/") or f.lower().endswith(".h")]
    return sorted(set(units) | set(header_dependents(tree, version, heads, run)))


def changed_units(tree: Path, run=subprocess.run, version: str = "") -> list[str]:
    out = run(["git", "-C", str(tree), "diff", "--name-only"], capture_output=True, text=True).stdout.split()
    return expand_units(tree, version, out, run) if version else [u for u in out if u.startswith("src/")]


def exact_fns(cfg: DataConfig, tree: Path, version: str, units: list[str]) -> dict[str, list[str]]:
    """unit -> exact (score 100) function names of the version's map."""
    import json

    rows = json.loads((tree / cfg.path(cfg.map_file, version)).read_text())["fns"]
    c = cfg.map_columns
    out: dict[str, list[str]] = {u: [] for u in units}
    for r in rows:
        if r[c["unit"]] in out and r[c["score"]] >= 100:
            out[r[c["unit"]]].append(r[c["name"]])
    return out


def broken(b: Builder, version: str, units: list[str]) -> list[tuple[str, str, str]]:
    """[(unit, fn, why)] for every exact function of UNITS whose rebuilt object no longer scores 0."""
    bad = []
    for unit, fns in exact_fns(b.cfg, b.tree, version, units).items():
        for fn in fns:
            sc = b.score(version, fn, unit)
            if sc != 0:
                bad.append((unit, fn, "edit=%s" % sc if sc is not None else "no-score"))
    return bad


def check(b: Builder, version: str, units: list[str] | None = None) -> dict:
    units = units if units is not None else changed_units(b.tree, b.run, version)
    if not units:
        return {"version": version, "units": 0, "built": True, "broken": []}
    rc, log = b.objects(version, units)
    if rc:
        errs = [x for x in log.splitlines() if re.search(r"error", x, re.I)][:5]
        return {"version": version, "units": len(units), "built": False, "broken": [], "errors": errs}
    bad = broken(b, version, units)
    return {"version": version, "units": len(units), "built": True, "broken": [list(x) for x in bad]}


def mixed_loop(
    b: Builder, version: str, sym: str, expr: str, decl: str, units: list[str] | None = None, rounds: int = 3
) -> dict:
    """rewrite done -> check -> put each broken function's accesses back to the scalar -> check, until clean."""
    units = units if units is not None else changed_units(b.tree, b.run, version)
    logged: list[list[str]] = []
    res = check(b, version, units)
    for _ in range(rounds):
        if not res["built"] or not res["broken"]:
            break
        touched = set()
        for unit, fn, _why in res["broken"]:
            p = b.tree / unit
            text = p.read_text()
            new = source.put_back(text, fn, expr, sym, decl)
            if new != text:
                p.write_text(new)
                touched.add(unit)
                logged.append([sym, fn, unit])
        if not touched:
            break
        res = check(b, version, units)
    res["mixed"] = [f"MIXED {s} {fn} {u}" for s, fn, u in logged]
    return res
