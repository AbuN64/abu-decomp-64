"""Stream boundary: the compositor, the tile grid, the transport and capture.

A tile is either a video role (a process window) or a console; the grid and roles come from the seat profile
`tiles` table, the compositor, ports and services from its `stream` section.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TileKind = Literal["video", "console"]


@dataclass(frozen=True)
class Tile:
    name: str
    kind: TileKind
    # video: the role's command line; console: the console panes it shows.
    command: tuple[str, ...] = ()
    panes: tuple[str, ...] = ()
