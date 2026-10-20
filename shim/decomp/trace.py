# SPDX-License-Identifier: GPL-3.0-or-later
"""Compile the oracle's boot trace into the table the hit census reads (trace.c)."""
import argparse
import csv
import pathlib
import re
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import policy  # noqa: E402

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
    project = policy.load_project(manifest, source)
    by_address = {}
    for line in (source / "versions" / project["version"] / "symbol_addrs.txt").read_text().splitlines():
        match = re.match(r"\s*(\w+)\s*=\s*(0x[0-9A-Fa-f]+)\s*;", line)
        if match:
            by_address.setdefault(int(match[2], 16), []).append(match[1])
    bound = set(project["bindings"])
    lines = ['/* Generated boot trace table; addresses and names only. */', '#include "trace.h"',
             "const N64cTraceEntry n64c_trace_entries[] = {"]
    entries = rows(trace)
    for order, address, names in entries:
        names = list(dict.fromkeys(names + by_address.get(address, [])))
        if len(names) > MAX_NAMES or not all(IDENT.fullmatch(n) for n in names):
            raise ValueError(f"trace order {order}: unusable names {names}")
        shim = int(any(n in bound for n in names))
        quoted = ", ".join(f'"{n}"' for n in names)
        lines.append(f"    {{{order}, 0x{address:08x}u, {shim}, {{{quoted}}}}},")
    lines += ["};", f"const unsigned n64c_trace_count = {len(entries)};", ""]
    output.mkdir(parents=True, exist_ok=True)
    temporary = output / "trace_table.c.tmp"
    temporary.write_text("\n".join(lines))
    temporary.replace(output / "trace_table.c")


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
