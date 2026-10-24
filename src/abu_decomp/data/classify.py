"""Clusters and classes from a World: STRUCT / ARRAY / F32_TABLE / SUBWORD / SCALAR_MEMBER / STRUCT_ALIAS /
LITERAL_POOL, and the lone globals (MISTYPED / STRING / LITERAL / LONE). Output is the census.json structure."""

from __future__ import annotations

import re

from abu_decomp.data import evidence
from abu_decomp.data.asm import kindof
from abu_decomp.data.cdecl import decl_kind
from abu_decomp.data.evidence import World

CLASS_RANK = {
    "SCALAR_MEMBER": 0,
    "SUBWORD": 1,
    "STRUCT": 2,
    "ARRAY": 2,
    "F32_TABLE": 2,
    "STRUCT_ALIAS": 4,
    "LITERAL_POOL": 9,
}
CLASSES = tuple(CLASS_RANK)


def build(w: World, raw: re.Pattern[str], prefix: str, open_fns: set[str], psize_paths: dict | None = None) -> dict:
    rev = w.rev()
    datas = sorted((n for n in rev if n.startswith(prefix)), key=lambda n: rev[n])
    lit = {n for n in datas if w.is_literal(n)}
    pars = evidence.parents(w, datas)
    inside = evidence.inside_map(w, datas, pars)
    edges = evidence.edges_between(w, datas, lit)
    owner = evidence.pick_owners(w, inside)
    groups, evmap = evidence.group(w, owner, edges)

    def info(n: str) -> dict:
        lab = w.labels[w.ref].get(n)
        dt = sorted({t for _, t in w.decl.get(n, [])})
        o = owner.get(n)
        st = w.delta_stable(o[0], n)[0] if o else True
        dps = [x for x in inside.get(n, []) if x[2].startswith("decl:")]
        inner = max(dps, key=lambda x: rev[x[0]]) if dps else None
        per = w.sizes.get(n, {})
        return {
            "sym": n, "inner": (f"{inner[0]}+0x{inner[1]:X}") if inner else "", "addr": f"{rev[n]:08X}",
            "size": w.vsize(w.ref, n), "access": w.acc_types(n), "decl": dt, "decl_kind": sorted(decl_kind(dt)),
            "decl_files": sorted({f for f, _ in w.decl.get(n, [])}), "sizeof": w.ref_size(n), "sizeof_by_version": per,
            "sizeof_stable": evidence_stable(per), "file": lab[0] if lab else "bss",
            "init": lab[1][:16].hex() if lab else "", "owner": ("%s+0x%X (%s)" % o) if o else "",
            "owner_stable": st, "literal": n in lit, "consumers": sorted({fn for v in w.versions for fn in w.fns_v(n, v)}),
            "c_files": sorted(w.uses.get(n, ())),
            "path": (psize_paths or {}).get(n, ""),
        }  # fmt: skip

    def scalar(m: dict) -> bool:
        return bool(set(m["decl_kind"]) & {"i", "f", "p"}) or (not m["decl"] and bool(m["access"]))

    clusters, seen = [], set()
    for root, mem in groups.items():
        mem.sort(key=lambda n: rev[n])
        seen.update(mem)
        mi = [info(n) for n in mem]
        kinds = sorted(
            {e[0] for x in evmap[root] for e in x["ev"]} | ({"INSIDE"} if any(m["owner"] for m in mi) else set())
        )
        base = mi[0]
        declared = bool(base["sizeof"]) and any(m["owner"].startswith(base["sym"] + "+") for m in mi[1:])
        widths = {
            next(iter(m["access"]), "?").split(":")[0] + kindof(next(iter(m["access"]), "?:i").split(":")[1])
            for m in mi
        }
        strides = {rev[mem[i + 1]] - rev[mem[i]] for i in range(len(mem) - 1)}
        if declared:
            cls = "SCALAR_MEMBER" if any(scalar(m) for m in mi[1:]) else "STRUCT_ALIAS"
        elif "SUBWORD" in kinds:
            cls = "SUBWORD"
        elif len(widths) == 1 and len(strides) == 1 and "?" not in next(iter(widths)):
            cls = "F32_TABLE" if next(iter(widths)).endswith("f") else "ARRAY"
        else:
            cls = "STRUCT"
        if all(m["literal"] for m in mi):
            cls = "LITERAL_POOL"
        unstable = [m["sym"] for m in mi if not m["owner_stable"]]
        unstable += [m["sym"] + ":sizeof" for m in mi if not m["sizeof_stable"]]
        clusters.append(
            {
                "base": base["sym"],
                "class": cls,
                "kinds": kinds,
                "members": mi,
                "edges": evmap[root][:8],
                "unstable": unstable,
            }
        )

    lones = []
    for n in datas:
        if n in seen or not raw.match(n) or not w.uses.get(n):
            continue
        i = info(n)
        i["class"] = lone_class(i, n in lit, w.labels[w.ref].get(n))
        lones.append(i)

    for c in clusters:
        cons = {fn for m in c["members"] for fn in m["consumers"]}
        o = cons & open_fns
        rank = CLASS_RANK[c["class"]]
        c["open_consumers"] = sorted(o)
        c["prio"] = "P1" if o and rank < 4 else "P2" if rank < 4 else "P3" if rank < 9 else "P9"
    clusters.sort(
        key=lambda c: (c["prio"], -len(c["open_consumers"]), -len({fn for m in c["members"] for fn in m["consumers"]}))
    )
    return {"clusters": clusters, "lones": lones}


