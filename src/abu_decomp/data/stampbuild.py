"""`decomp data stamp --diff X --build --tree SHARED`: build X alone on a fresh warm tree and stamp it.

A stamp used to need `data check` on a lane tree that held the diff, and a lane tree with several diffs rebuilt every unit
all of them changed (8 min wall, 1404 CPU-s with one header in it, 10-24). Here the diff gets its own `decomp worktree new`
tree (extraction, objects and times seeded from SHARED, so make rebuilds only what the diff reaches), `data check` judges
every version, a clean pass writes the stamp, and the tree is removed. The result carries wall and CPU seconds."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from abu_decomp import meter, worktree
from abu_decomp.data import check as check_mod
from abu_decomp.data import stamp as stamp_mod
from abu_decomp.data import trial
from abu_decomp.data.config import DataConfig

CPU_CAP = 300.0


def build(cfg: DataConfig, profile: Any, shared: Path, diff: Path, stamps: Path, at: str | None = None):
    """(result, code, findings). The stamp is written only when every version built and no exact function broke."""
    text = diff.read_text(errors="replace")
    if not stamp_mod.touches_data(cfg, text):
        return {"stamp": "not needed: the diff touches no declaration or data"}, 0, []
    if stamp_mod.stamped(stamps, text):
        return {"stamp": "present"}, 0, []
    started, cpu = time.monotonic(), meter.cpu()
    lane = "STAMP-" + stamp_mod.digest(text)[:10]
    shared = shared.expanduser().resolve()
    worktree.prune(shared, [lane])  # a leftover of an interrupted run
    # The tree is seeded warm from SHARED at SHARED's own commit: any other base differs in symbol facts and splits the
    # ROM again for every version (5 cold extractions, 70 s wall, 10-24). Only a diff that does not apply there goes to the tip.
    head = subprocess.run(["git", "-C", str(shared), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    made, code, findings = worktree.new(profile, shared, lane, at or head or None)
    tree = Path(made["worktree"])
    try:
        if code:
            return {"worktree": made}, code, findings
        applied = subprocess.run(["git", "-C", str(tree), "apply", str(diff.resolve())], capture_output=True, text=True)
        if applied.returncode and not at and head:
            worktree.prune(shared, [lane])
            made, code, findings = worktree.new(profile, shared, lane, None)
            tree = Path(made["worktree"])
            if code:
                return {"worktree": made}, code, findings
            applied = subprocess.run(["git", "-C", str(tree), "apply", str(diff.resolve())], capture_output=True, text=True)
        if applied.returncode:
            msg = f"{diff.name} does not apply on {made['rev'][:9]}: {applied.stderr.strip()[:200]}"
            return {"worktree": made}, 1, [{"key": "data.stamp", "blocking": True, "message": msg}]
        builder = trial.Builder(cfg, tree)
        results = [check_mod.check(builder, v) for v in cfg.versions]
        findings = [
            {"key": "data.check", "blocking": True, "message": f"{r['version']}: {u} {fn} {why}"}
            for r in results for u, fn, why in r["broken"]
        ] + [
            {"key": "data.build", "blocking": True, "message": f"{r['version']}: {u} does not compile: {why}"}
            for r in results for u, why in r.get("failed", [])
        ]  # fmt: skip
        result: dict[str, Any] = {"results": results, "rev": made["rev"]}
        if not findings and all(r["built"] for r in results):
            result["stamp"] = str(stamp_mod.write(stamps, text))
        else:
            result["stamp"] = "not stamped: the check failed"
        return result, 1 if findings else 0, findings
    finally:
        wall, used = time.monotonic() - started, meter.cpu() - cpu
        worktree.prune(shared, [lane])
        print(
            f"stamp build: wall={wall:.0f}s cpu={used:.0f}s" + ("  FLAG CPU-OVER-5min" if used > CPU_CAP else ""),
            flush=True,
        )
