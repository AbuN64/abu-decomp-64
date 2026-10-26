"""`decomp data check`: after a type rewrite, build only the changed units and list every exact function in them that
stopped matching (the MIXED finder, seconds instead of a ROM bisect); `--mixed` puts those functions' accesses back."""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
from pathlib import Path

from abu_decomp.data import source
from abu_decomp.data.config import DataConfig
from abu_decomp.data.trial import Builder, fn_unit  # noqa: F401  re-export for callers

_MARK = ".unbake/data-extracted.json"
_SPLAT = re.compile(r"(^|/)splat\.ya?ml$")


def _inputs(tree: Path, run=subprocess.run) -> tuple[str, list[str]]:
    """(sha1 of the working content of every extraction input the tree tracks, their paths)."""
    import hashlib

    from abu_decomp.data import stamp

    names = stamp.extraction_files(
        run(["git", "-C", str(tree), "ls-files"], capture_output=True, text=True).stdout.split()
    )
    h = hashlib.sha1()
    for n in sorted(names):
        with contextlib.suppress(OSError):
            h.update(n.encode() + b"\0" + (tree / n).read_bytes())
    return h.hexdigest(), names


def _moved_splat(tree: Path, mark: dict, run=subprocess.run) -> list[str]:
    """splat configs that differ between the commit the tree's asm was extracted at and its HEAD: a tree moved to a newer
    main re-splits every segment they touch (446 CPU s on the next build, 10-24 REV1-ONE-17)."""
    old = mark.get("head")
    if not old:
        try:
            old = (tree / "build/.pool-extracted-at").read_text().strip()
        except OSError:
            return []
    out = run(["git", "-C", str(tree), "diff", "--name-only", old, "HEAD"], capture_output=True, text=True)
    changed = [f for f in out.stdout.split() if _SPLAT.search(f)] if out.returncode == 0 else []
    return [f for f in changed if not _extracted_from(tree, f)]


def _extracted_from(tree: Path, config: str) -> bool:
    """Whether the build's own record says asm/<version> was extracted from this splat config as it is now: `make extract`
    writes build/.extract-src-<version> (sha1 of the config and the symbol_addrs.txt beside it), so a tree re-extracted by
    make needs no record of ours (10-24 REV1-ONE-25 hand-wrote .unbake/data-extracted.json)."""
    import hashlib

    folder = (tree / config).parent
    try:
        recorded = (tree / "build" / f".extract-src-{folder.name}").read_text().split()[0]
        h = hashlib.sha1((tree / config).read_bytes())
        with contextlib.suppress(OSError):
            h.update((folder / "symbol_addrs.txt").read_bytes())
        return h.hexdigest() == recorded
    except (OSError, IndexError):
        return False


