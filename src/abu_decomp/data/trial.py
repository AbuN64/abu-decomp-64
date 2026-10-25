"""Build one unit, score one function, and the greedy in-struct / literal probe (`decomp data probe`).

A probe edit is an experiment, never a landing: `S` -> `((struct{__typeof__(S) v;}*)&S)->v` makes the compiler treat
the access as a member access (MEM_IN_STRUCT), so a score drop says the ROM reads S as part of an aggregate. The unit is
written, built and always restored (also on SIGTERM)."""

from __future__ import annotations

import filecmp
import json
import os
import re
import signal
import subprocess
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from abu_decomp.data import literals, source
from abu_decomp.data.config import DataConfig, expand

Run = Callable[..., "subprocess.CompletedProcess[str]"]


class Builder:
    def __init__(self, cfg: DataConfig, tree: Path, run: Run = subprocess.run, reference: Path | None = None) -> None:
        self.cfg, self.tree, self.run, self.reference = cfg, tree, run, reference
        # reference: a clean tree built at the same commit. A unit whose rebuilt object is byte-identical to its object
        # there cannot have changed any function's score, so the check does not score it (a decl-only diff scored 576
        # units x 5 versions, ~450 CPU s, for nothing).

    def unchanged(self, version: str, unit: str) -> bool:
        """The unit's object here is byte-identical to the reference tree's."""
        if self.reference is None:
            return False
        rel = self.cfg.object.format(v=version, unit=unit)
        try:
            return filecmp.cmp(self.tree / rel, self.reference / rel, shallow=False)
        except OSError:
            return False

    def jobs(self) -> str:
        if not self.cfg.jobs:
            return "1"
        out = self.run([expand(self.cfg.jobs[0]), *self.cfg.jobs[1:]], capture_output=True, text=True).stdout.strip()
        return out or "1"

    def build(self, version: str, unit: str) -> tuple[int, str]:
        obj = self.cfg.object.format(v=version, unit=unit)
        env = dict(os.environ, **{k: expand(v) for k, v in self.cfg.make_env.items()})
        r = self.run(
            ["nice", *map(expand, self.cfg.make), f"-j{self.jobs()}", obj],
            cwd=self.tree,
            capture_output=True,
            text=True,
            env=env,
        )
        return r.returncode, (r.stdout or "")[-3000:] + (r.stderr or "")[-3000:]

    def objects(self, version: str, units: list[str], keep_going: bool = False) -> tuple[int, str]:
        env = dict(os.environ, **{k: expand(v) for k, v in self.cfg.make_env.items()})
        objs = [self.cfg.object.format(v=version, unit=u) for u in units]
        r = self.run(
            ["nice", *map(expand, self.cfg.make), f"-j{self.jobs()}", *(["-k"] if keep_going else []), *objs],
            cwd=self.tree,
            capture_output=True,
            text=True,
            env=env,
        )
        return r.returncode, (r.stdout or "")[-60000:] + (r.stderr or "")[-60000:]

    def score(self, version: str, fn: str, unit: str) -> int | None:
        """fncmp edit count of FN's object (0 = exact), None when fncmp printed no count."""
        obj = self.cfg.object.format(v=version, unit=unit)
        cmd = [expand(c.format(v=version, fn=fn, obj=obj)) for c in self.cfg.fncmp]
        out = self.run(cmd, cwd=self.tree, capture_output=True, text=True).stdout
        m = re.search(self.cfg.fncmp_score, out)
        return int(m.group(1)) if m else None


@contextmanager
def restored(path: Path):
    """Write-restore guard for a unit: the original text comes back on exit and on SIGTERM."""
    orig = path.read_text()
    old = signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(SystemExit(1)))
    try:
        yield orig
    finally:
        path.write_text(orig)
        signal.signal(signal.SIGTERM, old)


def build_score(b: Builder, version: str, fn: str, unit: str, text: str) -> int | None:
    (b.tree / unit).write_text(text)
    rc, _log = b.build(version, unit)
    return None if rc else b.score(version, fn, unit)


