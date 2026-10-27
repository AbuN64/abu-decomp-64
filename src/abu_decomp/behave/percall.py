"""Behaviour per call: the port's calls against the original's, call by call.

Both sides write calls.jsonl (port: shim/boot/trace.c with N64C_CALLS=DIR; oracle: the n64host lockstep hook). A `call`
line names the function `fn`, its `caller` (the logical parent frame's function), the call's ordinal `o` among that
caller's calls of the function, the `frame` (poll) it ran in, its argument words `a` (oracle: a0-a3 with `f12`/`f14`;
port: the first 8 words of the cdecl stack area) and, for a function returning, a `ret` line `r` ([v0, v1] on the
oracle, [eax, edx] on the port; `f0` the oracle's float result, `st0` the port's) keyed by `i`. A `ret` line carries
`m`: for each pointer argument (key: the oracle's register number, the port's cdecl word number) the 16 words of RDRAM
it names, read at the return as the original's code sees them (an address inside a window reads `pppppppp`); the
windows of the same pointer parameter are part of the result. Only calls at the first N ordinals of a (caller,
function) pair and at every power-of-two ordinal are recorded, the same on both sides.

Calls are aligned by (function, caller, ordinal): the phase of the two sides' polls does not matter. A function's
verdict follows its first diverging call (oracle chronology): `differs-args` when the same call got other arguments (or
another caller), `differs-result` when equal arguments gave another return or write digest, `missing` when only the
original called it, `port-only` when only the port did, else `behaves`. The cause tag says where to fix: `input`
(the arguments already differ: fix upstream) or `own` (same arguments, different output: fix the function)."""

from __future__ import annotations

import json
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from abu_decomp.behave.prototype import Prototype, argument_slots

MASKS = {"int8": 0xFF, "uint8": 0xFF, "int16": 0xFFFF, "uint16": 0xFFFF}
POINTERS = ("pointer", "funcptr")
VERDICTS = ("behaves", "differs-args", "differs-result", "missing", "port-only")
Key = tuple[str, str, int]  # function, caller, ordinal
Same = Callable[[str, int, int], bool]  # kind, oracle value, port value


@dataclass
class Call:
    fn: str
    caller: str
    ordinal: int
    frame: int
    seq: int
    words: list[int]
    f12: int = 0
    f14: int = 0
    ret: dict[str, Any] | None = None
    writes: dict[str, str] | None = None  # pointer-argument windows read at the return (`m`)


def read_calls(path: Path, side: str, canon: Callable[[str], str] = lambda n: n) -> tuple[dict[Key, Call], int]:
    """Every recorded call of one side keyed by (function, caller, ordinal), and the meta line's `first` bound. A call
    line without caller and ordinal is refused: that capture predates per-call alignment."""
    calls: dict[Key, Call] = {}
    by_id: dict[tuple[Any, ...], Call] = {}
    first = 0
    for number, text in enumerate(path.read_text().splitlines(), 1):
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            continue  # the last line of a run killed mid-write
        kind = row.get("k")
        if kind == "meta":
            first = int(row.get("first", 0))
        elif kind == "call":
            if "caller" not in row or "o" not in row or "fn" not in row:
                raise ValueError(f"{path}:{number}: {side} call line has no fn/caller/o (capture predates per-call)")
            call = Call(
                canon(row["fn"]),
                canon(row["caller"]),
                int(row["o"]),
                int(row.get("frame", 0)),
                number,
                [int(w, 16) for w in row["a"]],
                int(row.get("f12", "0"), 16),
                int(row.get("f14", "0"), 16),
                None,
                None,
            )
            calls.setdefault((call.fn, call.caller, call.ordinal), call)
            by_id[("fn", row["fn"], row["i"])] = call
            if "order" in row:
                by_id[("order", row["order"], row["i"])] = call
        elif kind == "ret":
            key = ("fn", row["fn"], row["i"]) if "fn" in row else ("order", row.get("order"), row["i"])
            call = by_id.get(key)
            if call is not None and call.ret is None:
                call.ret = row
                call.writes = row.get("m")
    return calls, first


def _ulps(a: int, b: int, width: int) -> int:
    def ordered(bits: int) -> int:
        sign = 1 << (width - 1)
        return sign - (bits & (sign - 1)) if bits & sign else sign + bits

    return abs(ordered(a) - ordered(b))


