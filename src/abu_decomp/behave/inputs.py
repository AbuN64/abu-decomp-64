"""Deterministic input combinations for one function.

A case is a taste `cases` request row: argument registers, stack words and memory patches that seed the memory
each pointer argument points into. The set is built from the prototype alone: a baseline, every argument swept
alone over its edge values, and pairwise combinations of a reduced value set, capped. Pointed-to memory is seeded
from what a first probe of the original read, with the sweep values placed at exactly those offsets.
"""

from __future__ import annotations

import itertools
import random
import struct
from dataclasses import dataclass
from typing import Any

from abu_decomp.behave.prototype import Prototype, Slot, argument_slots

FULL = {
    "int8": [1, 0, -1, 127, -128, 2],
    "uint8": [1, 0, 255, 127, 128, 2],
    "int16": [1, 0, -1, 32767, -32768, 100],
    "uint16": [1, 0, 65535, 32767, 32768, 100],
    "int32": [1, 0, -1, 2, 0x7FFFFFFF, -0x80000000, 16, 100, 255, 256, 0x1234],
    "uint32": [1, 0, 0xFFFFFFFF, 2, 0x7FFFFFFF, 0x80000000, 16, 100, 255, 256, 0x1234],
    "int64": [1, 0, -1, 0x7FFFFFFF, -0x80000000, 0x100000000, 2**63 - 1, -(2**63)],
    "uint64": [1, 0, 0xFFFFFFFF, 0x100000000, 2**64 - 1, 2**63],
    "float": [1.0, 0.0, -1.0, 0.5, 2.0, 100.0, -100.0, 0.001, 1.0e6, 3.14159],
    "double": [1.0, 0.0, -1.0, 0.5, 2.0, 100.0, -100.0, 0.001, 1.0e6, 3.14159],
}
SMALL = {
    "int8": [1, 0, -1, 127],
    "uint8": [1, 0, 255, 128],
    "int16": [1, 0, -1, 32767],
    "uint16": [1, 0, 65535, 32768],
    "int32": [1, 0, -1, 0x7FFFFFFF],
    "uint32": [1, 0, 0xFFFFFFFF, 0x7FFFFFFF],
    "int64": [1, 0, -1, 0x100000000],
    "uint64": [1, 0, 0xFFFFFFFF, 0x100000000],
    "float": [1.0, 0.0, -1.0, 100.0],
    "double": [1.0, 0.0, -1.0, 100.0],
}
_WORDS = [0, 1, 2, 3, 4, 8, 16, 100]


def is_nan_word(word: int) -> bool:
    """Whether the 32-bit word is a float NaN or infinity (the generated code asserts on a NaN operand)."""
    return (word >> 23) & 0xFF == 0xFF


_STATE = [1, 2, 3, 0, 4, 0xFFFFFFFF, 8, 0x10, 0x100, 0x3F800000, 100, 0x7FFFFFFF, 5, 6, 7]
_NORMAL_FLOATS = [0x3F800000, 0x3F000000, 0x40000000, 0xBF800000, 0x42C80000, 0x40400000, 0xC0000000, 0x3FC00000]
_FLOAT_BITS = [0x3F800000, 0x3F000000, 0x40000000, 0xBF800000, 0x42C80000, 0x00000000]
_HOT = [
    0,
    1,
    0xFFFFFFFF,
    0x7FFFFFFF,
    0x80000000,
    2,
    16,
    0x100,
    0x7FFF,
    0x8000,
    0xFFFF,
    0x3F800000,
    0xBF800000,
    0x42C80000,
    0x01010101,
    0x80808080,
]


@dataclass(frozen=True)
class Region:
    index: int
    base: int
    size: int