def evidence_stable(per: dict[str, int]) -> bool:
    return len(set(per.values())) <= 1


def lone_class(i: dict, literal: bool, lab) -> str:
    ak = {kindof(x.split(":")[1]) for x in i["access"]}
    dk = set(i["decl_kind"])
    widths = {int(x.split(":")[0]) for x in i["access"]}
    dt = " ".join(i["decl"])
    mis = bool(ak and dk and (("f" in ak and dk & {"i"}) or ("i" in ak and dk & {"f"})))
    narrow = widths == {4} and re.search(r"\b(s8|u8|char|s16|u16|short)\b", dt)
    wide = widths <= {1, 2} and re.search(r"\b(s32|u32|int|long)\b", dt)
    if dk == {"i"} and widths and "[" not in dt and (narrow or wide):
        mis = True
    if len(dk) > 1:
        mis = True
    b = lab[1] if lab else b""
    string = (
        len(b) >= 4
        and b[-1:] == b"\0"
        and all(32 <= c < 127 or c in (9, 10, 0) for c in b)
        and sum(c >= 32 for c in b) >= 3
    )
    return "LITERAL" if literal else "MISTYPED" if mis else "STRING" if string else "LONE"


def stats(w: World, raw: re.Pattern[str], data: dict, psize_n: int) -> dict:
    from abu_decomp.data.cdecl import decl_kind as dk

    raw_c = sorted(t for t in w.uses if raw.match(t))
    scalar_decls = [t for t in raw_c if w.decl.get(t) and dk([x for _, x in w.decl[t]]) <= {"i", "f", "p"}]
    cl, lo = data["clusters"], data["lones"]
    return {
        "raw_names_in_c": len(raw_c), "raw_scalar_decls": len(scalar_decls), "clusters": len(cl),
        "clustered_syms": sum(len(c["members"]) for c in cl), "probe_sizes": psize_n,
        "by_class": {k: sum(1 for c in cl if c["class"] == k) for k in CLASSES},
        "by_prio": {k: sum(1 for c in cl if c["prio"] == k) for k in ("P1", "P2", "P3", "P9")},
        "lone_by_class": {k: sum(1 for x in lo if x["class"] == k) for k in ("LONE", "MISTYPED", "STRING", "LITERAL")},
        "sizeof_unstable": sorted(s for s, per in w.sizes.items() if not evidence_stable(per)),
    }  # fmt: skip
