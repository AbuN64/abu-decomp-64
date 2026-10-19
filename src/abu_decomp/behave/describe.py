"""What the original does with its arguments, read from its own runs: the struct-typing helper.

For each pointer-like argument the offsets read and written, their widths, and which offsets held a pointer (a
case that seeded another region's address there and then touched that region). Reads and writes outside the
argument regions are listed against the nearest known symbol.
"""

from __future__ import annotations

import bisect
import struct
from typing import Any

from abu_decomp.behave import inputs
from abu_decomp.behave.inputs import Region
from abu_decomp.behave.prototype import Prototype, argument_slots


def learn(cases: list[dict[str, Any]], regions: list[Region]) -> dict[int, list[tuple[int, int]]]:
    """Offsets (and widths) of each region the original read or wrote, from the cases of a first probe."""
    seen: dict[int, set[tuple[int, int]]] = {r.index: set() for r in regions}
    for case in cases:
        touched = [(a, w) for a, w in [*case["reads"], *case["unseeded"]]]
        touched += [(e["addr"], e["w"]) for e in case["events"] if e["k"] == "st"]
        for address, width in touched:
            for region in regions:
                if region.base <= address < region.base + region.size:
                    seen[region.index].add((address - region.base, width))
    return {index: sorted(offsets) for index, offsets in seen.items()}


def _field(fields: dict[int, dict[str, Any]], offset: int) -> dict[str, Any]:
    return fields.setdefault(
        offset, {"offset": offset, "widths": set(), "read": 0, "written": 0, "stored": set(), "points_to": set()}
    )


def pointer_fields(
    cases: list[dict[str, Any]],
    regions: list[Region],
    known: tuple[tuple[int, int], ...],
    tagged: tuple[int, ...],
    limit: int = 8,
) -> list[tuple[int, int]]:
    """Fields (region, offset) and globals (-1, address) the original dereferenced: a learning case that ended
    unmapped at a fault tag plus a displacement names the word that held the tag."""
    found: list[tuple[int, int]] = []
    for case in cases:
        if case["status"] not in ("unmapped", "mmio"):
            continue
        try:
            bad = int(case["detail"].split()[1], 16)
        except (IndexError, ValueError):
            continue
        tag = inputs.decode_tag(bad)
        if tag is None:
            continue
        kind, first, offset = tag
        if kind == "field" and first < len(regions):
            key = (first, offset)
        elif kind == "global" and first < len(tagged):
            key = (-1, tagged[first])
        else:
            continue
        if key not in known and key not in found and len(known) + len(found) < limit:
            found.append(key)
    return found


