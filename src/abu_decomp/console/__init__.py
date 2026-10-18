"""Console boundary: the one Textual console and its panes.

Moves here from cake_taste.logs (console, dashboard_model, dashboard_sources, host_output, gitrefs) at the rig
cutover. Panes read published files and CLI JSON only (progress.json, `unbake report --next`, event feeds,
run status, lane PULSE.json and transcripts), never another tool's Python internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Row:
    key: str
    label: str
    details: str
    state: str


class Pane(Protocol):
    name: str

    def watched(self) -> tuple[Path, ...]:
        """The files whose changes wake this pane; no polling."""
        ...

    def rows(self) -> list[Row]:
        """The pane's rows from its files as they are now."""
        ...
