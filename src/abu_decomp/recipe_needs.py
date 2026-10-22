"""The type item a skipped function waits on, read from Recipe Maker's own state in the project.

`recipe skip F --reason needs-header-change|type-unresolved` writes `.unbake/recipe/needs.json` (`{F: {item, next}}`)
when a `void *` parameter of F needs a record: matching F cannot start before that type lands. Every ranked list
that would send an agent to `recipe work F` sends it to the type item's `next` instead; the landing deletes the entry.
"""

from __future__ import annotations

import json
from pathlib import Path


def read(project: Path | None) -> dict[str, dict[str, str]]:
    try:
        found = json.loads((project / ".unbake/recipe/needs.json").read_text()) if project else {}
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def route(project: Path | None, function: str, default: str) -> str:
    """`default` unless `function` waits for a type item, then that item's command."""
    row = read(project).get(function)
    return str(row["next"]) if isinstance(row, dict) and row.get("next") else default