def read_globals(
    cases: list[dict[str, Any]], regions: list[Region], stack: tuple[int, int], limit: int = 16
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Word-aligned global addresses the original read in `cases` (the most often read first), and those among
    them that sit in memory the image does not fill: the program's zeroed state."""
    count: dict[int, int] = {}
    state: set[int] = set()
    for case in cases:
        for unseeded, rows in ((False, case["reads"]), (True, case["unseeded"])):
            for address, width in rows:
                if (
                    width == 4
                    and address >= 0x80000000
                    and address & 3 == 0
                    and not stack[0] <= address < stack[1]
                    and not any(r.base <= address < r.base + r.size for r in regions)
                ):
                    count[address] = count.get(address, 0) + 1
                    if unseeded:
                        state.add(address)
    ranked = [a for a, _ in sorted(count.items(), key=lambda kv: (-kv[1], kv[0]))]
    return tuple(ranked[:limit]), tuple(a for a in ranked if a in state)[:8]


def _seeded_word(case: dict[str, Any], region: Region, offset: int) -> int | None:
    for patch in case.get("inputs", {}).get("memory", []):
        if int(patch["addr"]) == region.base:
            data = bytes.fromhex(patch["hex"])
            if offset + 4 <= len(data):
                return struct.unpack(">I", data[offset : offset + 4])[0]
    return None


def describe(
    cases: list[dict[str, Any]],
    regions: list[Region],
    proto: Prototype,
    symbols: list[tuple[int, str]],
    stack: tuple[int, int],
    pointers: tuple[tuple[int, int], ...] = (),
) -> dict[str, Any]:
    """A struct-typing summary of the original over its testable cases."""
    ok = [c for c in cases if c["status"] == "ok"]
    owner = {}
    for slot in argument_slots(proto):
        if slot.param.kind in ("pointer", "word", "struct"):
            owner[len(owner)] = slot
    names: dict[int, str] = {i: s.param.name for i, s in owner.items()}
    for number, (parent, offset) in enumerate(pointers):
        child = len(owner) + number
        names[child] = f"{names.get(parent, f'region{parent}')}+0x{offset:X}->" if parent >= 0 else f"*0x{offset:08X}->"
    args = []
    for region in regions:
        fields: dict[int, dict[str, Any]] = {}

        pointers = {r.base: r.index for r in regions}
        for case in ok:
            written = {e["addr"] for e in case["events"] if e["k"] == "st"}
            for address, width in [*case["reads"], *case["unseeded"]]:
                if region.base <= address < region.base + region.size:
                    row = _field(fields, address - region.base)
                    row["widths"].add(width)
                    row["read"] += 1
            for event in case["events"]:
                if event["k"] == "st" and region.base <= event["addr"] < region.base + region.size:
                    row = _field(fields, event["addr"] - region.base)
                    row["widths"].add(event["w"])
                    row["written"] += 1
                    if len(row["stored"]) < 4:
                        row["stored"].add(event["value"])
            for offset in list(fields):
                held = _seeded_word(case, region, offset & ~3)
                target = pointers.get(held & ~0xFFF) if held is not None and held & 0xFFF == 0 else None
                if target is not None and target != region.index:
                    base = regions[target].base
                    touched = any(
                        base <= a < base + regions[target].size for a, _ in [*case["reads"], *case["unseeded"]]
                    ) or any(base <= a < base + regions[target].size for a in written)
                    if touched:
                        fields[offset]["points_to"].add(target)
        slot = owner.get(region.index)
        args.append(
            {
                "argument": names.get(region.index, f"region{region.index}"),
                "type": slot.param.ctype if slot else "",
                "register": ",".join(slot.where) if slot else "",
                "fields": [
                    {
                        "offset": f"0x{o:X}",
                        "widths": sorted(f["widths"]),
                        "reads": f["read"],
                        "writes": f["written"],
                        "pointer_to_argument": sorted(names.get(t, f"region{t}") for t in f["points_to"]),
                        **({"stored_values": sorted(f"0x{v:X}" for v in f["stored"])} if f["stored"] else {}),
                    }
                    for o, f in sorted(fields.items())
                ],
            }
        )
    return {
        "testable_cases": len(ok),
        "arguments": args,
        "globals": _globals(ok, regions, symbols, stack),
        "calls": _callees(ok),
    }


def _globals(
    cases: list[dict[str, Any]], regions: list[Region], symbols: list[tuple[int, str]], stack: tuple[int, int]
) -> list[dict[str, Any]]:
    keys = [a for a, _ in symbols]
    table: dict[tuple[str, int, int], dict[str, int]] = {}
    for case in cases:
        rows = [(a, w, "reads") for a, w in [*case["reads"], *case["unseeded"]]]
        rows += [(e["addr"], e["w"], "writes") for e in case["events"] if e["k"] == "st"]
        for address, width, kind in rows:
            if any(r.base <= address < r.base + r.size for r in regions) or stack[0] <= address < stack[1]:
                continue
            position = bisect.bisect_right(keys, address) - 1
            name, offset = (symbols[position][1], address - symbols[position][0]) if position >= 0 else ("?", address)
            entry = table.setdefault((name, offset, width), {"reads": 0, "writes": 0})
            entry[kind] += 1
    return [
        {"symbol": name, "offset": f"0x{offset:X}", "width": width, **counts}
        for (name, offset, width), counts in sorted(table.items(), key=lambda kv: -sum(kv[1].values()))[:60]
    ]


def _callees(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, int] = {}
    for case in cases:
        for event in [e for e in case["events"] if e["k"] in ("call", "stub")][1:]:
            seen[event["name"]] = seen.get(event["name"], 0) + 1
    return [{"callee": name, "calls": n} for name, n in sorted(seen.items(), key=lambda kv: -kv[1])]
