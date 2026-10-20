# SPDX-License-Identifier: GPL-3.0-or-later
"""Grow us_rev1.json to the link closure of rage_decomp_boot, routing what cannot link.

Each round builds rage_decomp_boot (ninja -k 0) and routes every undefined reference:
- the source unit that defines it is added (C body, or an INCLUDE_ASM stand-in whose prepared stub is a stop);
- a unit that fails to compile (a declaration conflict: decomp owner) is refused; a function only it
  defines becomes an explicit stop naming the first conflict;
- a function of the original libultra (0x802AE000-0x802C8000, pinned or by name) is never linked natively:
  it binds to the shim when shim-bindings.txt identifies it and the shim declares that name, else it is a
  stop naming the shim decision it needs;
- a data name with no C definition binds to its pinned us-rev1 address (symbol_addrs.txt or a data
  alias in aliases.txt), or, for the source's
  address-named data D_auto_80XXXXXX, to the address its name states (or to the function defined there);
- a pre-KSEG0 alias (D_00XXXXXX, D_auto_00XXXXXX) binds to the function at that address;
- a function with no definition becomes an explicit stop;
- a data name never becomes a stop (a stop is a function): one that cannot be bound is a data need
  (WORK/data-needs.txt) and stays undefined.
With --frontier a defined function is not linked: it becomes a "(boot frontier)" stop that advance.py
links once the boot reaches it."""
import argparse
import json
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import needs as needsfeed  # noqa: E402
import policy  # noqa: E402
NAMED = re.compile(r"D_auto_(8[0-9A-F]{7})")
ALIAS = re.compile(r"D_(?:auto_)?00([0-9A-F]{6})")


class Facts:
    """The pinned source tree, its us-rev1 addresses and the shim's libultra identifications."""

    def __init__(self, source, shim_dir, shim_bindings, game=frozenset()):
        units = sorted(p for p in (source / "src").rglob("*")
                       if p.suffix in (".CPP", ".c") and "/libgcc/" not in str(p))
        self.source = source
        self.text = {str(p.relative_to(source)): p.read_text(errors="replace") for p in units}
        self.addrs = policy.pinned(source, "us-rev1")
        self.numeric = {n: int(a, 16) for n, (a, _) in self.addrs.items()}
        self.sdk_units = {}
        self.game = frozenset(game)  # policy.game_code: game functions inside the libultra range
        self.shim_src = "\n".join(p.read_text(errors="replace") for p in sorted(shim_dir.glob("n64c_*.[ch]")))
        self.shim = {}  # original function -> (libultra name, evidence)
        for line in shim_bindings.read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                fn, osname, *evidence = line.split(None, 2)
                self.shim[fn] = (osname, evidence[0] if evidence else "")

    def definers(self, sym):
        fn = re.compile(r"^[A-Za-z_][^;\n=]*\b" + re.escape(sym) + r"\s*\([^;]*$", re.M)
        asm = re.compile(r"INCLUDE_ASM\([^)]*\b" + re.escape(sym) + r"\s*\)")
        data = re.compile(r"^(?!extern)[A-Za-z_][^(\n]*\b" + re.escape(sym) + r"\b\s*(\[[^\]]*\])*\s*(=|;)", re.M)
        return [u for u, t in self.text.items() if sym in t and (fn.search(t) or asm.search(t) or
                (sym.startswith("D_") and data.search(t)))]

    def function_at(self, address):
        """The one defined function pinned at address, if any."""
        exact = [n for n, (a, _) in self.addrs.items()
                 if n.startswith("func_") and int(a, 16) == address and self.definers(n)]
        return exact[0] if len(exact) == 1 else None

    def function_alias(self, sym):
        """D_00XXXXXX / D_auto_00XXXXXX name a function's address without its KSEG0 bit
        (D_002BD378 -> func_802BD378_de). The pinned us-rev1 value wins over the name."""
        m = ALIAS.fullmatch(sym)
        if not m:
            return None
        target = int(self.addrs[sym][0], 16) | 0x80000000 if sym in self.addrs else int("80" + m.group(1), 16)
        if (fn := self.function_at(target)):
            return fn
        pat = re.compile(r"\bfunc_80" + m.group(1) + r"(?:_[a-z0-9_]+)?\b")
        names = sorted({n for t in self.text.values() if "func_80" + m.group(1) in t for n in pat.findall(t)})
        names = [n for n in names if self.definers(n)]
        return names[0] if len(names) == 1 else None

    def sdk(self, sym):
        """The libultra address of a function (pinned us-rev1, else the one its name states), if in range."""
        return policy.sdk_address(sym, self.numeric, self.game)

    def sdk_unit(self, unit):
        if unit not in self.sdk_units:
            self.sdk_units[unit] = policy.sdk_unit(unit, self.text[unit], self.numeric, self.game)
        return self.sdk_units[unit]

    def shim_name(self, sym):
        """The shim function an identified libultra function binds to, when the shim declares it."""
        osname = self.shim.get(sym, (None,))[0]
        if osname and re.search(r"\b" + re.escape(osname) + r"\s*\(", self.shim_src):
            return osname
        return None


