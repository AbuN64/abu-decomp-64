"""Generated cases for `behave`: permutations of the inputs of real call states, with no per-function code.

A real case is a call state captured from a movie (`hits`). The inputs of the function are its argument registers and
the memory words the original read while running the real cases. Each input is a factor whose domain is
  * the values seen in the real cases,
  * the type-derived values (enum members from the headers, each observed flag bit flipped, 0/1/-1/min/max, float
    zero/negative zero/denormal/infinity/NaN, NULL for a pointer),
  * the boundaries between observed values (one past, one before, the midpoint).
Cases take one factor off its real value at a time, then all pairs of factor values are covered (greedily, bounded by
the budget); when the whole cross product is small it is run in full. Each case is a real case plus register changes
and memory patches, so the original and the port run the same bytes and are compared strictly.
"""

from __future__ import annotations

import itertools
import random
import re
from pathlib import Path
from typing import Any

from abu_decomp.behave import inputs, prototype

RAM_BASE = 0x80000000
CROSS_LIMIT = 96  # the full cross product of the domains runs when it has at most this many rows
DOMAIN_CAP = 10  # values per input
MEMORY_INPUTS = 12  # most-read memory words varied
SEED = 0x5EED
_ARG_REGISTER = {"a0": "r4", "a1": "r5", "a2": "r6", "a3": "r7"}
_ENUM = re.compile(r"enum\s*(\w*)\s*\{([^}]*)\}\s*(\w*)\s*;", re.S)
_FLOATS = {
    32: (0x00000000, 0x80000000, 0x00000001, 0x7F800000, 0xFF800000, 0x7FC00000, 0x3F800000, 0xBF800000),
    64: (
        0,
        1 << 63,
        1,
        0x7FF0000000000000,
        0xFFF0000000000000,
        0x7FF8000000000000,
        0x3FF0000000000000,
        0xBFF0000000000000,
    ),
}


def is_real(case: dict[str, Any]) -> bool:
    """A call state captured from the movie (the first entry or a later one), not a generated or moved one."""
    labels = case.get("labels", {})
    return "seed" in labels and set(labels) <= {"seed", "hit"}


def stack_of(case: dict[str, Any], stack: tuple[int, int], window: int) -> tuple[int, int]:
    """The frame range of one case: below its own entry sp when a real later entry set one."""
    sp = case.get("registers", {}).get("r29")
    if sp is None:
        return stack
    value = int(str(sp), 0) & 0xFFFFFFFF
    return (value - window, value + 0x10) if value >= RAM_BASE else stack


class _Image:
    """The first entry's RDRAM and one case's patches over it."""

    def __init__(self, seeded: dict[str, Any]) -> None:
        item = seeded["memory"][0]
        self.base = int(str(item["addr"]), 0)
        try:
            self.data = Path(item["file"]).read_bytes()
        except OSError:
            self.data = b""

    def read(self, case: dict[str, Any], address: int, width: int) -> int | None:
        raw = None
        for patch in case.get("memory", []):
            if "hex" not in patch:
                continue
            at = int(str(patch["addr"]), 0)
            blob = bytes.fromhex(patch["hex"])
            if at <= address and address + width <= at + len(blob):
                raw = blob[address - at : address - at + width]
        if raw is None:
            offset = address - self.base
            if offset < 0 or offset + width > len(self.data):
                return None
            raw = self.data[offset : offset + width]
        return int.from_bytes(raw, "big")


def _enum_members(project: Path, headers: tuple[str, ...], ctype: str) -> list[int]:
    """Values of the enum named `ctype` (its tag or typedef) from the project's headers; [] when not found."""
    name = ctype.replace("enum", "").strip()
    if not re.fullmatch(r"[A-Za-z_]\w*", name):
        return []
    for pattern in (g for g in headers if g.lower().endswith(".h")):
        for path in sorted(project.glob(pattern)):
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            if name not in text:
                continue
            for tag, body, alias in _ENUM.findall(text):
                if name in (tag, alias):
                    members = _members(body)
                    if members:
                        return members
    return []


