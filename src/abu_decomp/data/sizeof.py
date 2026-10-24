"""sizeof of declared aggregates by the project's own compiler, per version.

A header probe includes the header and asks `__typeof__(SYM)` for every struct/array extern it declares; a unit probe
preprocesses a C unit (so a unit-local struct type is known) and asks the same. The size is read back from the
`.comm probe_SYM,N` the compiler emits. The compiler is called through `run` so tests inject a fake."""

from __future__ import annotations

import re
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from abu_decomp.data.cdecl import decl_kind
from abu_decomp.data.config import DataConfig, expand

Run = Callable[..., "subprocess.CompletedProcess[str]"]
COMM = re.compile(r"\.comm\s+probe_(\w+),(\d+)")


def comm_sizes(
    cfg: DataConfig,
    tree: Path,
    version: str,
    source: str,
    syms: list[str],
    run: Run = subprocess.run,
    scratch: Path | None = None,
) -> dict[str, int]:
    """Preprocess SOURCE (text), append the typeof probes, compile with cc1, return sym -> sizeof."""
    if not syms or not cfg.cc1:
        return {}
    if scratch:
        scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as d:
        c = Path(d) / "probe.c"
        c.write_text(source)
        pre = run([*cfg.cpp, cfg.define(version), str(c)], capture_output=True, text=True, cwd=tree).stdout
        pre += "".join(f"__typeof__({s}) probe_{s};\n" for s in sorted(syms))
        r = run([expand(cfg.cc1), *cfg.cc1_flags, "-o", "-"], input=pre, capture_output=True, text=True, cwd=tree)
    return {m.group(1): int(m.group(2)) for m in COMM.finditer(r.stdout)}


def probe_all(
    cfg: DataConfig,
    tree: Path,
    decl: dict,
    localdef: dict,
    versions: list[str] | None = None,
    run: Run = subprocess.run,
    scratch: Path | None = None,
    only: set[str] | None = None,
):
    """sym -> {version: size} for every aggregate-typed decl in a header and every unit-local aggregate definition."""
    byhdr: dict[str, set[str]] = defaultdict(set)
    byunit: dict[str, set[str]] = defaultdict(set)
    for sym, rows in decl.items():
        for f, t in rows:
            if f.startswith("include/") and decl_kind([t]) & {"s", "a"}:
                byhdr[f].add(sym)
    for sym, rows in localdef.items():
        if sym in decl and any(f.startswith("include/") for f, _ in decl[sym]):
            continue
        for f, t in rows:
            if decl_kind([t]) & {"s", "a"}:
                byunit[f].add(sym)
    sizes: dict[str, dict[str, int]] = defaultdict(dict)
    where: dict[str, str] = {}
    for v in versions or list(cfg.versions):
        for hdr, ss in sorted(byhdr.items()):
            ss = ss & only if only is not None else ss
            body = (f'#include "{cfg.prelude}"\n' if cfg.prelude else "") + f'#include "{hdr[len("include/") :]}"\n'
            for sym, n in comm_sizes(cfg, tree, v, body, sorted(ss), run, scratch).items():
                sizes[sym][v] = n
                where[sym] = "decl:" + hdr
        for unit, ss in sorted(byunit.items()):
            ss = ss & only if only is not None else ss
            if not ss:
                continue
            for sym, n in comm_sizes(
                cfg, tree, v, (tree / unit).read_text(errors="replace"), sorted(ss), run, scratch
            ).items():
                sizes[sym][v] = n
                where.setdefault(sym, "unit:" + unit)
    return dict(sizes), where


def stable(per_version: dict[str, int]) -> bool:
    return len(set(per_version.values())) <= 1