def drop_moved(d, facts):
    """Drop stops whose text names a source unit the pinned tree no longer has (a moved or renamed unit)."""
    stale = []
    for sym, why in d["stops"].items():
        unit = needsfeed.unit_of(why)
        if unit and unit not in facts.text and not unit.startswith("src/libgcc/"):
            stale.append(sym)
    for sym in stale:
        del d["stops"][sym]
    return stale
def drop_unrefused(d, deny):
    """Drop declaration-conflict stops ("RW NEEDS") whose unit this run has not refused: the denial that wrote
    them belongs to an older pin (repin clears deny.json), so the unit is tried again and refuse() stops it
    again only if it still fails."""
    stale = [sym for sym, why in d["stops"].items()
             if why.endswith("RW NEEDS one declaration") and needsfeed.unit_of(why) not in deny]
    for sym in stale:
        del d["stops"][sym]
    return stale


def tidy(d, facts, deny=None):
    """Undo routes an earlier round (or an older closure) got wrong, so this round routes them again."""
    for sym in drop_moved(d, facts):
        print(f"   dropped stale stop {sym} (its unit moved)", flush=True)
    for sym in drop_unrefused(d, deny) if deny is not None else ():
        print(f"   dropped stale stop {sym} (its unit is not refused at this pin)", flush=True)
    policy.sanitize(d, facts.source, facts.numeric, {n: v[0] for n, v in facts.shim.items()}, facts.shim_src)
    for sym in [k for k, v in d["data"].items() if int(v, 16) < 0x80000000]:
        del d["data"][sym]  # a pre-KSEG0 alias bound as data
    for sym in [k for k in d["stops"] if k.startswith("D_")]:
        del d["stops"][sym]  # data routed as a function stop
    for sym in [k for k, why in d["stops"].items() if k in facts.game and "(SDK: shim binding decision)" in why]:
        del d["stops"][sym]  # game code in the libultra range (policy.game_code) routes again as game code
    rebind(d, facts)


def rebind(d, facts):
    """Move each libultra stop the shim now provides (a shim-bindings.txt row whose name the shim declares) to bindings."""
    for sym in [k for k in d["stops"] if facts.shim_name(k)
                and (facts.sdk(k) or any(facts.sdk_unit(h) for h in facts.definers(k)))]:
        del d["stops"][sym]
        d["bindings"][sym] = facts.shim_name(sym)