def _members(body: str) -> list[int]:
    known: dict[str, int] = {}
    values: list[int] = []
    nxt = 0
    for item in body.split(","):
        item = re.sub(r"/\*.*?\*/|//[^\n]*", "", item, flags=re.S).strip()
        if not item:
            continue
        label, _, expr = item.partition("=")
        label = label.strip()
        if expr.strip():
            try:
                nxt = int(eval(expr.strip(), {"__builtins__": {}}, dict(known)))
            except Exception:
                return values
        known[label] = nxt
        values.append(nxt)
        nxt += 1
    return values


def _signed(value: int, bits: int) -> int:
    return value - (1 << bits) if value >= 1 << (bits - 1) else value


def _extend(value: int, bits: int, signed: bool) -> int:
    """A narrow value as the 32-bit register holds it (sign-extended when signed)."""
    if not signed or bits == 32:
        return value & 0xFFFFFFFF
    return _signed(value & ((1 << bits) - 1), bits) & 0xFFFFFFFF


def _mix(*groups: list[int]) -> list[int]:
    """Values of the groups in priority order, without repeats, at most DOMAIN_CAP."""
    out: list[int] = []
    for value in itertools.chain.from_iterable(groups):
        if value not in out:
            out.append(value)
        if len(out) >= DOMAIN_CAP:
            break
    return out