def reextract(
    profile, tree: Path, files: list[str], always: bool = False, allow: bool = False, run=subprocess.run
) -> str | None:
    """Bring the tree's extraction up to the symbol rows it now holds (`unbake setup --no-make`: a rename takes unbake's rename
    path, no splat; a current tree costs ~6 s). Without it fncmp finds no function under its new name (`no-score`: the asm
    still has the old one). `always`: also when no changed file says so, for a tree whose HEAD moved under it (10-24:
    MoveMenuFocus edit=91 in every version on a check tree older than the rename). The tree records the inputs it was
    extracted from (`.unbake/data-extracted.json`): an unchanged set skips the call, and a tree whose HEAD moved over
    splat.yaml changes since its extraction is refused (`allow`: `--reextract`), because the full re-split is ~450 CPU s and
    a fresh pool lease is free. None when nothing needed it or it worked, else why not."""
    from unbake import treelock

    from abu_decomp import tools
    from abu_decomp.data import stamp

    if not always and not stamp.extraction_files(files):
        return None
    now, _names = _inputs(tree, run)
    mark: dict = {}
    with contextlib.suppress(OSError, ValueError):
        mark = json.loads((tree / _MARK).read_text())
    if mark.get("inputs") == now:
        return None
    moved = [] if allow else _moved_splat(tree, mark, run)
    if moved:
        return (
            f"the tree's asm was extracted before {', '.join(moved[:3])} changed on main: re-extracting re-splits every segment "
            "(~450 CPU s). Take a fresh tree (`pool-mint.sh --release LANE` then `--lease LANE`, reapply the diff), "
            "or pass --reextract to pay it here"
        )
    # setup rewrites build/ (link.ld, the Makefile) and asm/: beside a ROM build in this tree it deleted build/eu/link.ld
    # under it (10-25 REV1-ONE-38), so it waits for the tree as `decomp make` holds it
    with treelock.hold(tree, exclusive=True, who="data check setup"):
        called = tools.invoke("unbake", profile, ["setup", "--no-make"], project=tree)
    if not called.ok:
        return "unbake setup refused: the tree's extraction could not be brought current"
    with contextlib.suppress(OSError):
        head = run(["git", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        (tree / _MARK).parent.mkdir(exist_ok=True)
        (tree / _MARK).write_text(json.dumps({"inputs": now, "head": head}))
    return None


def changed_files(tree: Path, base: str | None = None, run=subprocess.run) -> list[str]:
    return run(
        ["git", "-C", str(tree), "diff", "--name-only", base or "HEAD"], capture_output=True, text=True
    ).stdout.split()


def header_dependents(tree: Path, version: str, headers: list[str], run=subprocess.run) -> list[str]:
    """Units whose dependency file (build/<version>/<unit>.d of an earlier build) names one of HEADERS."""
    if not headers:
        return []
    cmd = ["grep", "-rlF", "--include=*.d", *[a for h in headers for a in ("-e", h)], f"build/{version}"]
    out = run(cmd, cwd=tree, capture_output=True, text=True).stdout.split()
    prefix = f"build/{version}/"
    units = (p[len(prefix) : -2] for p in out if p.startswith(prefix) and p.endswith(".d"))
    # a .d of an earlier build outlives its source: a unit moved or retired since (10-25 P2b: src/FRONTEND/... after the
    # move to src/game/) is no unit to rebuild, and asking for it failed the stamp of a diff whose 5-ROM proof passed
    return sorted(u for u in units if (tree / u).is_file())


def expand_units(
    tree: Path,
    version: str,
    files: list[str],
    run=subprocess.run,
    cfg: DataConfig | None = None,
    stats: dict | None = None,
    skipped: dict | None = None,
) -> list[str]:
    """The units to rebuild for changed FILES: the changed units themselves, and every unit including a changed header
    that the header edit can reach (CFG given: `hdrnarrow` drops includers whose compile key the edit cannot change;
    STATS receives what it did, SKIPPED[version] the includers it dropped)."""
    units = [f for f in files if f.startswith("src/") and not f.lower().endswith(".h")]
    heads = [f for f in files if f.startswith("include/") or f.lower().endswith(".h")]
    deps = header_dependents(tree, version, heads, run)
    if cfg is not None and deps:
        from unbake import hdrnarrow  # the build's own narrowing (one copy: unbake owns hdrkey)

        kept, got = hdrnarrow.narrow_tree(tree, version, heads, deps, cfg.object, flags=list(cfg.cpp or []))
        if skipped is not None:
            skipped[version] = sorted(set(deps) - set(kept))
        deps = kept
        if stats is not None:
            stats[version] = got
    return sorted(set(units) | set(deps))


def changed_symbols(cfg: DataConfig, tree: Path, run=subprocess.run, base: str | None = None) -> list[str]:
    """Data symbols named on a changed line of the working diff (a declaration retyped, a use rewritten); with BASE, of
    everything since that revision (a diff applied and committed, as the train holds it)."""
    diff = run(["git", "-C", str(tree), "diff", "-U0", base or "HEAD"], capture_output=True, text=True).stdout
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
    """Exclude function units known to belong only to another version. A function map is not a source inventory:
    data-only units have no row in any version and must still build. Unknown units stay in the check, so a missing
    build rule fails visibly instead of silently dropping the changed source from the stamp. A unit the version's own
    build already made (its object or dependency file is in the tree) stays whatever the maps say: a function ported
    into a unit that only the other versions' maps list (10-25 Libc_SignedDivide, US) is judged where it now lives.
    """

    def mapped(v: str) -> set[str]:
        rows = json.loads((tree / cfg.path(cfg.map_file, v)).read_text())["fns"]
        return {r[cfg.map_columns["unit"]] for r in rows}

    try:
        held = mapped(version)
    except (OSError, ValueError, KeyError):
        return units
    known = set(held)
    for other in cfg.versions:
        if other != version:
            with contextlib.suppress(OSError, ValueError, KeyError):
                known.update(mapped(other))
    obj = cfg.object

    def built(u: str) -> bool:
        path = tree / obj.format(v=version, unit=u)
        return path.exists() or path.with_suffix(".d").exists()

    return [u for u in units if u in held or u not in known or built(u)]


def changed_units(
    tree: Path,
    run=subprocess.run,
    version: str = "",
    base: str | None = None,
    cfg: DataConfig | None = None,
    stats: dict | None = None,
    skipped: dict | None = None,
) -> list[str]:
    out = run(
        ["git", "-C", str(tree), "diff", "--name-only", base or "HEAD"], capture_output=True, text=True
    ).stdout.split()
    if not version:
        return [u for u in out if u.startswith("src/")]
    return expand_units(tree, version, out, run, cfg, stats, skipped)


def moved_units(tree: Path, base: str | None = None, run=subprocess.run) -> dict[str, str]:
    """old unit path -> new path for the source files the working diff renames. The map of a tree that has not synced
    still files the functions under the old path, and the build's dependency files still name it: scoring the old path
    finds no object (stamp of a folder rename: every function "no-score")."""
    out = run(
        ["git", "-C", str(tree), "diff", "--name-status", "-M", base or "HEAD"], capture_output=True, text=True
    ).stdout
    moves = {}
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) == 3 and cols[0].startswith("R") and cols[1].startswith("src/"):
            moves[cols[1]] = cols[2]
        elif len(cols) == 2 and cols[0] == "D" and cols[1].startswith("src/"):
            moves[cols[1]] = ""  # removed
    return moves


def exact_fns(
    cfg: DataConfig, tree: Path, version: str, units: list[str], moved: dict[str, str] | None = None
) -> dict[str, list[str]]:
    """unit -> exact (score 100) function names of the version's map (MOVED: old unit path -> new, for a renamed file)."""
    import json

    moved = moved or {}
    rows = json.loads((tree / cfg.path(cfg.map_file, version)).read_text())["fns"]
    c = cfg.map_columns
    names, by_rom = current_symbols(tree, version)
    out: dict[str, list[str]] = {u: [] for u in units}
    for r in rows:
        unit = moved.get(r[c["unit"]]) or r[c["unit"]]
        if unit in out and r[c["score"]] >= 100:
            name = r[c["name"]]
            if names and name not in names and isinstance(r[1], int):
                name = by_rom.get(r[1], name)  # the map lags a rename: the ROM offset still names the same function
            out[unit].append(name)
    return out


_SYMBOL_ROW = re.compile(r"^(\w+)\s*=\s*0x[0-9A-Fa-f]+\s*;.*?\brom:0x([0-9A-Fa-f]+)", re.M)


def current_symbols(tree: Path, version: str) -> tuple[set[str], dict[int, str]]:
    """(every name, ROM offset -> name of the functions) of the tree's own symbol table of VERSION. The map file is
    written by the progress run and lags a rename; a function asked of fncmp by its stale name is "not found" and the
    check then searched every other unit for it (10-25 CLEAN-P1-EXTERNS4: 212 CPU s on the first version). Resolving by
    ROM offset asks for the name the objects and assembly carry. Empty when the table is not there."""
    path = tree / "versions" / version / "symbol_addrs.txt"
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return set(), {}
    names = set(re.findall(r"(?m)^\s*(\w+)\s*=", text))
    return names, {int(rom, 16): name for name, rom in _SYMBOL_ROW.findall(text)}


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
        getattr(b, "diff_base", None) or "HEAD",
    ]
    out = run([*cmd, "--", ":(glob)**/symbol_addrs*.txt"], capture_output=True, text=True).stdout
    return stamp.renames_of(out) if isinstance(out, str) else {}


