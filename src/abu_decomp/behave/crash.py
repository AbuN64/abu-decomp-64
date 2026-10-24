"""Cases from a port's fault argument slots when the original has no recorded reach.

The native i386 ABI is translated to o32 by the known prototype. Host pointers are never passed to
an oracle as guest addresses: unresolved pointers keep the generated case's region, explicitly reported.
This is argument evidence at the fault, not an oracle entry snapshot or proof of original reach.
"""

from __future__ import annotations

import copy
import struct
from typing import Any

from abu_decomp import DecompError, tas, tas_fault
from abu_decomp.behave import inputs
from abu_decomp.behave.prototype import Prototype, argument_slots


def arguments(profile: Any, function: str) -> tuple[dict[str, Any] | None, str]:
    try:
        cfg = tas.settings(profile)
    except DecompError:
        return None, "no TAS runs configured for crash arguments"
    if "runs" not in cfg:
        return None, "no TAS runs configured for crash arguments"
    for run, row in reversed(tas.run_rows(cfg)):
        if row.get("outcome") != "crash":
            continue
        fault = row.get("fault")
        if not isinstance(fault, dict):
            # Old signal runs have no fault record. Reuse their stack, but never symbolize against
            # a different live executable: the old run must name its own port.
            stack = run / "signal-stack.txt"
            if not stack.is_file() or function not in stack.read_text(errors="replace"):
                continue
            port = row.get("port")
            fault = tas_fault.capture(run, port) if port else None
        args = fault.get("arguments") if isinstance(fault, dict) else None
        if isinstance(args, dict) and args.get("function") == function and args.get("abi") == "i386-cdecl":
            return {**args, "run": str(run), "movie": row.get("movie"), "poll": row.get("polls_reached")}, (
                f"crash argument slots from movie {row.get('movie')} at poll {row.get('polls_reached')} ({run.name})"
            )
    return None, "no captured crash argument slots for this function; replay its crash movie with the current tool"


def case(
    proto: Prototype, baseline: dict[str, Any], crash: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    provenance = {k: crash.get(k) for k in ("run", "movie", "poll", "function", "at", "note")}
    if proto.blind:
        return None, {**provenance, "reason": "crash argument ABI needs a complete, nonvariadic prototype"}
    words = crash["words"]
    if sum(p.words for p in proto.params) > len(words):
        return None, {**provenance, "reason": "crash argument record is shorter than the prototype"}
    entry = copy.deepcopy(baseline)
    entry["name"] = "crash-arguments"
    entry["labels"] = {"crash": str(crash.get("movie")), "seed": "crash argument slots"}
    used, omitted = [], []
    offset = 0
    for slot in argument_slots(proto):
        param = slot.param
        raw = words[offset : offset + param.words]
        offset += param.words
        if param.kind in ("pointer", "funcptr") and raw[0] != 0:
            omitted.append(param.name)
            continue
        if param.kind == "float":
            value = struct.unpack("<f", struct.pack("<I", raw[0]))[0]
        elif param.kind == "double":
            value = struct.unpack("<d", struct.pack("<II", *raw))[0]
        else:
            value = sum(w << (32 * i) for i, w in enumerate(raw))
        regs, stack = inputs._value(slot, value)
        entry["registers"].update(regs)
        for at, word in stack.items():
            index = (at - 16) // 4
            while len(entry["stack"]) <= index:
                entry["stack"].append(0)
            entry["stack"][index] = word
        entry["labels"][param.name] = repr(value)
        used.append(param.name)
    provenance.update(arguments=used, synthetic_pointers=omitted, memory="synthetic; no oracle entry captured")
    if not used:
        return None, {**provenance, "reason": "no portable scalar or null argument in the crash record"}
    return entry, provenance