def _between(seen: list[int], bits: int, signed: bool) -> list[int]:
    mask = (1 << bits) - 1
    key = (lambda v: _signed(v, bits)) if signed else (lambda v: v)
    ordered = sorted(set(seen), key=key)
    out = []
    for low, high in itertools.pairwise(ordered):
        a, b = key(low), key(high)
        out += [(a + 1) & mask, (b - 1) & mask, ((a + b) // 2) & mask]
    return out


def _integer(seen: list[int], bits: int, signed: bool, members: list[int]) -> list[int]:
    mask = (1 << bits) - 1
    flags = 0
    for value in seen:
        flags |= value & mask
    flips = [seen[0] ^ (1 << bit) for bit in range(bits) if flags >> bit & 1] if seen else []
    limits = [0, 1, mask]
    limits += [(1 << (bits - 1)) - 1, 1 << (bits - 1)] if signed else [mask >> 1, 1 << (bits - 1)]
    nearby = [(v + d) & mask for v in seen for d in (1, -1)]
    return _mix(seen, [m & mask for m in members], limits[:3], nearby, flips, _between(seen, bits, signed), limits[3:])


def _low(v: int) -> int:
    """The 32-bit address of a register value; the registers hold pointers sign-extended (0xffffffff801b...)."""
    return v & 0xFFFFFFFF if v >> 32 in (0, 0xFFFFFFFF) else v


def _pointerish(seen: list[int]) -> bool:
    return bool(seen) and all(v == 0 or RAM_BASE <= _low(v) < RAM_BASE + (64 << 20) for v in seen)


def _floatish(seen: list[int]) -> bool:
    """Whether every non-zero word the real cases held has a float exponent (1e-19..1e19): an integer limit such as
    0xffffffff only makes it a NaN the original refuses (28 of 256 generated cases of one function, TOOL-SPEED-14)."""
    words = [v for v in seen if v]
    return bool(words) and all(0x20000000 <= v & 0x7FFFFFFF < 0x60000000 for v in words)


def _dereferenced(seen: list[int], reads: set[int]) -> bool:
    """Whether the original read just behind a non-null pointer value the real cases held: it is used, so NULL only
    faults it (96 of 100 generated cases of one function were unmapped-at-NULL, TOOL-SPEED-13)."""
    return any(v and any(_low(v) <= a < _low(v) + 0x400 for a in reads) for v in seen)


def _factors(
    real: list[dict[str, Any]],
    results: list[dict[str, Any]],
    image: _Image,
    proto: Any,
    project: Path,
    headers: tuple[str, ...],
    readonly: tuple[tuple[int, int], ...] = (),
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    reads = {a for r in results for a, _ in r.get("reads", [])}
    registers = [c.get("registers", {}) for c in real]
    for slot in prototype.argument_slots(proto):
        kind, place = slot.param.kind, slot.where[0]
        if kind == "struct" or len(slot.where) != 1 or place.startswith("stack:"):
            continue
        key = _ARG_REGISTER.get(place, place)
        if not all(key in r for r in registers):
            key = place
        if not all(key in r for r in registers):
            continue
        seen = [int(str(r[key]), 0) for r in registers]
        guarded: list[int] = []
        if kind in ("float", "double"):
            bits = 32 if kind == "float" else 64
            domain = _mix(seen, list(_FLOATS[bits]))
        elif kind in ("pointer", "funcptr", "word") and _pointerish(seen):
            domain = _mix(seen, [0])
            guarded = [0] if _dereferenced(seen, reads) else []
            if guarded:
                domain = [0]
        else:
            bits = {"int8": 8, "uint8": 8, "int16": 16, "uint16": 16}.get(kind, 32)
            signed = kind.startswith("int")
            domain = _mix(
                seen,
                [
                    _extend(v, bits, signed)
                    for v in _integer(
                        [v & ((1 << bits) - 1) for v in seen],
                        bits,
                        signed,
                        _enum_members(project, headers, slot.param.ctype),
                    )
                ],
            )
        out.append(
            {
                "kind": "arg",
                "name": slot.param.name,
                "register": key,
                "domain": domain,
                "bits": 64 if kind == "double" else 32,
                "alone": [v for v in guarded if v in domain],
            }
        )
    count: dict[tuple[int, int], int] = {}
    for case, result in zip(real, results, strict=True):
        low = int(str(case.get("registers", {}).get("r29", "0")), 0) & 0xFFFFFFFF
        for address, width in result.get("reads", []):
            if (
                width in (1, 2, 4)
                and address >= RAM_BASE
                and address % width == 0
                and not any(lo <= address < hi for lo, hi in readonly)  # the ROM's constants are not inputs
                and not (low >= RAM_BASE and low - 0x4000 <= address < low + 0x10)
            ):
                count[(address, width)] = count.get((address, width), 0) + 1
    ranked = sorted(count, key=lambda k: (-count[k], k))[:MEMORY_INPUTS]
    for address, width in ranked:
        seen = [v for c in real if (v := image.read(c, address, width)) is not None]
        if not seen:
            continue
        bits = 8 * width
        pointer = _pointerish(seen) and width == 4
        if pointer:
            domain = _mix(seen, [0])
        elif width == 4 and _floatish(seen):
            domain = _mix(seen, [v for v in _FLOATS[32] if v >> 23 & 0xFF != 0xFF])  # finite: NaN faults the original
        else:
            domain = _integer(seen, bits, False, [])
        guarded = [0] if pointer and _dereferenced(seen, reads) else []
        if guarded:
            domain = [0]
        out.append(
            {
                "kind": "memory",
                "name": f"{address:#010x}/{width}",
                "address": address,
                "width": width,
                "domain": domain,
                "bits": bits,
                "alone": [v for v in guarded if v in domain],
            }
        )
    return [f for f in out if len(f["domain"]) > 1 or f["alone"]]


def _apply(base: dict[str, Any], factors: list[dict[str, Any]], row: tuple[Any, ...], n: int) -> dict[str, Any]:
    registers = dict(base.get("registers", {}))
    memory = list(base.get("memory", []))
    parts = []
    moved = [(f, v) for f, v in zip(factors, row, strict=True) if v is not None]
    for factor, value in zip(factors, row, strict=True):
        if value is None:
            continue
        if factor["kind"] == "arg":
            registers[factor["register"]] = f"{value:#x}"
            parts.append(f"{factor['name']}={value:#x}")
        else:
            blob = value.to_bytes(factor["width"], "big")
            memory.append({"addr": f"{factor['address']:#x}", "hex": blob.hex()})
            parts.append(f"@{factor['name']}={value:#x}")
    case = {
        **base,
        "name": f"gen{n}-{base['name']}"[:96],
        "registers": registers,
        "labels": {"generated": ",".join(parts)[:160], "base": str(base["name"])},
    }
    if len(moved) == 1:  # one input off its real value: its outcome says whether that value is usable (see refused)
        case["labels"]["single"] = f"{moved[0][0]['name']}\t{moved[0][1]}"
    if memory:
        case["memory"] = memory
    return case


def refused(cases: list[dict[str, Any]], results: list[dict[str, Any]]) -> frozenset[tuple[str, int]]:
    """(input name, value) pairs a single-input case showed the original cannot run: an index past its table, a word
    that is read as a pointer, a value that fails a check. Combined with other inputs such a value only repeats the
    same fault (91 of 150 cases of one function were `unmapped`, TOOL-SPEED-14), so the second stage leaves it out."""
    bad: set[tuple[str, int]] = set()
    for case, result in zip(cases, results, strict=True):
        single = case.get("labels", {}).get("single")
        if single and result.get("status") != "ok":
            name, _, value = single.partition("\t")
            bad.add((name, int(value)))
    return frozenset(bad)


def generate(
    cases: list[dict[str, Any]],
    results: list[dict[str, Any]],
    seeded: dict[str, Any],
    proto: Any,
    project: Path,
    headers: tuple[str, ...],
    budget: int,
    readonly: tuple[tuple[int, int], ...] = (),
    stage: str = "all",
    skip: frozenset[tuple[str, int]] = frozenset(),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Generated cases (at most `budget`) over the real cases that ran in the original, and the report of how.
    A large cross product runs in two stages so that what the first shows is used by the second: stage "singles"
    returns the one-input cases and the report says `more`; the caller runs them, takes `refused(...)` of the results
    and asks stage "pairs" with it as `skip`, which returns the pairwise cases over the values that were not refused.
    Stage "all" (the default) is both at once, with nothing refused."""
    pairs = [(c, r) for c, r in zip(cases, results, strict=True) if is_real(c) and r["status"] == "ok"]
    report: dict[str, Any] = {"budget": budget, "real_bases": len(pairs)}
    if not pairs or budget <= 0:
        return [], {**report, "generated": 0, "why": "no real case ran in the original" if not pairs else "budget 0"}
    real = [c for c, _ in pairs]
    factors = _factors(real, [r for _, r in pairs], _Image(seeded), proto, project, headers, readonly)
    report["inputs"] = [{"kind": f["kind"], "name": f["name"], "values": len(f["domain"])} for f in factors]
    if not factors:
        return [], {**report, "generated": 0, "why": "no input with more than one value"}
    # a dereferenced pointer's NULL runs once on its own (the original faults on it anywhere it is combined with others)
    # and its other observed values are other objects this base snapshot does not hold: the base keeps its own
    domains = [
        [v for v in f["domain"] if v not in f.get("alone", ()) and (f["name"], v) not in skip] or [None]
        for f in factors
    ]
    alone = [(i, v) for i, f in enumerate(factors) for v in f.get("alone", ())]
    size = 1
    for domain in domains:
        size *= len(domain)
    rows: list[tuple[Any, ...]] = []
    if size <= CROSS_LIMIT:
        report["mode"] = "cross-product"
        rows = [tuple(r) for r in itertools.product(*domains)]
        rows += [tuple(v if j == i else None for j in range(len(factors))) for i, v in alone]
    else:
        report["mode"] = "single+pairwise" if stage == "all" else stage
        none = (None,) * len(factors)
        if stage != "pairs":
            for i, domain in enumerate(domains):
                rows += [(*none[:i], v, *none[i + 1 :]) for v in domain]
        if stage != "singles":
            rng = random.Random(SEED)
            rows += [tuple(domains[i][j] for i, j in enumerate(r)) for r in inputs.pairwise(domains, rng, budget)]
        if stage != "pairs":
            rows += [tuple(v if j == i else None for j in range(len(factors))) for i, v in alone]
        if stage == "singles":
            report["more"] = True
        if stage == "pairs":
            report["refused"] = sorted(f"{n}={v:#x}" for n, v in skip)[:24]
    generated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for n, row in enumerate(rows):
        base = real[n % len(real)]
        case = _apply(base, factors, row, n + (1 << 12 if stage == "pairs" else 0))
        sig = repr((case["registers"], [(m["addr"], m["hex"]) for m in case.get("memory", [])], case["labels"]["base"]))
        if sig in seen or case["labels"]["generated"] == "":
            continue
        seen.add(sig)
        generated.append(case)
        if len(generated) >= budget:
            break
    report["generated"] = len(generated)
    return generated, report
