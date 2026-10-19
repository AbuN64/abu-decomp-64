"""A fully transparent XCursor theme, so the stream shows the viewer's own cursor and no second one.

The captured compositor output includes the compositor's cursor, and the browser draws its local one over the video;
with `stream.up.cursor = "hidden"` the compositor and every client it runs resolve every cursor name to a 1x1
transparent image. The theme is written under the run directory on each start.
"""

from __future__ import annotations

import struct
from pathlib import Path

THEME = "blank"
SIZE = 24
NAMES = [
    "default",
    "left_ptr",
    "arrow",
    "top_left_arrow",
    "right_ptr",
    "text",
    "xterm",
    "ibeam",
    "pointer",
    "hand",
    "hand1",
    "hand2",
    "pointing_hand",
    "grab",
    "grabbing",
    "openhand",
    "closedhand",
    "move",
    "fleur",
    "all-scroll",
    "crosshair",
    "cross",
    "wait",
    "watch",
    "progress",
    "left_ptr_watch",
    "help",
    "question_arrow",
    "context-menu",
    "not-allowed",
    "crossed_circle",
    "no-drop",
    "copy",
    "alias",
    "cell",
    "vertical-text",
    "n-resize",
    "s-resize",
    "e-resize",
    "w-resize",
    "ne-resize",
    "nw-resize",
    "se-resize",
    "sw-resize",
    "ns-resize",
    "ew-resize",
    "nesw-resize",
    "nwse-resize",
    "row-resize",
    "col-resize",
    "sb_h_double_arrow",
    "sb_v_double_arrow",
    "size_hor",
    "size_ver",
    "size_fdiag",
    "size_bdiag",
    "top_side",
    "bottom_side",
    "left_side",
    "right_side",
    "top_left_corner",
    "top_right_corner",
    "bottom_left_corner",
    "bottom_right_corner",
    "zoom-in",
    "zoom-out",
]


def image() -> bytes:
    """One Xcursor file: header, one table entry, one 1x1 fully transparent ARGB image of the nominal size."""
    chunk_type = 0xFFFD0002
    body = struct.pack("<IIIIIIIII", 36, chunk_type, SIZE, 1, 1, 1, 0, 0, 0) + struct.pack("<I", 0)
    header = struct.pack("<4sIII", b"Xcur", 16, 0x10000, 1)
    entry = struct.pack("<III", chunk_type, SIZE, 16 + 12)
    return header + entry + body


def write_theme(root: Path) -> Path:
    """Write the theme at root/blank and return root, the XCURSOR_PATH entry."""
    cursors = root / THEME / "cursors"
    cursors.mkdir(parents=True, exist_ok=True)
    (root / THEME / "index.theme").write_text("[Icon Theme]\nName=blank\n")
    data = image()
    for name in NAMES:
        (cursors / name).write_bytes(data)
    return root
