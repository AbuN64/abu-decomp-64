"""`decomp data census`: load a tree, classify every raw global, write census.json, CENSUS.md and QUEUE.md."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from abu_decomp.data import asm, cdecl, classify, clayout, report, sizeof
from abu_decomp.data.config import DataConfig
from abu_decomp.data.evidence import World


def load_map(tree: Path, cfg: DataConfig, v: str) -> tuple[dict, dict]:
    f = tree / cfg.path(cfg.map_file, v)
    if not f.is_file():
        return {}, {}
    c = cfg.map_columns
    rows = json.loads(f.read_text())["fns"]
    return {r[c["name"]]: r[c["score"]] for r in rows}, {r[c["name"]]: r[c["unit"]] for r in rows}


def load_world(
    tree: Path, cfg: DataConfig, run=subprocess.run, scratch: Path | None = None, sizes: bool = True
) -> World:
    versions = [v for v in cfg.versions if (tree / cfg.path(cfg.symbols_file, v)).is_file()] or list(cfg.versions)
    decl, uses, localdef = cdecl.load_cdecls(tree, cfg)
    size_map, where = sizeof.probe_all(cfg, tree, decl, localdef, versions, run, scratch) if sizes else ({}, {})
    score, unit = load_map(tree, cfg, cfg.reference)
    return World(
        versions=versions, ref=cfg.reference,
        syms={v: asm.load_syms(tree, cfg, v) for v in versions},
        labels={v: asm.load_labels(tree, cfg, v) for v in versions},
        acc={v: asm.load_access(tree, cfg, v) for v in versions},
        decl=decl, uses=uses, sizes=size_map, size_src=where, fnunit=unit, fnscore=score,
    )  # fmt: skip


def owner_type(types: clayout.Types, w: World, owner: str, header: str) -> clayout.CType | None:
    """The owner's type from a decl in HEADER whose computed size reproduces the compiler's sizeof of the owner."""
    want = w.ref_size(owner)
    seen = []
    for f, t in w.decl.get(owner, []):
        if f != header or t in seen:
            continue
        seen.append(t)
        try:
            typ = types.parse_type_text(t)
        except (clayout.LayoutError, ValueError):
            continue
        if typ.kind == "array" and typ.count == 0 and typ.elem and typ.elem.size:  # `T x[]`: the extent is the symbol's
            typ = clayout.array(typ.elem, want // typ.elem.size)
        if typ.size == want:
            return typ
    return None


def resolve_paths(tree: Path, cfg: DataConfig, w: World, data: dict) -> int:
    """Fill `path` ('OWNER.a.b[2]') and `path_note` for every member that lies inside a declared aggregate. A layout is
    used only when it reproduces the compiler's sizeof of the owner, read from the headers the owner's header includes."""
    include = tree / "include"
    done = 0
    cache: dict[str, clayout.CType | None] = {}
    by_header: dict[str, clayout.Types] = {}
    for c in data["clusters"]:
        for m in c["members"]:
            owner, _, off = m["inner"].partition("+")
            if not owner:
                continue
            if owner not in cache:
                cache[owner] = None
                for f, _t in w.decl.get(owner, []):
                    if f.startswith("include/"):
                        rel = f[len("include/") :]
                        if rel not in by_header:
                            by_header[rel] = clayout.read_closure(include, rel)
                        cache[owner] = owner_type(by_header[rel], w, owner, f)
                        if cache[owner] is not None:
                            break
            typ = cache[owner]
            if typ is None:
                m["path"], m["path_note"] = "", "layout-unverified"
                continue
            top = next(iter(m["access"]), "")
            width = int(top.split(":")[0]) if top else 0
            fl = top.split(":")[1].startswith("f") if top else None
            got = clayout.leaf_path(typ, int(off, 16), width, fl)
            m["path"], m["path_note"] = (owner + got[0], got[1]) if got else ("", "no-leaf")
            done += bool(got)
    return done


def run(
    cfg: DataConfig,
    tree: Path,
    out: Path,
    open_fns: set[str] | None = None,
    probe_log: Path | None = None,
    queue: Path | None = None,
    runner=subprocess.run,
    paths: bool = True,
) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    w = load_world(tree, cfg, runner, out / "probe")
    if open_fns is None:
        open_fns = {f for f, s in w.fnscore.items() if s < 100}
    raw = re.compile(cfg.symbol)
    wins = report.parse_probe_log(probe_log.read_text()) if probe_log and probe_log.is_file() else {}
    data = classify.build(w, raw, cfg.data_prefix, open_fns)
    resolved = resolve_paths(tree, cfg, w, data) if paths else 0
    st = classify.stats(w, raw, data, len(w.sizes))
    st["paths_resolved"] = resolved
    data["stats"] = st
    (out / "census.json").write_text(json.dumps(data, indent=1))
    ref_fns: dict[str, list] = {}
    for n in sorted(w.rev(), key=lambda n: w.rev()[n]):
        if n.startswith(cfg.data_prefix):
            for fn in w.fns_v(n, w.ref):
                ref_fns.setdefault(fn, []).append(n)
    (out / "CENSUS.md").write_text(report.render_census(data, st, open_fns, w.fnscore, ref_fns))
    qpath = queue or out / "QUEUE.md"
    prev = report.carried_status(qpath)
    qpath.write_text(report.render_queue(data, open_fns, wins, prev))
    return st
