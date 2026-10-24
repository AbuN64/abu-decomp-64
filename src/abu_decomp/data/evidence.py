"""Edges and owners between address-adjacent raw globals: INSIDE / REACH / INDEX / SUBWORD / TABLE evidence.

Pure functions over a `World` (the loaded facts), so tests build a World by hand."""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass, field

from abu_decomp.data.asm import kindof


@dataclass
class World:
    versions: list[str]
    ref: str
    syms: dict[str, dict[str, int]]  # version -> name -> addr
    labels: dict[str, dict[str, tuple[str, bytes]]]
    acc: dict[str, dict[str, list]]  # version -> sym -> accessors
    decl: dict[str, list]  # sym -> [(file, type text)]
    uses: dict[str, set]
    sizes: dict[str, dict[str, int]]  # sym -> {version: sizeof}
    size_src: dict[str, str] = field(default_factory=dict)
    fnunit: dict[str, str] = field(default_factory=dict)
    fnscore: dict[str, float] = field(default_factory=dict)
    _vorder: dict[str, list[int]] = field(default_factory=dict)

    def rev(self) -> dict[str, int]:
        return self.syms[self.ref]

    def vsize(self, v: str, n: str) -> int:
        """Bytes of label n in version v: its init bytes, else the gap to the next symbol."""
        if n in self.labels[v] and self.labels[v][n][1]:
            return len(self.labels[v][n][1])
        ad = self.syms[v].get(n)
        if ad is None:
            return 0
        if v not in self._vorder:
            self._vorder[v] = sorted(set(self.syms[v].values()))
        order = self._vorder[v]
        i = bisect.bisect_right(order, ad)
        return (order[i] - ad) if i < len(order) else 0

    def ref_size(self, n: str) -> int:
        return self.sizes.get(n, {}).get(self.ref, 0)

    def acc_types(self, n: str) -> dict[str, int]:
        ts: dict[str, int] = defaultdict(int)
        for v in self.versions:
            for _fn, off, w, t, _k in self.acc[v].get(n, []):
                if off == 0 and w:
                    ts[f"{w}:{t}"] += 1
        return dict(sorted(ts.items(), key=lambda kv: -kv[1]))

    def fns_v(self, n: str, v: str) -> set[str]:
        return {r[0] for r in self.acc[v].get(n, [])}

    def is_literal(self, n: str) -> bool:
        """Read-only and every version reads it only from fns of ONE C unit: TU-local rodata."""
        any_acc = False
        for v in self.versions:
            rows = self.acc[v].get(n, [])
            if not rows:
                continue
            any_acc = True
            if any(r[4] == "S" for r in rows) or len({self.fnunit[r[0]] for r in rows if r[0] in self.fnunit}) > 1:
                return False
        return any_acc

    def delta_stable(self, p: str, t: str) -> tuple[bool, dict[str, int]]:
        ds = {v: self.syms[v][t] - self.syms[v][p] for v in self.versions if p in self.syms[v] and t in self.syms[v]}
        return len(set(ds.values())) <= 1, ds


def parents(w: World, datas: list[str]) -> list[tuple[int, int, str, str]]:
    """(addr, size, sym, source) for each declared aggregate (sizeof) and each asm extent that reaches past its label."""
    rev = w.rev()
    out = []
    for n in datas:
        sz = w.ref_size(n)
        if sz > max(w.vsize(w.ref, n), 4) and rev[n] % 4 == 0:  # an odd-address 4 KB union is a bogus decl
            out.append((rev[n], sz, n, "decl:" + w.size_src.get(n, "").removeprefix("decl:")))
        ext = max([off + max(wd, 1) for _fn, off, wd, _t, _k in w.acc[w.ref].get(n, []) if off > 0] or [0])
        if ext > max(w.vsize(w.ref, n), 0) and ext > 0:
            out.append((rev[n], ext, n, "asm-extent"))
    return out


def inside_map(w: World, datas: list[str], pars) -> dict[str, list]:
    rev = w.rev()
    inside: dict[str, list] = defaultdict(list)
    starts = sorted(pars)
    for n in datas:
        ad = rev[n]
        for pa, sz, p, src in starts:
            if pa >= ad:
                break
            if pa < ad < pa + sz and p != n:
                inside[n].append((p, ad - pa, src))
    return inside


def edges_between(w: World, datas: list[str], lit: set[str]) -> list:
    """Adjacent pairs (s, t, delta, stable, evidence list) that look like one aggregate."""
    rev = w.rev()
    edges = []
    for i in range(len(datas) - 1):
        s, t = datas[i], datas[i + 1]
        d = rev[t] - rev[s]
        if d <= 0 or d > 0x1000 or (s in lit and t in lit):
            continue
        st, ds = w.delta_stable(s, t)
        ev = []
        for v in w.versions:
            if v not in ds:
                continue
            hit = next(((r[0], r[1]) for r in w.acc[v].get(s, []) if r[4] == "I" and r[1] + max(r[2], 1) > 0), None)
            if hit:
                ev.append(("INDEX", v, hit[0]))
                break
        sw = max([r[2] for v in w.versions for r in w.acc[v].get(s, []) if r[1] == 0] or [0])
        if d < sw and (rev[t] % 4 or t.startswith("D_auto_")):
            ev.append(("SUBWORD", w.ref, f"lw/lh width {sw} over +{d}"))
        ta, tb = w.acc_types(s), w.acc_types(t)
        if st and ta and tb and s not in lit and t not in lit:
            wa = {(x.split(":")[0], kindof(x.split(":")[1])) for x in ta}
            wb = {(x.split(":")[0], kindof(x.split(":")[1])) for x in tb}
            co = max((len(w.fns_v(s, v) & w.fns_v(t, v)), v) for v in w.versions)
            if len(wa) == 1 and wa == wb and co[0] >= 2:
                ev.append(("TABLE", co[1], f"{co[0]} shared fns"))
        if ev:
            edges.append((s, t, d, st, ev))
    return edges


def pick_owners(w: World, inside: dict[str, list]) -> dict[str, tuple[str, int, str]]:
    """owner = outermost declared parent if any, else outermost asm-extent parent."""
    rev = w.rev()
    owner = {}
    for n, ps in inside.items():
        decl_ps = [x for x in ps if x[2].startswith("decl:")]
        owner[n] = min(decl_ps or ps, key=lambda x: rev[x[0]])
    return owner


def group(w: World, owner: dict, edges: list):
    """Union-find over owner links and edges: root -> members, root -> edge evidence."""
    rev = w.rev()
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        ra, rb = find(a), find(b)
        if ra != rb:
            if rev[ra] <= rev[rb]:
                parent[rb] = ra
            else:
                parent[ra] = rb

    for n, (p, _off, _src) in owner.items():
        union(p, n)
    for s, t, _d, _st, _ev in edges:
        union(s, t)
    groups: dict[str, list[str]] = defaultdict(list)
    for n in list(parent):
        groups[find(n)].append(n)
    evmap: dict[str, list] = defaultdict(list)
    for s, t, d, st, ev in edges:
        evmap[find(s)].append({"from": s, "to": t, "delta": d, "stable": st, "ev": [list(map(str, e)) for e in ev]})
    return groups, evmap
