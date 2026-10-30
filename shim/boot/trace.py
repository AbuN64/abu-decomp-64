# SPDX-License-Identifier: GPL-3.0-or-later
"""Compile the oracle's boot trace into the table the hit census reads (trace.c)."""
import argparse
import csv
import json
import pathlib
import re
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import policy  # noqa: E402
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import portdir  # noqa: E402

IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")
MAX_NAMES = 4


def rows(trace):
    """(order, address, [symbol names]) from boot-trace.tsv: order, address, then one or more names."""
    result, seen = [], set()
    for number, line in enumerate(trace.read_text().splitlines()):
        if number == 0 or not line.strip():
            continue
        fields = next(csv.reader([line], delimiter="\t"))
        order, address, names = int(fields[0]), int(fields[1], 16), [n for n in fields[2:] if n]
        if order != len(result) + 1 or order in seen or not names:
            raise ValueError(f"trace row {number}: bad order or no name")
        seen.add(order)
        result.append((order, address, names))
    if not result:
        raise ValueError("empty trace")
    return result


def generate(trace, manifest, source, output):
    # CMake passes the effective manifest that produced bindings.ld. Re-running
    # SDK policy here can replace an explicit native adapter with a different API.
    project = json.loads(manifest.read_text())
    by_address, functions = {}, {}
    for line in (source / "versions" / project["version"] / "symbol_addrs.txt").read_text().splitlines():
        match = re.match(r"\s*(\w+)\s*=\s*(0x[0-9A-Fa-f]+)\s*;(.*)", line)
        if match:
            by_address.setdefault(int(match[2], 16), []).append(match[1])
            if "type:func" in match[3]:
                functions[match[1]] = int(match[2], 16)
    native_names = native_functions()
    native_bindings = {symbol: target for symbol, target in project["bindings"].items()
                       if target in native_names}
    entries = rows(trace)
    traced_names = {n for _, address, names in entries for n in names + by_address.get(address, [])}
    for symbol in sorted(traced_names & native_names):
        native_bindings.setdefault(symbol, symbol)
    # Native APIs can keep original SDK names; game aliases remain original hits.
    bound = set(native_bindings) | set(native_bindings.values())
    lines = ['/* Generated boot trace table; addresses and names only. */', '#include "trace.h"',
             "const N64cTraceEntry n64c_trace_entries[] = {"]
    for order, address, names in entries:
        names = list(dict.fromkeys(names + by_address.get(address, [])))
        if len(names) > MAX_NAMES or not all(IDENT.fullmatch(n) for n in names):
            raise ValueError(f"trace order {order}: unusable names {names}")
        shim = int(any(n in bound for n in names))
        quoted = ", ".join(f'"{n}"' for n in names)
        lines.append(f"    {{{order}, 0x{address:08x}u, {shim}, {{{quoted}}}}},")
    lines += ["};", f"const unsigned n64c_trace_count = {len(entries)};", ""]
    native = {}
    for symbol, target in sorted(native_bindings.items()):
        if not IDENT.fullmatch(symbol) or not IDENT.fullmatch(target):
            raise ValueError(f"unusable shim binding {symbol}: {target}")
        native.setdefault(target, []).append(symbol)
    lines.append("const N64cShimEntry n64c_shim_entries[] = {")
    for target, symbols in sorted(native.items()):
        lines.append(f'    {{"{target}", "{" ".join(sorted(set(symbols) | {target}))}"}},')
    if not native:
        lines.append("    {0, 0},")
    lines += ["};", f"const unsigned n64c_shim_count = {len(native)};", ""]
    # Guest work (RUNTIME.md "Virtual guest time"): one tick per entry of an original function the port compiles, so
    # never of the SDK range (policy) or of a function the port runs natively (bound, or defined by the shim under its
    # original name); both sides charge this same set (the oracle's cpu.native_code fact lists the same vrams).
    native_addresses = {functions[name] for name in bound | (native_names & set(functions)) if name in functions}
    charged = sorted(name for name, address in functions.items()
                     if not policy.SDK_LO <= address < policy.SDK_HI and address not in native_addresses)
    if not all(IDENT.fullmatch(name) for name in charged):
        raise ValueError("unusable function name in symbol_addrs.txt")
    lines.append("const char *const n64c_trace_charged[] = {")
    lines += [f'    "{name}",' for name in charged] or ["    0,"]
    lines += ["};", f"const unsigned n64c_trace_charged_count = {len(charged)};", ""]
    output.mkdir(parents=True, exist_ok=True)
    temporary = output / "trace_table.c.tmp"
    temporary.write_text("\n".join(lines))
    temporary.replace(output / "trace_table.c")


def native_functions():
    """Definitions at the native adapter boundary, excluding original game aliases."""
    here = pathlib.Path(__file__).resolve().parent
    files = sorted(here.parent.glob("n64c_*.[ch]")) + [portdir.fact("sched_glue.c")]
    return {name for path in files for name in policy.functions(path.read_text())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=pathlib.Path, required=True)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--source", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        generate(args.trace, args.manifest, args.source, args.output)
    except (ValueError, OSError) as error:
        sys.exit(f"trace: {error}")
