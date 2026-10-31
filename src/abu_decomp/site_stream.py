"""Site region `stream`: the lockstep stream of one recorded run (polls, inputs, frames per side).

Stub until card RUNNER-STREAM builds it (lanes/RUNNER-REDO/SPLIT.tsv).
"""

from __future__ import annotations

from typing import Any

from abu_decomp import site_core


def answer(server: Any, method: str, rest: str, query: str, body: bytes) -> tuple[int, Any]:
    return site_core.stub("RUNNER-STREAM", "stream")