def route(d, sym, facts, deny, pin, frontier, fresh):
    """Route one undefined symbol into d. Returns (kind, detail); kind is added, routed, need or skip."""
    if sym in d["stops"] or sym in d["data"] or sym in d["bindings"] or sym in d.get("rom_offsets", {}):
        return "skip", ""
    addrs = facts.addrs
    if (address := facts.sdk(sym)):
        if (osname := facts.shim_name(sym)):
            d["bindings"][sym] = osname
            return "routed", f"binding {osname} (libultra, shim)"
        osname, evidence = facts.shim.get(sym, (None, ""))
        d["stops"][sym] = (f"libultra 0x{address:08X}" + (f" = {osname} ({evidence}); shim lacks {osname}" if osname
                           else "; not identified for the shim") + f" at {pin} (SDK: shim binding decision)")
        return "routed", "stop " + d["stops"][sym]
    found = facts.definers(sym)
    if any(facts.sdk_unit(h) for h in found):
        if sym.startswith("D_"):
            return "need", f"{sym} | original SDK data needs a clean provider; refusing its code unit"
        if (osname := facts.shim_name(sym)):
            d["bindings"][sym] = osname
            return "routed", f"binding {osname} (library unit, shim)"
        d["stops"][sym] = "original SDK unit; clean shim required (SDK: shim binding decision)"
        return "routed", "stop " + d["stops"][sym]
    if any(h in fresh for h in found):
        return "skip", ""
    hits = [h for h in found if h not in d["sources"] and h not in deny]
    # Data must have real storage before linking. Only functions can wait behind
    # an executable frontier stop; treating a data definition as one would bind
    # reads and writes to code bytes instead of the object's storage.
    if hits and not any(h in d["sources"] for h in found) and (not frontier or sym.startswith("D_")):
        d["sources"].append(hits[0])
        fresh.add(hits[0])
        return "added", hits[0]
    if (alias := facts.function_alias(sym)):
        d["bindings"][sym] = alias
        owners = facts.definers(alias)
        if alias not in d["stops"] and not any(h in d["sources"] for h in owners):
            d["stops"][alias] = (f"{owners[0]} defines it; not linked yet at {pin} (boot frontier)" if frontier and owners
                                 else f"no linked definition at {pin}")
        return "routed", "binding " + alias
    if re.fullmatch(r"D_[0-9A-F]{3,6}", sym) and sym in addrs and not found and int(addrs[sym][0], 16) < 0x4000000:
        d.setdefault("rom_offsets", {})[sym] = addrs[sym][0]
        return "routed", "rom " + addrs[sym][0]
    if (named := NAMED.fullmatch(sym)) and sym not in addrs and not found:
        if (fn := facts.function_at(int(named.group(1), 16))):
            d["bindings"][sym] = fn
            return "routed", "binding " + fn
        d["data"][sym] = "0x" + named.group(1)
        return "routed", "data (named) 0x" + named.group(1)
    if sym.startswith("D_") and sym in addrs and not found and int(addrs[sym][0], 16) >= 0x80000000:
        d["data"][sym] = addrs[sym][0]
        return "routed", "data " + addrs[sym][0]
    why = next((deny[h] for h in found if h in deny), None)
    if sym.startswith("D_"):
        return "need", f"{sym} | data with no pinned address at {pin}" + (
            f"; defining unit refused: {why}" if why else f"; defined in {found[0]}" if found else "; no C definition")
    if not why and frontier and hits:
        d["stops"][sym] = f"{hits[0]} defines it; not linked yet at {pin} (boot frontier)"
        return "routed", "frontier " + hits[0]
    pinned = f" (pinned {addrs[sym][0]}{addrs[sym][1].strip() and ' ' + addrs[sym][1].strip()})" if sym in addrs else ""
    d["stops"][sym] = (why + f" at {pin}; RW NEEDS one declaration") if why else f"no C definition at {pin}" + pinned
    return "routed", "stop " + d["stops"][sym]


