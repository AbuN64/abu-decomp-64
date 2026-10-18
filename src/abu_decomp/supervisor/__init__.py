"""Supervisor boundary: run a target under gdb with retry and capture, the boot gate, RUN-FAIL events.

Moves here headless from cake_taste.panes (supervisor, gdb, artifact) and logs/child.py at the build switch.
It writes supervisor_status for the console and RUN-FAIL through the Cake Taste writer; it draws nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Outcome = Literal["running", "clean_exit", "manual_restart", "debugger_exit", "crashed", "gate_failed"]


@dataclass(frozen=True)
class Status:
    role: str
    outcome: Outcome
    reason: str | None
    receipt: str | None