def broken(
    b: Builder,
    version: str,
    units: list[str],
    names: dict[str, str] | None = None,
    moved: dict[str, str] | None = None,
) -> list[tuple[str, str, str]]:
    """[(unit, fn, why)] for every exact function of UNITS whose rebuilt object no longer scores 0 (a function the diff
    renames is looked up by its new name; NAMES: old -> new). Every function of the version is asked of ONE fncmp process
    when the builder can batch (`fncmp_batch`): a process start and an objdump per function took 284 s on one header edit."""
    bad = []
    names = names or {}
    wanted: list[tuple[str, str]] = []
    for unit, fns in exact_fns(b.cfg, b.tree, version, units, moved).items():
        if getattr(b, "unchanged", lambda *_: False)(
            version, unit
        ):  # same bytes as the clean build: every exact function there still matches
            continue
        wanted += [(names.get(fn, fn), unit) for fn in fns]
    scores = getattr(b, "scores", None)
    known = scores(version, wanted) if scores is not None and getattr(b.cfg, "fncmp_batch", None) else {}
    for fn, unit in wanted:
        sc = known[(fn, unit)] if (fn, unit) in known else b.score(version, fn, unit)
        if sc is None:
            # the map still files FN under the unit it had (a split-asm unit) while the diff made it C in another one:
            # match by symbol, in the other units this check built (10-24 Render_GetSpatialEffectLevel, US)
            sc = next((s2 for u2 in units if u2 != unit and (s2 := b.score(version, fn, u2)) is not None), None)
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