@dataclass(frozen=True)
class Learned:
    """What probing the original taught: offsets it touches in each region, and the pointer fields that need a
    region of their own. A pointer field is (region index, offset), or (-1, address) for a global."""

    hot: tuple[tuple[int, tuple[tuple[int, int], ...]], ...] = ()
    pointers: tuple[tuple[int, int], ...] = ()
    globals: tuple[int, ...] = ()  # global words the original read; tagged in learning runs to find pointers
    state: tuple[int, ...] = ()  # of those, the ones in memory the image does not fill: swept
    stubs: tuple[str, ...] = ()  # callees cut off at the boundary: their return values are tagged to find pointers
    returns: tuple[str, ...] = ()  # of those, the stubs whose return value the original dereferenced
    finite: bool = False  # the original hit a NaN check on the way: no filler or sweep word is a NaN as a float
    rom: tuple[tuple[int, int], ...] = ()  # (address, word) of ROM read-only state: seeded

    def offsets(self, region: int) -> list[tuple[int, int]]:
        return list(dict(self.hot).get(region, ()))


LIMIT = 8  # pointer words found (fields and globals), and stub return values found, per function

# Learning puts a fault tag into each word whose use is in question: a global, a field of a region, the return
# value of a stub. A tag is an address nothing maps, in the middle of a span as wide as a load/store
# displacement, so the faulting address (tag plus displacement) names the word it came from.
TAG_BASE = 0x60000000
TAG_SPAN = 0x10000
TAG_END = 0x80000000


