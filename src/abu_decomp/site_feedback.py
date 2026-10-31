"""Site region `feedback`: human feedback records (file at a poll, list, show).

Stub until card RUNNER-PLAY builds it (lanes/RUNNER-REDO/SPLIT.tsv).
"""

from __future__ import annotations

from typing import Any

from abu_decomp import site_core


def answer(server: Any, method: str, rest: str, query: str, body: bytes) -> tuple[int, Any]:
    return site_core.stub("RUNNER-PLAY", "feedback")