def _narrow_note(narrowed: dict, version: str) -> dict:
    return {"narrowed": narrowed[version]} if version in narrowed else {}


def plan(b: Builder, version: str) -> dict:
    """What `check` rebuilds for VERSION when no units are given: {"units", "narrowed" (the narrowing's note),
    "skipped" (the includers of the changed headers the narrowing proved the edit cannot reach)}."""
    narrowed: dict = {}
    skipped: dict = {}
    base = getattr(b, "diff_base", None)
    units = changed_units(b.tree, b.run, version, base, b.cfg, narrowed, skipped)
    syms = changed_symbols(b.cfg, b.tree, b.run, base)
    units = sorted(set(units) | set(referencing_units(b.cfg, b.tree, syms, b.run)))
    built = set(units)
    return {"units": units, "narrowed": narrowed, "skipped": [u for u in skipped.get(version, []) if u not in built]}


def prepare(b: Builder, versions: list[str]) -> dict[str, dict]:
    """`plan` of every version (kept on B for `check`), then the narrowed-away includers' stamps signed under the new header bytes
    (`hdrnarrow.sign`) before any make runs: the first make's stamp sweep covers every version and would drop them all
    (same date, other header bytes), so a later build that skips the headers by date (the stamp's ROM check, `make -o`)
    remade every one (10-25: 186-619 CPU s per stamp for 2,000-4,000 units per version the check had proved)."""
    from unbake import hdrnarrow

    plans = {v: plan(b, v) for v in versions}
    obj = b.cfg.object
    stamps = [obj.format(v=v, unit=u)[: -len(".o")] + ".i.ok" for v, p in plans.items() for u in p["skipped"]]
    if stamps and obj.endswith(".o"):
        signed = hdrnarrow.sign(b.tree, stamps)
        for v, p in plans.items():
            p["narrowed"].setdefault(v, {})["signed"] = len(p["skipped"])
        print(f"data check: {signed} stamps the narrowing proved signed under the new header bytes", flush=True)
    b.plans = plans
    return plans


def check(b: Builder, version: str, units: list[str] | None = None, baseline: dict[str, str] | None = None) -> dict:
    """Rebuild the units a working diff reaches in VERSION (changed units, includers of changed headers, and every unit
    naming a changed data symbol) with `make -k`: a unit that fails to compile is listed with its own error and does not
    hide the rest; the exact functions of the units that built are scored. A unit whose error equals its entry in
    BASELINE (unit -> first error, recorded on a clean tree by `--write-baseline`) already failed before the change:
    it is listed as `preexisting` and neither fails the check nor is scored."""
    narrowed: dict = {}
    if units is None:
        got = getattr(b, "plans", {}).get(version) or plan(b, version)  # b.plans: made by `prepare`
        units, narrowed = got["units"], got["narrowed"]
    moved = moved_units(b.tree, getattr(b, "diff_base", None), b.run)
    units = [moved.get(u, u) for u in in_version(b.cfg, b.tree, version, [*units, *moved])]
    units = sorted({u for u in units if u})  # a path the diff removed has no source to build
    if not units:
        return {"version": version, "units": 0, "built": True, "broken": [], **_narrow_note(narrowed, version)}
    rc, log = b.objects(version, units, keep_going=True)
    failed = failures(b, version, units, log)
    old = {u: why for u, why in failed.items() if baseline and baseline.get(u) == why}
    failed = {u: why for u, why in failed.items() if u not in old}
    if (
        rc and not failed and not old
    ):  # make failed with no unit named (a missing rule, a toolchain error): nothing can be judged
        lines = [x for x in log.splitlines() if x.strip()]
        errs = (
            [x for x in lines if re.search(r"error", x, re.I)][:5] or lines[-5:] or [f"make exited {rc} with no output"]
        )
        return {"version": version, "units": len(units), "built": False, "broken": [], "errors": errs}
    bad = broken(b, version, [u for u in units if u not in failed and u not in old], renamed(b), moved)
    return {
        "version": version,
        "units": len(units),
        "built": not failed,
        "broken": [list(x) for x in bad],
        "failed": [[u, why] for u, why in sorted(failed.items())],
        "preexisting": [[u, why] for u, why in sorted(old.items())],
        "all_failed": {**old, **failed},
        **_narrow_note(narrowed, version),
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
