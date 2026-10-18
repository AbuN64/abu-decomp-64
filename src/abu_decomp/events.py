"""Event boundary. Cake Taste owns the one event writer; decomp imports it and never writes the format itself.

decomp produces the lifecycle kinds; Cake Taste instruments produce PROBE and HANDOFF.
"""

from __future__ import annotations

from types import ModuleType

from abu_decomp import DecompError

LIFECYCLE_KINDS = ("RUN-FAIL", "LANE", "LAND", "MATCH", "DECISION")


def writer() -> ModuleType:
    """The installed Cake Taste writer, required at event schema 2."""
    try:
        from cake_taste.logs import events
    except ImportError as exc:
        raise DecompError("events.writer", "Cake Taste is not installed; run decomp dependencies") from exc
    if getattr(events, "SCHEMA", 1) != 2 or not set(LIFECYCLE_KINDS) <= set(getattr(events, "KINDS", ())):
        raise DecompError("events.writer", "the installed Cake Taste writes event schema 1; pin a schema 2 commit")
    return events