def default_same(kind: str, oracle: int, port: int) -> bool:
    """Equality of one value across the builds: pointers compare as null/non-null (an address differs between the
    original and the port; what it points to is the memory-write digest's business), floats within 4 ulp (MIPS
    single against x87), the rest exactly."""
    if kind in POINTERS:
        return (oracle == 0) == (port == 0)
    if kind == "float":
        return _ulps(oracle, port, 32) <= 4
    if kind == "double":
        return _ulps(oracle, port, 64) <= 4
    return oracle == port


def _hex(value: int) -> str:
    return f"0x{value:x}"


def _oracle_value(call: Call, where: tuple[str, ...], kind: str) -> int | None:
    if where[0] in ("f12", "f14"):
        value = call.f12 if where[0] == "f12" else call.f14
        return value & 0xFFFFFFFF if kind == "float" else value
    if where[0].startswith("stack:"):
        return None  # the oracle records a0-a3 only
    words = [call.words[int(w[1:])] for w in where]
    value = 0
    for word in words:
        value = value << 32 | word
    return value & MASKS.get(kind, value)


def diff_args(proto: Prototype, oracle: Call, port: Call, same: Same = default_same) -> list[dict[str, str]]:
    """Every argument the prototype reads that differs, in parameter order: {field, kind, oracle, port}."""
    out: list[dict[str, str]] = []
    word = 0
    for param, slot in ((s.param, s) for s in argument_slots(proto)):
        at = word
        word += param.words
        if at + param.words > len(port.words):
            break
        ov = _oracle_value(oracle, slot.where, param.kind)
        if ov is None:
            continue
        pv = port.words[at] if param.words == 1 else port.words[at] | port.words[at + 1] << 32
        pv &= MASKS.get(param.kind, pv)
        if not same(param.kind, ov, pv):
            out.append({"field": f"{param.name}", "kind": param.kind, "oracle": _hex(ov), "port": _hex(pv)})
    return out


def _port_result(kind: str, ret: dict[str, Any]) -> int | None:
    eax, edx = (int(x, 16) for x in ret["r"])
    if kind in ("float", "double"):
        if ret.get("st0") is None:
            return None
        bits = int(ret["st0"], 16)
        if kind == "double":
            return bits
        return struct.unpack("<I", struct.pack("<f", struct.unpack("<d", bits.to_bytes(8, "little"))[0]))[0]
    if kind in ("int64", "uint64"):
        return edx << 32 | eax
    return eax & MASKS.get(kind, 0xFFFFFFFF)


def _oracle_result(kind: str, ret: dict[str, Any]) -> int:
    v0, v1 = (int(x, 16) for x in ret["r"])
    if kind in ("float", "double"):
        f0 = int(ret["f0"], 16)
        return f0 & 0xFFFFFFFF if kind == "float" else f0
    if kind in ("int64", "uint64"):
        return v0 << 32 | v1
    return v0 & MASKS.get(kind, 0xFFFFFFFF)


def _window_keys(proto: Prototype) -> list[tuple[str, str, str]]:
    """(parameter, oracle key, port key) of every pointer parameter whose window the oracle records (a0-a3)."""
    out: list[tuple[str, str, str]] = []
    word = 0
    for slot in argument_slots(proto):
        at = word
        word += slot.param.words
        where = slot.where[0]
        if slot.param.kind == "pointer" and where[0] == "a" and where[1:].isdigit():
            out.append((slot.param.name, where[1:], str(at)))
    return out


def _window_text(call: Call, proto: Prototype, side: int) -> str | None:
    if not call.writes:
        return None
    parts = [f"{k[0]}: {call.writes[k[1 + side]]}" for k in _window_keys(proto) if k[1 + side] in call.writes]
    return "; ".join(parts) or None


def diff_windows(proto: Prototype, oracle: Call, port: Call) -> list[dict[str, str]]:
    """The first differing word of each pointer parameter's window that both sides recorded: {field `*name[word]`,
    kind `memory`, oracle, port}. A window one side did not record (a null or foreign pointer) is not compared."""
    out: list[dict[str, str]] = []
    for name, ok, pk in _window_keys(proto):
        ow, pw = (oracle.writes or {}).get(ok), (port.writes or {}).get(pk)
        if ow is None or pw is None or ow == pw:
            continue
        for at in range(0, min(len(ow), len(pw)), 8):
            if ow[at : at + 8] != pw[at : at + 8]:
                out.append({"field": f"*{name}[{at // 8}]", "kind": "memory", "oracle": ow[at : at + 8], "port": pw[at : at + 8]})
                break
    return out