def refuse(d, deny, log):
    """Record units whose compile failed and drop them from the sources."""
    for unit in sorted(set(re.findall(r"^FAILED: decomp/(src/\S+?)\.i ", log, re.M))):
        err = re.search(re.escape(unit) + r":(\d+):\d+: error: (.*)", log)
        deny[unit] = f"{unit}:{err.group(1)} {err.group(2)}" if err else unit + " does not compile"
        if unit in d["sources"]:
            d["sources"].remove(unit)
    for sym in set(re.findall(r"multiple definition of [`']([A-Za-z0-9_]+)'", log)):
        d["stops"].pop(sym, None)  # a linked unit now defines a former frontier stop


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--build", type=pathlib.Path, required=True, help="configured RAGE_DECOMP_SRC build directory")
    parser.add_argument("--source", type=pathlib.Path, required=True, help="the decomp tree that build compiles")
    parser.add_argument("--work", type=pathlib.Path, required=True, help="logs, deny.json, data-needs.txt")
    parser.add_argument("--manifest", type=pathlib.Path, default=HERE / "us_rev1.json")
    parser.add_argument("--shim-bindings", type=pathlib.Path, default=HERE / "shim-bindings.txt")
    parser.add_argument("--worklist", type=pathlib.Path, help="worklist-rw.tsv, for trace_order in needs.jsonl")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--rounds", type=int, default=8)
    parser.add_argument("--frontier", action="store_true")
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    facts = Facts(args.source, HERE.parent, args.shim_bindings, policy.game_code(json.loads(args.manifest.read_text())))
    deny_path, log_path = args.work / "deny.json", args.work / "build.log"
    deny = json.loads(deny_path.read_text()) if deny_path.exists() else {}
    pin = json.loads(args.manifest.read_text())["source_pin"][:7]
    # Sanitize inherited manifests before the first build can consume them.
    initial = json.loads(args.manifest.read_text())
    tidy(initial, facts, deny)
    args.manifest.write_text(json.dumps(initial, indent=2) + "\n")
    for round_ in range(args.rounds):
        with log_path.open("w") as log_file:
            rc = subprocess.run(["cmake", "--build", str(args.build), "-j", str(args.jobs), "--target",
                                 "rage_decomp_boot", "--", "-k", "0"], stdout=log_file, stderr=subprocess.STDOUT).returncode
        log = log_path.read_text(errors="replace")
        d = json.loads(args.manifest.read_text())
        built_manifest = json.dumps(d, sort_keys=True)
        tidy(d, facts, deny)
        refuse(d, deny, log)
        undefined = sorted(set(re.findall(r"undefined reference to [`']([A-Za-z0-9_]+)'", log)))
        fresh, out, needs = set(), [], []
        for sym in undefined:
            kind, detail = route(d, sym, facts, deny, pin, args.frontier, fresh)
            if kind == "need":
                needs.append(detail)
            elif kind != "skip":
                out.append((kind, sym, detail))
        args.manifest.write_text(json.dumps(d, indent=2) + "\n")
        deny_path.write_text(json.dumps(deny, indent=1) + "\n")
        (args.work / "data-needs.txt").write_text("".join(n + "\n" for n in needs))
        print(f"round {round_}: rc={rc} undefined={len(undefined)} changed={len(out)} needs={len(needs)} "
              f"refused={len(deny)}", flush=True)
        for kind, sym, detail in out:
            print(f"   {kind} {sym} {detail}", flush=True)
        for n in needs:
            print("   need", n, flush=True)
        if rc == 0 and json.dumps(d, sort_keys=True) == built_manifest:
            needsfeed.write(args.work, args.manifest, args.worklist)
            print("closure: links")
            return 0
        if json.dumps(d, sort_keys=True) != built_manifest:
            continue  # the linked binary must reflect tidy/refuse changes too
        if not out and "multiple definition" not in log and "FAILED: decomp/" not in log:
            needsfeed.write(args.work, args.manifest, args.worklist)
            print(f"closure: stuck, see {log_path}")
            return 1
    needsfeed.write(args.work, args.manifest, args.worklist)
    print("closure: rounds exhausted")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
