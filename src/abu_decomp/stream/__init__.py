"""Stream boundary: the compositor, the tile grid, the transport and capture.

Moves here 1:1 from cake_taste.stream (and panes/layout.py) at the rig cutover. A tile is either a video role
(a process window) or a console; the grid, roles and ports come only from the seat profile `stream` section.
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
