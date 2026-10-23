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


def capture(run: Path, port: Path | str) -> dict[str, Any] | None:
    """The run's fault record with its addresses named, and `failure-fault.txt` written; None when the run left none."""
    try:
        record: dict[str, Any] | None = {**json.loads((run / FILE).read_text()), "source": "shim"}
    except (OSError, ValueError):
        threads = run / "failure-threads.txt"
        record = from_threads(threads.read_text(errors="replace")) if threads.is_file() else None
    if not record or "eip" not in record:
        return None
    named = []
    for address in (record["eip"], *record.get("frames", [])):
        found = tas_symbolize.resolve(port, int(address, 16))
        named.append(f"{found[0]}+{found[1]:#x}" if found else address)
    record["at"], record["callers"] = named[0], named[1:]
    (run / STACK).write_text("".join(f"#{i}  {name}\n" for i, name in enumerate(named)))
    return record