def probe_fn(b: Builder, version: str, fn: str, unit: str, census: dict) -> str:
    """One line: `FN base=N final=M kept=[...] tried=K` (the format `census --probe` reads back)."""
    info = {m["sym"]: m for c in census["clusters"] for m in c["members"]}
    info.update({m["sym"]: m for m in census["lones"]})
    use = re.compile(r"(?<![\w&.>])(%s)\b(?!\s*\[)(?!\s*\()" % b.cfg.symbol_token)
    path = b.tree / unit
    with restored(path) as orig:
        src = source.unwrap(orig, fn)
        sp = source.body_span(src, fn)
        if not sp:
            return f"{fn} NO-BODY"
        head, body, tail = src[: sp[0]], src[sp[0] : sp[1]], src[sp[1] :]
        syms = sorted(set(m.group(1) for m in use.finditer(body)))
        base = build_score(b, version, fn, unit, src)
        if base is None:
            return f"{fn} BASE-BUILD-FAIL"
        kept, cur = [], base
        lits = {s: literals.literal_for(info.get(s)) for s in syms}
        lits = {k: v for k, v in lits.items() if v}
        if lits:  # every TU-local float pool constant as a literal, in one build
            nb = use.sub(lambda m: lits.get(m.group(1), m.group(0)), body)
            sc = build_score(b, version, fn, unit, head + nb + tail)
            if sc is not None and sc < cur:
                kept.append(f"literals({len(lits)}) {cur}->{sc}")
                cur, body = sc, nb
            else:
                kept.append(f"literals({len(lits)}) no-gain {sc}")
        for s in syms:
            if cur == 0:
                break
            i = info.get(s, {})
            if s in lits or (set(i.get("decl_kind", [])) & {"s", "a"}) or i.get("literal"):
                continue
            rep = "((struct{__typeof__(%s) v;}*)&%s)->v" % (s, s)
            nb = use.sub(lambda m, s=s, rep=rep: rep if m.group(1) == s else m.group(0), body)
            sc = build_score(b, version, fn, unit, head + nb + tail)
            if sc is not None and sc < cur:
                kept.append(f"{s}:instruct {cur}->{sc}")
                cur, body = sc, nb
    return f"{fn} base={base} final={cur} kept=[{'; '.join(kept)}] tried={len(syms)}"


def try_edits(b: Builder, version: str, fn: str, unit: str, edits: list[tuple[str, str]], prelude: str = "") -> str:
    """Score FN's draft with OLD=>NEW edits (re:PATTERN=>REPL outside string literals); the unit is restored."""
    path = b.tree / unit
    with restored(path) as orig:
        src = source.unwrap(orig, fn)
        for old, new in edits:
            if old.startswith("re:"):
                pat = re.compile(old[3:])
                parts = re.split(r'("(?:\\.|[^"\\])*")', src)
                if not any(pat.search(p) for p in parts[::2]):
                    return f"{fn} NOMATCH {old!r}"
                src = "".join(pat.sub(new.replace("\\n", "\n"), p) if i % 2 == 0 else p for i, p in enumerate(parts))
            elif old not in src:
                return f"{fn} NOMATCH {old!r}"
            else:
                src = src.replace(old, new.replace("\\n", "\n"))
        if prelude:
            inc = re.search(r"^#include .*\n", src, re.M)
            at = inc.end() if inc else 0
            pre = (f'#include "{b.cfg.prelude}"\n' if b.cfg.prelude else "") + prelude + "\n"
            src = src[:at] + pre + src[at:]
        sc = build_score(b, version, fn, unit, src)
    return f"{fn} BUILD-FAIL" if sc is None else f"{fn} edit={sc}"


def fn_unit(cfg: DataConfig, tree: Path, version: str, fn: str) -> str | None:
    f = tree / cfg.path(cfg.map_file, version)
    c = cfg.map_columns
    for r in json.loads(f.read_text())["fns"]:
        if r[c["name"]] == fn:
            return r[c["unit"]]
    return None
