"""A crash names its real signal frame, not the thread that reported it.
A host fault is taken on one thread (a texture worker, the AI loop) and reported from the frame loop's thread, so the
gdb stack of the stop shows only the supervisor and ranks whatever waits. The shim writes the faulting frame itself
into `fault-frame.json` of the run (N64C_FAULT_FRAME): thread id, EIP, general registers, si_addr and access type,
the stack and argument words and the return addresses up the ebp chain. A port without that record falls back to
`failure-threads.txt`: the thread with `<signal handler called>` and the frames under it. Either way the run gets
`fault` (the record plus each address as `function+offset` in the port binary) and `failure-fault.txt`, a stack file
that lists the faulting frame first so every reader of the stacks (arity, triage, ranking) starts at the fault.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from abu_decomp import tas_symbolize

FILE, STACK = "fault-frame.json", "failure-fault.txt"
_FRAME = re.compile(r"^#\d+\s+(?:0x([0-9a-fA-F]+) in )?")
_LWP = re.compile(r"LWP (\d+)")


def from_threads(text: str) -> dict[str, Any] | None:
    """The first thread of a `thread apply all bt` listing that holds a signal frame: its id and the pcs under it."""
    for block in re.split(r"(?m)^(?=Thread \d+ )", text):
        lines = block.splitlines()
        at = next((i for i, line in enumerate(lines) if "<signal handler called>" in line), None)
        if at is None:
            continue
        pcs = [int(m[1], 16) for line in lines[at + 1 :] if (m := _FRAME.match(line)) and m[1]]
        if pcs:
            lwp = _LWP.search(lines[0])
            return {
                "source": "gdb",
                "tid": int(lwp[1]) if lwp else 0,
                "eip": f"{pcs[0]:#010x}",
                "frames": [f"{p:#010x}" for p in pcs[1:]],
            }
    return None


def from_stack(text: str, pc: int | None = None) -> dict[str, Any] | None:
    """The stopped PC owns the fault; an addressless frame zero never promotes its caller."""
    frames = [(line, m[1]) for line in text.splitlines() if (m := _FRAME.match(line))]
    top = next((address for line, address in frames if re.match(r"^#0\s", line)), None)
    if pc is None:
        if top is None:
            return None
        pc = int(top, 16)
    callers = ["0x" + address for line, address in frames if address and not re.match(r"^#0\s", line)]
    return {"source": "gdb", "eip": f"{pc:#010x}", "frames": callers}


def fp_exception(registers: dict[str, int], instruction: str) -> dict[str, Any] | None:
    """Reconstruct FCSR enable/cause bits from native trap state, using the shim's mapping.

    The native shim has no guest FCSR store. Denormal-operand is host-only, not a guest cause.
    """
    x87 = bool(re.search(r"\b(?:fdiv\w*|fmul\w*|fadd\w*|fsub\w*|fsqrt|fist\w*)\b", instruction))
    if x87:
        if not {"fctrl", "fstat"} <= registers.keys():
            return None
        masks, status, origin = registers["fctrl"], registers["fstat"], "x87 control/status"
    else:
        if "mxcsr" not in registers:
            return None
        status, origin = registers["mxcsr"], "MXCSR"
        masks = status >> 7
    enables = causes = 0
    active = []
    for guest, (native, name) in enumerate(
        ((5, "inexact"), (4, "underflow"), (3, "overflow"), (2, "divide-by-zero"), (0, "invalid operation"))
    ):
        enabled, raised = not masks & (1 << native), bool(status & (1 << native))
        enables |= int(enabled) << (7 + guest)
        causes |= int(raised) << (12 + guest)
        if enabled and raised:
            active.append({"exception": name, "enable_bit": 7 + guest, "cause_bit": 12 + guest})
    return {
        "source": origin,
        "reconstructed": True,
        "enable_bits": enables,
        "cause_bits": causes,
        "exceptions": active,
        "message": "; ".join(
            f"guest FP exception: {e['exception']}, enabled by FCSR bit {e['enable_bit']} (cause bit {e['cause_bit']})"
            for e in active
        ),
    }


def capture(run: Path, port: Path | str) -> dict[str, Any] | None:
    """The run's fault record with its addresses named, and `failure-fault.txt` written; None when the run left none."""
    context = run / "signal-context.txt"
    text = context.read_text(errors="replace") if context.is_file() else ""
    registers = {
        m[1]: int(m[2], 16) for m in re.finditer(r"(?m)^(eip|ebp|esp|mxcsr|fctrl|fstat)\s+0x([0-9a-fA-F]+)", text)
    }
    try:
        record: dict[str, Any] | None = {**json.loads((run / FILE).read_text()), "source": "shim"}
    except (OSError, ValueError):
        stack = run / "signal-stack.txt"
        record = from_stack(stack.read_text(errors="replace") if stack.is_file() else "", registers.get("eip"))
        for name in ("failure-threads.txt", "signal-threads.txt"):
            threads = run / name
            if record is None and threads.is_file():
                record = from_threads(threads.read_text(errors="replace"))
    if not record or "eip" not in record:
        return None
    named = []
    for address in (record["eip"], *record.get("frames", [])):
        found = tas_symbolize.resolve(port, int(address, 16))
        named.append(f"{found[0]}+{found[1]:#x}" if found else address)
    record["at"], record["callers"], record["port"] = named[0], named[1:], str(port)
    pc = int(record["eip"], 16)
    record["instruction"] = tas_symbolize.instruction_of(port, pc)
    record["source_line"] = tas_symbolize.line_of(port, pc)
    if record["source"] == "gdb" and context.is_file():
        record["registers"] = registers
        if fp := fp_exception(registers, record["instruction"] or ""):
            record["fcsr"] = fp
        # Require an i386 frame-pointer prologue before interpreting ebp+8 as argument slots.
        found = tas_symbolize.resolve(port, pc)
        if "pointer_bytes=4" in text and found and tas_symbolize.frame_pointer(port, pc - found[1]):
            words = [
                int(w, 16)
                for line in text.splitlines()
                if re.match(r"^0x[0-9a-fA-F]+(?: <[^>]+>)?:", line)
                for w in re.findall(r"0x[0-9a-fA-F]+", line.partition(":")[2])
            ]
            if words:
                record["arguments"] = {
                    "abi": "i386-cdecl",
                    "function": found[0],
                    "words": words,
                    "at": "fault",
                    "note": "stack argument slots at the stop; may have been modified",
                }
    if record["source"] == "shim" and record.get("args"):
        found = tas_symbolize.resolve(port, pc)
        if found and tas_symbolize.frame_pointer(port, pc - found[1]):
            record["arguments"] = {
                "abi": "i386-cdecl",
                "function": found[0],
                "words": [int(w, 16) for w in record["args"]],
                "at": "fault",
                "note": "stack argument slots at the stop; may have been modified",
            }
    (run / STACK).write_text("".join(f"#{i}  {name}\n" for i, name in enumerate(named)))
    return record
