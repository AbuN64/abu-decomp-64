"""`decomp data check`: after a type rewrite, build only the changed units and list every exact function in them that
stopped matching (the MIXED finder, seconds instead of a ROM bisect); `--mixed` puts those functions' accesses back."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from abu_decomp.data import source
from abu_decomp.data.config import DataConfig
from abu_decomp.data.trial import Builder, fn_unit  # noqa: F401  re-export for callers


def reextract(profile, tree: Path, files: list[str], always: bool = False) -> str | None:
    """Bring the tree's extraction up to the symbol rows it now holds (`unbake setup --no-make`: a rename takes unbake's rename
    path, no splat; a current tree costs ~6 s). Without it fncmp finds no function under its new name (`no-score`: the asm
    still has the old one). `always`: also when no changed file says so, for a tree whose HEAD moved under it (10-24:
    MoveMenuFocus edit=91 in every version on a check tree older than the rename). None when nothing needed it or it
    worked, else why not."""
    from abu_decomp import tools
    from abu_decomp.data import stamp

    if not always and not stamp.extraction_files(files):
        return None
    called = tools.invoke("unbake", profile, ["setup", "--no-make"], project=tree)
    return None if called.ok else "unbake setup refused: the tree's extraction could not be brought current"


def changed_files(tree: Path, base: str | None = None, run=subprocess.run) -> list[str]:
    return run(
        ["git", "-C", str(tree), "diff", "--name-only", *([base] if base else [])], capture_output=True, text=True
    ).stdout.split()


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


def changed_symbols(cfg: DataConfig, tree: Path, run=subprocess.run, base: str | None = None) -> list[str]:
    """Data symbols named on a changed line of the working diff (a declaration retyped, a use rewritten); with BASE, of
    everything since that revision (a diff applied and committed, as the train holds it)."""
    diff = run(
        ["git", "-C", str(tree), "diff", "-U0", *([base] if base else [])], capture_output=True, text=True
    ).stdout
    token = re.compile(r"\b" + cfg.symbol_token)
    lines = [x[1:] for x in diff.splitlines() if x[:1] in "+-" and x[:3] not in ("+++", "---")]
    return sorted({m.group(0) for line in lines for m in token.finditer(line)})


def referencing_units(cfg: DataConfig, tree: Path, symbols: list[str], run=subprocess.run) -> list[str]:
    """Every source unit that names one of SYMBOLS, changed or not: a declaration that changed in one file breaks an
    unchanged unit that still declares or uses the old type (10-24 WATCH-1527: a DE unit the check never rebuilt)."""
    if not symbols:
        return []
    suffixes = tuple(cfg.source_suffixes)
    cmd = ["grep", "-rlwF", *[a for s in symbols for a in ("-e", s)], *cfg.source_dirs]
    out = run(cmd, cwd=tree, capture_output=True, text=True).stdout.split()
    return sorted(
        u for u in out if u.lower().endswith(suffixes) and u.startswith("src/") and not u.lower().endswith(".h")
    )


def in_version(cfg: DataConfig, tree: Path, version: str, units: list[str]) -> list[str]:
    """UNITS the version builds (rows of its map): a unit only another version holds has no rule here."""
    try:
        import json

        rows = json.loads((tree / cfg.path(cfg.map_file, version)).read_text())["fns"]
    except (OSError, ValueError, KeyError):
        return units
    held = {r[cfg.map_columns["unit"]] for r in rows}
    return [u for u in units if u in held]


def changed_units(tree: Path, run=subprocess.run, version: str = "", base: str | None = None) -> list[str]:
    out = run(
        ["git", "-C", str(tree), "diff", "--name-only", *([base] if base else [])], capture_output=True, text=True
    ).stdout.split()
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


def renamed(b: Builder) -> dict[str, str]:
    """old name -> new name for the symbol rows the working diff renames at the same address. The map rows of a tree
    that has not synced still carry the old names while the re-extracted asm and the rebuilt objects carry the new ones:
    scoring by the old name finds no function (10-24 REV1-ONE-17: 11-25 false edits on a mixed rename diff)."""
    from abu_decomp.data import stamp

    run = getattr(b, "run", subprocess.run)
    cmd = [
        "git",
        "-C",
        str(b.tree),
        "diff",
        "-U0",
        *([getattr(b, "diff_base", None)] if getattr(b, "diff_base", None) else []),
    ]
    out = run([*cmd, "--", ":(glob)**/symbol_addrs*.txt"], capture_output=True, text=True).stdout
    return stamp.renames_of(out) if isinstance(out, str) else {}


def broken(
    b: Builder, version: str, units: list[str], names: dict[str, str] | None = None
) -> list[tuple[str, str, str]]:
    """[(unit, fn, why)] for every exact function of UNITS whose rebuilt object no longer scores 0 (a function the diff
    renames is looked up by its new name; NAMES: old -> new)."""
    bad = []
    names = names or {}
    for unit, fns in exact_fns(b.cfg, b.tree, version, units).items():
        if getattr(b, "unchanged", lambda *_: False)(
            version, unit
        ):  # same bytes as the clean build: every exact function there still matches
            continue
        for fn in fns:
            fn = names.get(fn, fn)
            sc = b.score(version, fn, unit)
            if sc != 0:
                bad.append((unit, fn, "edit=%s" % sc if sc is not None else "no-score"))
    return bad


def failures(b: Builder, version: str, units: list[str], log: str) -> dict[str, str]:
    """unit -> its first compile error, for each unit a keep-going build left without an object."""
    out: dict[str, str] = {}
    for unit in units:
        obj = b.cfg.object.format(v=version, unit=unit)
        # make names the failed rule, which is the object or one of its intermediates (`<unit>.i.ok`): the unit path is enough
        if not any("***" in line and "Error" in line and (obj in line or unit in line) for line in log.splitlines()):
            continue
        mine = [x for x in log.splitlines() if unit in x and re.search(r"error", x, re.I)]
        out[unit] = (mine or [x for x in log.splitlines() if re.search(r"error", x, re.I)] or ["build failed"])[
            0
        ].strip()[:300]
    return out


def check(b: Builder, version: str, units: list[str] | None = None, baseline: dict[str, str] | None = None) -> dict:
    """Rebuild the units a working diff reaches in VERSION (changed units, includers of changed headers, and every unit
    naming a changed data symbol) with `make -k`: a unit that fails to compile is listed with its own error and does not
    hide the rest; the exact functions of the units that built are scored. A unit whose error equals its entry in
    BASELINE (unit -> first error, recorded on a clean tree by `--write-baseline`) already failed before the change:
    it is listed as `preexisting` and neither fails the check nor is scored."""
    if units is None:
        base = getattr(b, "diff_base", None)
        units = changed_units(b.tree, b.run, version, base)
        syms = changed_symbols(b.cfg, b.tree, b.run, base)
        units = sorted(set(units) | set(referencing_units(b.cfg, b.tree, syms, b.run)))
    units = in_version(b.cfg, b.tree, version, units)
    if not units:
        return {"version": version, "units": 0, "built": True, "broken": []}
    rc, log = b.objects(version, units, keep_going=True)
    failed = failures(b, version, units, log)
    old = {u: why for u, why in failed.items() if baseline and baseline.get(u) == why}
    failed = {u: why for u, why in failed.items() if u not in old}
    if (
        rc and not failed and not old
    ):  # make failed with no unit named (a missing rule, a toolchain error): nothing can be judged
        errs = [x for x in log.splitlines() if re.search(r"error", x, re.I)][:5]
        return {"version": version, "units": len(units), "built": False, "broken": [], "errors": errs}
    bad = broken(b, version, [u for u in units if u not in failed and u not in old], renamed(b))
    return {
        "version": version,
        "units": len(units),
        "built": not failed,
        "broken": [list(x) for x in bad],
        "failed": [[u, why] for u, why in sorted(failed.items())],
        "preexisting": [[u, why] for u, why in sorted(old.items())],
        "all_failed": {**old, **failed},
        **({"errors": [f"{u}: {why}" for u, why in sorted(failed.items())][:8]} if failed else {}),
    }


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
