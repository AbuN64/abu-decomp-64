"""Compare what two runs of the same case did: the original and a candidate.

Hard differences (any one makes a case `differs`): the returned value the prototype says is meaningful, the net
change to memory (stack frame excluded), the ordered calls with the arguments their prototypes say are read, and
callee-saved registers the candidate leaves changed. Soft notes (never a verdict): reads outside the seeded
memory that only one side makes, calls whose callee has no prototype, stores that moved across a call.
"""

from __future__ import annotations

from typing import Any

from abu_decomp.behave.prototype import Prototype, argument_slots

PREFIX = "behave_"
MASKS = {"int8": 0xFF, "uint8": 0xFF, "int16": 0xFFFF, "uint16": 0xFFFF}
SAME, DIFFERS, UNTESTABLE = "same", "differs", "untestable"


def _name(name: str) -> str:
    return name.removeprefix(PREFIX)


def _calls(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Call and stub events after the entry of the function under test."""
    events = [e for e in case["events"] if e["k"] in ("call", "stub")]
    return events[1:]


def _hex(value: int, width: int = 8) -> str:
    return f"0x{value & ((1 << (4 * width)) - 1):0{width}X}"


def return_of(case: dict[str, Any], proto: Prototype) -> tuple[str, Any, bool] | None:
    """(register text, comparable value, meaningful) of the result, or None for a void function."""
    ret, regs = proto.ret, case.get("ret")
    if regs is None or ret == "void":
        return None
    if ret == "float":
        return "f0", regs["f0"] & 0xFFFFFFFF, True
    if ret == "double":
        return "f0", regs["f0"], True
    if ret in ("int64", "uint64"):
        return "v0:v1", (regs["v0"], regs["v1"]), True
    mask = MASKS.get(ret, 0xFFFFFFFF)
    return "v0", regs["v0"] & mask, ret != "word"


def _args(event: dict[str, Any], proto: Prototype | None) -> list[tuple[str, int]] | None:
    """The argument values an event carries, by the callee's prototype; None when no prototype is known."""
    if proto is None:
        return None
    a, s, f = event["args"][:4], event["args"][4:8], event["f"]
    values = []
    for slot in argument_slots(proto):
        mask = MASKS.get(slot.param.kind, 0xFFFFFFFF)
        for place in slot.where:
            if place in ("f12", "f14"):
                raw = f[0 if place == "f12" else 1]
                values.append((f"{slot.param.name}({place})", raw))
            elif place.startswith("a"):
                values.append((f"{slot.param.name}({place})", a[int(place[1:])] & mask))
            else:
                index = (int(place[6:]) - 16) // 4
                if index < len(s):
                    values.append((f"{slot.param.name}(sp+{int(place[6:])})", s[index] & mask))
    return values


def _store_after(case: dict[str, Any], address: int) -> str:
    """Where in the call order the last store to `address` happened: 'before the first call', 'after call 2 f'."""
    calls = 0
    where = "before the first call"
    for event in case["events"]:
        if event["k"] in ("call", "stub"):
            calls += 1
            where = f"after call {calls} {_name(event['name'])}" if calls > 1 else "before the first call"
        elif event["k"] == "st" and event["addr"] <= address < event["addr"] + event["w"]:
            return where
    return where


def compare_case(
    original: dict[str, Any],
    candidate: dict[str, Any],
    proto: Prototype,
    callees: dict[str, Prototype],
    stack: tuple[int, int],
    own: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """The verdict of one case. `stack` is the [low, high) frame range excluded from the memory effect; `own` the
    candidate image range (its own statics, never comparable)."""
    result: dict[str, Any] = {
        "name": original["name"],
        "verdict": SAME,
        "reason": None,
        "difference": None,
        "notes": [],
    }
    if original["status"] != "ok":
        result.update(verdict=UNTESTABLE, reason=f"original {original['status']}: {original['detail']}")
        return result
    if candidate["status"] != "ok":
        result.update(
            verdict=DIFFERS,
            reason=f"candidate {candidate['status']} where the original returned: {candidate['detail']}",
            difference={"kind": "status", "got": candidate["status"], "detail": candidate["detail"]},
        )
        return result

    def differ(kind: str, text: str, **fields: Any) -> dict[str, Any]:
        result.update(verdict=DIFFERS, reason=text, difference={"kind": kind, **fields})
        return result

    if extra := [r for r in candidate.get("saved", []) if r not in original.get("saved", [])]:
        return differ("clobber", f"candidate leaves callee-saved {', '.join(extra)} changed", registers=extra)
    calls_a, calls_b = _calls(original), _calls(candidate)
    for index in range(max(len(calls_a), len(calls_b))):
        a = calls_a[index] if index < len(calls_a) else None
        b = calls_b[index] if index < len(calls_b) else None
        if a is None or b is None or _name(a["name"]) != _name(b["name"]):
            want = _name(a["name"]) if a else "return"
            got = _name(b["name"]) if b else "return"
            return differ(
                "call",
                f"call {index + 1}: original calls {want}, candidate {got}",
                index=index + 1,
                expected=want,
                got=got,
            )
        values_a, values_b = _args(a, callees.get(_name(a["name"]))), _args(b, callees.get(_name(b["name"])))
        if values_a is None:
            if a["args"][:4] != b["args"][:4]:
                result["notes"].append(f"call {index + 1} {_name(a['name'])} has no prototype; a0-a3 differ")
            continue
        for (label, x), (_, y) in zip(values_a, values_b or [], strict=False):
            if x != y:
                return differ(
                    "call-args",
                    f"call {index + 1} {_name(a['name'])}: {label} is {_hex(x)} in the "
                    f"original, {_hex(y)} in the candidate",
                    index=index + 1,
                    callee=_name(a["name"]),
                    argument=label,
                    expected=x,
                    got=y,
                )

    def effect(case: dict[str, Any]) -> dict[int, int]:
        return {a: v for a, v in case["net"] if not stack[0] <= a < stack[1] and not (own and own[0] <= a < own[1])}

    net_a, net_b = effect(original), effect(candidate)
    for address in sorted(net_a.keys() | net_b.keys()):
        x, y = net_a.get(address), net_b.get(address)
        if x != y:
            text = (
                f"store {_hex(address)}: original leaves {_hex(x) if x is not None else 'it unchanged'}, candidate "
                f"{_hex(y) if y is not None else 'leaves it unchanged'} (original wrote it "
                f"{_store_after(original, address)}, candidate {_store_after(candidate, address)})"
            )
            return differ("store", text, address=address, expected=x, got=y)
    if not original.get("overflow") and not candidate.get("overflow"):
        ret_a, ret_b = return_of(original, proto), return_of(candidate, proto)
        if ret_a and ret_b and ret_a[1] != ret_b[1]:
            soft = not ret_a[2]
            shown = lambda v: _hex(v) if isinstance(v, int) else str(v)  # noqa: E731
            text = f"return {ret_a[0]}: original {shown(ret_a[1])}, candidate {shown(ret_b[1])}"
            if soft:
                result["notes"].append(text + " (return type unknown)")
            else:
                return differ("return", text, register=ret_a[0], expected=ret_a[1], got=ret_b[1])
    only_cand = sorted({tuple(x) for x in candidate["unseeded"]} - {tuple(x) for x in original["unseeded"]})
    only_orig = sorted({tuple(x) for x in original["unseeded"]} - {tuple(x) for x in candidate["unseeded"]})
    if only_cand:
        result["notes"].append(
            f"candidate reads {len(only_cand)} unseeded word(s) the original does not, first {_hex(only_cand[0][0])}"
        )
    if only_orig:
        result["notes"].append(
            f"original reads {len(only_orig)} unseeded word(s) the candidate does not, first {_hex(only_orig[0][0])}"
        )
    return result


def reason_key(reason: str | None) -> str:
    """Group untestable reasons: 'original fatal pause-self 00000000 0' -> 'original fatal pause-self'."""
    if not reason:
        return "unknown"
    head, _, detail = reason.partition(": ")
    return f"{head} {detail.split()[0]}" if detail.split() and head.endswith("fatal") else head


def overall(cases: list[dict[str, Any]], inputs: list[dict[str, str]]) -> dict[str, Any]:
    """The verdict over every case: differs when any does (first one named with its inputs), untestable when none
    could be compared, else same."""
    counts = {SAME: 0, DIFFERS: 0, UNTESTABLE: 0}
    for case in cases:
        counts[case["verdict"]] += 1
    first = next(((c, i) for c, i in zip(cases, inputs, strict=True) if c["verdict"] == DIFFERS), None)
    reasons: dict[str, int] = {}
    for case in cases:
        if case["verdict"] == UNTESTABLE:
            reasons[reason_key(case["reason"])] = reasons.get(reason_key(case["reason"]), 0) + 1
    if first:
        given = " ".join(f"{k}={v}" for k, v in first[1].items())
        verdict, reason = DIFFERS, f"{first[0]['name']} ({given}): {first[0]['reason']}"
    elif counts[SAME] == 0:
        top = max(reasons, key=reasons.get) if reasons else "no cases"
        verdict, reason = UNTESTABLE, f"no case ran to a comparison; most common: {top}"
    else:
        verdict, reason = SAME, None
    return {
        "verdict": verdict,
        "reason": reason,
        "counts": counts,
        "untestable_reasons": reasons,
        "first_difference": (
            {"case": first[0]["name"], "inputs": first[1], **first[0]["difference"]} if first else None
        ),
    }
