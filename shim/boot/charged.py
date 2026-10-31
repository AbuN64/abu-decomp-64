# SPDX-License-Identifier: GPL-3.0-or-later
"""Compile the project's guest work declaration into the table the entry charge reads (n64c_charge.c)."""
import argparse
import json
import pathlib
import re
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import policy  # noqa: E402

IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")
NS_PER_ENTRY = re.compile(r"[0-9]+(\.[0-9]+)?\Z")


def native_functions(port_dir):
    """Definitions at the native adapter boundary, excluding original game aliases: the shim's n64c_* units and the
    port's sched_glue.c, read from the port facts directory."""
    here = pathlib.Path(__file__).resolve().parent
    glue = port_dir / "sched_glue.c"
    if not glue.is_file():
        raise ValueError(f"the port facts directory has no sched_glue.c: {glue}")
    files = sorted(here.parent.glob("n64c_*.[ch]")) + [glue]
    return {name for path in files for name in policy.functions(path.read_text())}


def symbol_table(source, version):
    """({name: address of every function}, {address: [names]}) from the version's symbol_addrs.txt."""
    by_address, functions = {}, {}
    for line in (source / "versions" / version / "symbol_addrs.txt").read_text().splitlines():
        match = re.match(r"\s*(\w+)\s*=\s*(0x[0-9A-Fa-f]+)\s*;(.*)", line)
        if match:
            by_address.setdefault(int(match[2], 16), []).append(match[1])
            if "type:func" in match[3]:
                functions[match[1]] = int(match[2], 16)
    return functions, by_address


def charged_names(bound, native_names, functions):
    """Guest work (RUNTIME.md "Virtual guest time"): one tick per entry of an original function the port compiles, so
    never of the SDK range (policy) or of a function the port runs natively (bound, or defined by the shim under its
    original name); both sides charge this same set (the oracle's cpu.native_code fact lists the same vrams)."""
    native_addresses = {functions[name] for name in set(bound) | (native_names & set(functions)) if name in functions}
    names = sorted(name for name, address in functions.items()
                   if not policy.SDK_LO <= address < policy.SDK_HI and address not in native_addresses)
    if not all(IDENT.fullmatch(name) for name in names):
        raise ValueError("unusable function name in symbol_addrs.txt")
    return names


def generate(manifest, source, ns_per_entry, output, port_dir):
    """Write charged_table.c: the declared work scale and the charged names. The scale is the project's one explicit
    value, spelled as a positive decimal; there is no default."""
    if not NS_PER_ENTRY.fullmatch(ns_per_entry) or not float(ns_per_entry) > 0:
        raise ValueError(f"the work scale must be a positive decimal number of ns per entry, got {ns_per_entry!r}")
    project = json.loads(manifest.read_text())
    functions, _ = symbol_table(source, project["version"])
    native_names = native_functions(port_dir)
    native = {symbol: target for symbol, target in project["bindings"].items() if target in native_names}
    bound = set(native) | set(native.values())  # a binding to another original function is no native code
    names = charged_names(bound, native_names, functions)
    if not names:
        raise ValueError("no charged function: the version's symbols hold no original code outside the SDK")
    lines = ["/* Generated guest work declaration (charged.py): the work scale and the charged names. */",
             f'const char n64c_guest_ns_per_entry[] = "{ns_per_entry}";', "const char *const n64c_charged_names[] = {"]
    lines += [f'    "{name}",' for name in names]
    lines += ["};", f"const unsigned n64c_charged_count = {len(names)};", ""]
    output.mkdir(parents=True, exist_ok=True)
    temporary = output / "charged_table.c.tmp"
    temporary.write_text("\n".join(lines))
    temporary.replace(output / "charged_table.c")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--source", type=pathlib.Path, required=True)
    parser.add_argument("--ns-per-entry", required=True, help="guest ns one charged entry costs, a positive decimal")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--port-dir", type=pathlib.Path, required=True, help="the port's facts directory (sched_glue.c)")
    args = parser.parse_args()
    try:
        generate(args.manifest, args.source, args.ns_per_entry, args.output, args.port_dir)
    except (ValueError, OSError, KeyError) as error:
        sys.exit(f"charged: {error}")