@dataclass(frozen=True)
class Tags:
    """The numbered words of one learning round: ("field", (region, offset)), ("global", address), ("stub", name)."""

    order: tuple[tuple[str, Any], ...] = ()

    @staticmethod
    def of(learned: Learned) -> Tags:
        fields = sorted({(region, offset & ~3) for region, offsets in learned.hot for offset, _ in offsets})
        known = {a for p, a in learned.pointers if p == -1}
        rows: list[tuple[str, Any]] = [("field", f) for f in fields]
        rows += [("global", a) for a in learned.globals if a not in known]
        rows += [("stub", n) for n in learned.stubs if n not in learned.returns]
        return Tags(tuple(rows[: (TAG_END - TAG_BASE) // TAG_SPAN]))

    def value(self, kind: str, key: Any) -> int:
        return TAG_BASE + self.order.index((kind, key)) * TAG_SPAN + TAG_SPAN // 2

    def decode(self, address: int) -> tuple[str, Any] | None:
        """The word a faulting address came from, or None when it is no tag's span."""
        if not TAG_BASE <= address < TAG_END:
            return None
        number = (address - TAG_BASE) // TAG_SPAN
        return self.order[number] if number < len(self.order) else None


@dataclass(frozen=True)
class Layout:
    arena: int = 0x80A00000
    spacing: int = 0x1000
    size: int = 0x400

    def region(self, index: int) -> Region:
        return Region(index, self.arena + index * self.spacing, self.size)

    def end(self, count: int) -> int:
        return self.arena + (count + 1) * self.spacing

    def reach(self, regions: list[Region]) -> int:
        """The end of memory that holds every one of `regions` (their indexes need not be consecutive)."""
        return self.end(max((r.index + 1 for r in regions), default=0))


def _domain(slot: Slot, regions: dict[str, Region]) -> list[Any]:
    """Every value tried for one argument, the baseline first."""
    kind = slot.param.kind
    if kind == "pointer":
        return [regions[_key(slot)], 0]
    if kind == "funcptr":
        return [0]
    if kind in ("word", "struct"):
        return [regions[_key(slot)], 0, 1, -1, 2, 0x7FFFFFFF]
    return list(FULL[kind])


def _key(slot: Slot) -> str:
    return slot.param.name + "@" + ",".join(slot.where)


def _core(domain: list[Any]) -> list[int]:
    """The few value indexes combined pairwise: the baseline, zero, one more edge and the largest extreme."""
    return sorted({i for i in (0, 1, 2, min(4, len(domain) - 1)) if i < len(domain)})


def _value(slot: Slot, value: Any) -> tuple[dict[str, int], dict[int, int]]:
    """Register and stack-word assignments for one argument value."""
    kind = slot.param.kind
    if isinstance(value, Region):
        words = [value.base]
    elif kind == "float":
        words = [struct.unpack(">I", struct.pack(">f", value))[0]]
    elif kind == "double":
        words = list(struct.unpack(">II", struct.pack(">d", value)))
    elif slot.param.words == 2:
        number = int(value) & 0xFFFFFFFFFFFFFFFF
        words = [number >> 32, number & 0xFFFFFFFF]
    else:
        words = [int(value) & 0xFFFFFFFF]
    regs: dict[str, int] = {}
    stack: dict[int, int] = {}
    if slot.where[0] in ("f12", "f14"):
        regs[slot.where[0]] = words[0] << 32 | words[1] if kind == "double" else words[0]
        return regs, stack
    for place, word in zip(slot.where, words, strict=True):
        if place.startswith("stack:"):
            stack[int(place[6:])] = word
        else:
            regs[place] = word
    return regs, stack


def _label(value: Any) -> str:
    if isinstance(value, Region):
        return f"region{value.index}"
    if isinstance(value, float):
        return repr(value)
    return str(value) if isinstance(value, int) and abs(value) < 4096 else hex(int(value) & 0xFFFFFFFFFFFFFFFF)


def pairwise(domains: list[Any], rng: random.Random, limit: int) -> list[tuple[int, ...]]:
    """Rows of value indexes covering every pair of values of any two columns, greedily, at most `limit` rows."""
    columns = len(domains)
    if columns < 2:
        return []
    uncovered = {
        (i, a, j, b)
        for i, j in itertools.combinations(range(columns), 2)
        for a in range(len(domains[i]))
        for b in range(len(domains[j]))
    }
    rows: list[tuple[int, ...]] = []
    while uncovered and len(rows) < limit:
        i, a, j, b = min(uncovered)
        best, gain = None, -1
        for _ in range(24):
            row = [rng.randrange(len(domain)) for domain in domains]
            row[i], row[j] = a, b
            score = sum((x, row[x], y, row[y]) in uncovered for x, y in itertools.combinations(range(columns), 2))
            if score > gain:
                best, gain = tuple(row), score
        assert best is not None
        rows.append(best)
        uncovered -= {(x, best[x], y, best[y]) for x, y in itertools.combinations(range(columns), 2)}
    return rows


def plan(proto: Prototype, count: int, seed: int, layout: Layout) -> tuple[list[dict[str, Any]], list[Region]]:
    """Up to `count` argument assignments (no memory yet) and the regions the pointer arguments use."""
    slots = argument_slots(proto)
    rng = random.Random(f"{seed}:{proto.name}:plan")
    regions: dict[str, Region] = {}
    for slot in slots:
        if slot.param.kind in ("pointer", "word", "struct"):
            regions[_key(slot)] = layout.region(len(regions))
    domains = [_domain(s, regions) for s in slots]
    cores = [_core(d) for d in domains]
    baseline = tuple(0 for _ in slots)
    nulls: list[tuple[int, ...]] = []
    singles: list[tuple[int, ...]] = []
    for i, domain in enumerate(domains):
        for v in range(1, len(domain)):
            row = tuple(v if k == i else 0 for k in range(len(slots)))
            is_null = slots[i].param.kind in ("pointer", "word", "struct") and domain[v] == 0
            (nulls if is_null else singles).append(row)
    pairs = [
        tuple(cores[i][v] for i, v in enumerate(row)) for row in pairwise([range(len(c)) for c in cores], rng, count)
    ]
    ordered = [baseline, *nulls]
    for pair in itertools.zip_longest(singles, pairs):
        ordered.extend(r for r in pair if r is not None)
    chosen = list(dict.fromkeys(ordered))[:count]
    cases = []
    for number, row in enumerate(chosen):
        registers: dict[str, int] = {}
        stack: dict[int, int] = {}
        labels: dict[str, str] = {}
        for slot, domain, v in zip(slots, domains, row, strict=True):
            regs, words = _value(slot, domain[v])
            registers.update(regs)
            stack.update(words)
            labels[slot.param.name] = _label(domain[v])
        size = (max(stack) - 12) // 4 if stack else 0
        cases.append(
            {
                "name": f"c{number}",
                "registers": registers,
                "stack": [stack.get(16 + 4 * i, 0) for i in range(size)],
                "labels": labels,
            }
        )
    return cases, sorted(set(regions.values()), key=lambda r: r.index)


def fill(
    rng: random.Random,
    size: int,
    hot: list[tuple[int, int]],
    step: int,
    bases: list[int],
    pointers: dict[int, int] | None = None,
    tags: dict[int, int] | None = None,
    tag_all: bool = False,
    finite: bool = False,
) -> bytes:
    """A region's bytes: benign filler everywhere, sweep values at the offsets the original is known to read, and
    at its pointer fields the base of the region they point to (null in every fifth case)."""
    words = []
    for _ in range(size // 4):
        roll = rng.random()
        words.append(
            rng.choice(_WORDS) if roll < 0.7 else rng.choice(_FLOAT_BITS) if roll < 0.85 else rng.getrandbits(32)
        )
    if finite:
        # the original asserted on a NaN operand; the integer filler (0, 1, 2 are zero and denormals as floats) made
        # singular matrices and zero divisors, so most filler words become ordinary floats (InvertMatrix: 23 of 26
        # untestable cases were the NaN check, TOOL-SPEED-13)
        words = [rng.choice(_NORMAL_FLOATS) if rng.random() < 0.75 else w for w in words]
        words = [w if not is_nan_word(w) else 0 for w in words]
    pool = [w for w in (*_HOT, *bases) if not (finite and is_nan_word(w))]
    if (
        finite
    ):  # the swept offsets of a function that asserts on NaN are float operands: ordinary floats, not 0/1/2 denormals
        pool = [*_NORMAL_FLOATS, *_NORMAL_FLOATS, *pool[:4]]
    targets = pointers or {}
    for position, offset in enumerate(sorted({o & ~3 for o, _ in hot})):
        if offset + 4 <= size and offset not in targets:
            words[offset // 4] = pool[(step + position * 5) % len(pool)]
    for offset, base in targets.items():
        if offset + 4 <= size:
            words[offset // 4] = 0 if step % 5 == 4 else base
    for offset, tag in (tags or {}).items():
        if offset + 4 <= size and offset not in targets and (tag_all or rng.random() < 0.5):
            words[offset // 4] = tag
    return b"".join(struct.pack(">I", w & 0xFFFFFFFF) for w in words)


def seeded_cases(
    proto: Prototype, count: int, seed: int, layout: Layout, learned: Learned | None = None, learn: bool = False
) -> tuple[list[dict[str, Any]], list[Region]]:
    """The final case list: `plan` plus each case's memory (hot offsets swept, pointer fields aimed at regions).
    With `learn` about half of the hot words (and read globals) hold fault tags instead, to find the pointers."""
    cases, regions = plan(proto, count, seed, layout)
    learned = learned or Learned()
    if cases and (learn or learned.hot or learned.state):
        # fewer argument rows than asked: the memory the rows point at and the state globals carry the variety;
        # a learning round repeats them too, so that every global and stub is tagged in some cases and not others
        pointers = [p.name for p in proto.params if p.kind in ("pointer", "word", "struct")]
        valid = [c for c in cases if all(c["labels"][n].startswith("region") for n in pointers)] or cases
        if len(cases) < count:
            cases += [dict(valid[i % len(valid)], name=f"c{len(cases) + i}") for i in range(count - len(cases))]
    arguments = len(regions)
    children = [layout.region(arguments + i) for i in range(len(learned.pointers))]
    answers = [layout.region(arguments + LIMIT + i) for i in range(len(learned.returns))]  # after the room for children
    regions = [*regions, *children, *answers]
    bases = [r.base for r in regions]
    tags = Tags.of(learned)
    for number, case in enumerate(cases):
        memory = []
        for region in regions:
            rng = random.Random(f"{seed}:{proto.name}:{number}:{region.index}")
            aimed = {
                offset: children[i].base
                for i, (parent, offset) in enumerate(learned.pointers)
                if parent == region.index
            }
            marks = (
                {o & ~3: tags.value("field", (region.index, o & ~3)) for o, _ in learned.offsets(region.index)}
                if learn
                else None
            )
            data = fill(
                rng,
                region.size,
                learned.offsets(region.index),
                number,
                bases,
                aimed,
                marks,
                number == 0,
                learned.finite,
            )
            memory.append({"addr": region.base, "hex": data.hex()})
        if learn:
            rng = random.Random(f"{seed}:{proto.name}:{number}:globals")
            known = {a for p, a in learned.pointers if p == -1}
            memory += [
                {"addr": address, "hex": struct.pack(">I", tags.value("global", address)).hex()}
                for address in learned.globals
                if address not in known and (number == 0 or rng.random() < 0.5)
            ]
            case["stub_v0"] = {
                name: tags.value("stub", name)
                for name in learned.stubs
                if name not in learned.returns and (number == 0 or rng.random() < 0.5)
            }
        if not learn:
            known = {a for p, a in learned.pointers if p == -1}
            memory += [{"addr": a, "hex": struct.pack(">I", w).hex()} for a, w in learned.rom]
            fixed = {a for a, _ in learned.rom}
            for i, address in enumerate(a for a in learned.state if a not in known and a not in fixed):
                if (number + i) % 3:
                    word = _STATE[(number * 7 + i * 3) % len(_STATE)]
                    if learned.finite and is_nan_word(word):
                        word = 0x3F800000
                    memory.append({"addr": address, "hex": struct.pack(">I", word).hex()})
        for i, (parent, address) in enumerate(learned.pointers):
            if parent == -1:  # a global that held a null the original dereferenced
                word = 0 if number % 5 == 4 else children[i].base
                memory.append({"addr": address, "hex": struct.pack(">I", word).hex()})
        case["memory"] = memory
        if learned.returns:  # a stub the original dereferenced answers with a region (null every fifth case)
            case["stub_v0"] = {
                **case.get("stub_v0", {}),
                **{name: 0 if number % 5 == 4 else answers[i].base for i, name in enumerate(learned.returns)},
            }
    return cases, regions


_ARG_REGISTER = {"a0": "r4", "a1": "r5", "a2": "r6", "a3": "r7"}


def _nearby(kind: str, value: int) -> list[tuple[str, int]]:
    """Values next to a captured argument register value: the bits for a float, the 32-bit word otherwise."""
    if kind == "float":
        number = struct.unpack(">f", struct.pack(">I", value & 0xFFFFFFFF))[0]
        moved = [("x2", number * 2), ("neg", -number), ("zero", 0.0)]
        return [(label, struct.unpack(">I", struct.pack(">f", x))[0]) for label, x in moved]
    if kind == "double":
        number = struct.unpack(">d", struct.pack(">Q", value & 0xFFFFFFFFFFFFFFFF))[0]
        moved = [("x2", number * 2), ("neg", -number), ("zero", 0.0)]
        return [(label, struct.unpack(">Q", struct.pack(">d", x))[0]) for label, x in moved]
    word = value & 0xFFFFFFFF
    return [("plus1", (word + 1) & 0xFFFFFFFF), ("minus1", (word - 1) & 0xFFFFFFFF), ("zero", 0)]


def around(entry: dict[str, Any], proto: Prototype, limit: int) -> list[dict[str, Any]]:
    """Cases next to the captured entry: its registers and memory, one argument moved off its real value (one more,
    one less, zero; twice, negated, zero for a float). Pointers stay where the game aimed them (the memory behind them
    is the real input), stack arguments stay in the captured stack, and every argument gets a turn before any gets a
    second one, up to `limit` cases."""
    registers = entry.get("registers", {})
    per_argument = []
    for slot in argument_slots(proto):
        kind, place = slot.param.kind, slot.where[0]
        if kind in ("pointer", "funcptr", "struct") or len(slot.where) != 1 or place.startswith("stack:"):
            continue
        name = _ARG_REGISTER.get(place, place)
        if name not in registers and place in registers:
            name = place
        if name not in registers:
            continue
        rows = []
        for label, value in _nearby(kind, int(str(registers[name]), 0)):
            rows.append(
                {
                    **entry,
                    "name": f"{entry['name']}-{slot.param.name}-{label}",
                    "registers": {**registers, name: f"{value:#x}"},
                    "labels": {**entry.get("labels", {}), "seed": entry["name"], slot.param.name: label},
                }
            )
        per_argument.append(rows)
    mixed = [row for turn in itertools.zip_longest(*per_argument) for row in turn if row is not None]
    return mixed[:limit]
