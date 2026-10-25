"""`decomp data try`: the fast single-function loop. Put a candidate file (or the unit as it stands) in place of the function's unit,
build only that object, compare only that function, put the unit back: seconds, no round, no stamp, nothing left behind.
Lanes kept re-writing this as private scripts (t2.sh, fast.sh) that vanished with their lane dirs."""

from __future__ import annotations

from pathlib import Path

from abu_decomp.data.trial import Builder, fn_unit, restored


def fncmp_verbose(b: Builder, version: str, fn: str, unit: str) -> str:
    """fncmp's own text with its -v hunks (the config command with `-v` before the first positional template)."""
    obj = b.cfg.object.format(v=version, unit=unit)
    cmd = list(b.cfg.fncmp)
    at = next((i for i, c in enumerate(cmd) if "{v}" in c), len(cmd))
    cmd = [c.format(v=version, fn=fn, obj=obj) for c in [*cmd[:at], "-v", *cmd[at:]]]
    from abu_decomp.data.config import expand

    r = b.run([expand(c) for c in cmd], cwd=b.tree, capture_output=True, text=True)
    return (r.stdout or "") + (r.stderr or "")


def try_fn(
    b: Builder,
    fn: str,
    versions: list[str],
    candidate: Path | None = None,
    unit: str | None = None,
    verbose: bool = False,
) -> list[dict]:
    """One row per version: {version, unit, edit (None: did not build or no count), log (build tail on failure, hunks if verbose)}."""
    rows = []
    for v in versions:
        u = unit or fn_unit(b.cfg, b.tree, v, fn)
        if not u:
            rows.append({"version": v, "unit": None, "edit": None, "log": "function is in no unit of this version"})
            continue
        path = b.tree / u
        with restored(path):
            if candidate is not None:
                path.write_text(candidate.read_text())
            rc, log = b.build(v, u)
            edit = None if rc else b.score(v, fn, u)
            text = log if rc else (fncmp_verbose(b, v, fn, u) if verbose else "")
        rows.append({"version": v, "unit": u, "edit": edit, "log": text})
    return rows