def diff_result(proto: Prototype, oracle: Call, port: Call, same: Same = default_same) -> list[dict[str, str]]:
    """The return value (when the prototype says it is meaningful) and the memory-write digest, when both calls
    returned and recorded one: {field, kind, oracle, port}."""
    out: list[dict[str, str]] = []
    if oracle.ret is None or port.ret is None:
        return out
    if proto.ret not in ("void", "struct"):
        ov = _oracle_result(proto.ret, oracle.ret)
        pv = _port_result(proto.ret, port.ret)
        if pv is None:
            out.append({"field": "return", "kind": proto.ret, "oracle": _hex(ov), "port": "no st(0)"})
        elif not same(proto.ret, ov, pv):
            out.append({"field": "return", "kind": proto.ret, "oracle": _hex(ov), "port": _hex(pv)})
    out.extend(diff_windows(proto, oracle, port))
    return out


@dataclass
class Verdict:
    verdict: str
    compared: int = 0
    unmatched_oracle: int = 0
    unmatched_port: int = 0
    first: dict[str, Any] | None = None
    cause: str | None = None  # `input` (arguments already differ) or `own` (same arguments, other output)
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "compared": self.compared,
            "unmatched_oracle": self.unmatched_oracle,
            "unmatched_port": self.unmatched_port,
            "first": self.first,
            "cause": self.cause,
        }


def _call_row(proto: Prototype, oracle: Call, port: Call, diffs: list[dict[str, str]], what: str) -> dict[str, Any]:
    return {
        "what": what,
        "caller": oracle.caller,
        "ordinal": oracle.ordinal,
        "frame": {"oracle": oracle.frame, "port": port.frame},
        "diffs": diffs,
        "oracle": {
            "args": [_hex(w) for w in oracle.words[:4]],
            "ret": oracle.ret and oracle.ret["r"],
            "writes": _window_text(oracle, proto, 0),
        },
        "port": {"args": [_hex(w) for w in port.words], "ret": port.ret and port.ret["r"], "writes": _window_text(port, proto, 1)},
    }


def align(
    oracle: dict[Key, Call],
    port: dict[Key, Call],
    protos: Callable[[str], Prototype],
    same: Same = default_same,
) -> dict[str, Verdict]:
    """Per function either side called: its Verdict. `protos(name)` gives the prototype that decodes the call."""
    names: dict[str, list[Key]] = {}
    for side in (oracle, port):
        for key in side:
            names.setdefault(key[0], []).append(key)
    out: dict[str, Verdict] = {}
    for name in sorted(names):
        keys = set(names[name])
        o_keys = sorted((k for k in keys if k in oracle), key=lambda k: oracle[k].seq)
        p_keys = [k for k in keys if k in port]
        if not p_keys:
            out[name] = Verdict("missing", unmatched_oracle=len(o_keys))
            continue
        if not o_keys:
            out[name] = Verdict("port-only", unmatched_port=len(p_keys))
            continue
        proto = protos(name)
        both = [k for k in o_keys if k in port]
        verdict = Verdict(
            "behaves",
            len(both),
            unmatched_oracle=len(o_keys) - len(both),
            unmatched_port=len(p_keys) - len(both),
        )
        if not both:
            callers = {"oracle": sorted({k[1] for k in o_keys}), "port": sorted({k[1] for k in p_keys})}
            verdict.verdict, verdict.cause = "differs-args", "input"
            verdict.first = {
                "what": "caller",
                "diffs": [
                    {
                        "field": "caller",
                        "kind": "symbol",
                        **{"oracle": ",".join(callers["oracle"]), "port": ",".join(callers["port"])},
                    }
                ],
            }
        for key in both:
            args = diff_args(proto, oracle[key], port[key], same)
            if args:
                verdict.verdict, verdict.cause = "differs-args", "input"
                verdict.first = _call_row(proto, oracle[key], port[key], args, "args")
                break
            result = diff_result(proto, oracle[key], port[key], same)
            if result:
                verdict.verdict, verdict.cause = "differs-result", "own"
                verdict.first = _call_row(proto, oracle[key], port[key], result, "result")
                break
        out[name] = verdict
    return out


def summarise(verdicts: dict[str, Verdict]) -> dict[str, Any]:
    """Functions per verdict bucket and how many differ by their own fault (same input, other output)."""
    counts = dict.fromkeys(VERDICTS, 0)
    for v in verdicts.values():
        counts[v.verdict] += 1
    return {"buckets": counts, "own_fault": sum(1 for v in verdicts.values() if v.cause == "own")}
