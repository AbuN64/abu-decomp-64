"""One ranked list of open work, whose first row is the one next action.

`tas play --all` and `decomp owes` end with it, so an agent cannot read a build header as background and pick a
side item. Rule order is configuration (`tas.rank.order`, default build, stale, blocks, owes):
  build   one row per unit that failed to compile: its file:line, first error, the layer that owns the fix and a
          concrete `next`; `tas.rank.layers` rules `{match: regex on the error, layer, next}` name the owning layer
          (compile flags/config vs game source); the first matching rule wins, no match means game source;
  stale   the port binary is older than its sources: the rebuild command;
  blocks  each stop or crash subject, ranked by how many movies it blocks (then by polls its movies miss);
  owes    functions that owe a match, ranked by how many play runs reached them (module `owes`).
The last line is always `next: <command>`: row 1's.
"""

from __future__ import annotations

import re
from typing import Any

ORDER = ("build", "stale", "blocks", "owes")
RANK_KEYS = frozenset({"order", "layers", "owes_rows"})
_SITE = re.compile(r"^(\S+?:\d+)\s+(.*)$")


def settings(table: Any) -> dict[str, Any]:
    table = table or {}
    order = tuple(str(x) for x in table.get("order", ORDER))
    layers = [
        {"match": re.compile(str(r["match"])), "layer": str(r["layer"]), "next": str(r.get("next", ""))}
        for r in table.get("layers", [])
    ]
    return {"order": order, "layers": layers, "owes_rows": int(table.get("owes_rows", 3))}


def build_rows(health: dict[str, Any], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for unit in health.get("failed", []) if health else []:
        match = _SITE.match(unit["error"])
        where, text = (match[1], match[2]) if match else (unit["unit"], unit["error"])
        text = text.partition(";")[0][:140]  # the first sentence: the compiler's notes after it are in the build header
        layer, nxt = "game source", f"fix {where}: {text}"
        for rule in cfg["layers"]:
            if rule["match"].search(unit["error"]):
                layer = rule["layer"]
                nxt = rule["next"].replace("{where}", where).replace("{unit}", unit["unit"]) if rule["next"] else nxt
                break
        rows.append({"kind": "build failure", "where": where, "error": text, "layer": layer, "next": nxt})
    return rows


def stale_rows(health: dict[str, Any]) -> list[dict[str, Any]]:
    if not health or not health.get("stale"):
        return []
    return [
        {
            "kind": "stale binary",
            "where": health.get("stale_by", ""),
            "error": "binary older than source",
            "layer": "build",
            "next": health.get("rebuild") or "rebuild the port",
        }
    ]


def block_rows(movies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in movies:
        if row.get("outcome") != "ok":
            groups.setdefault(str(row.get("subject") or row.get("outcome")), []).append(row)
    out = []
    for subject, group in groups.items():
        first = group[0]
        out.append(
            {
                "kind": first.get("kind") or first["outcome"],
                "where": subject,
                "blocks": sorted(r["movie"] for r in group),
                "layer": "game source",
                "error": f"{first['outcome']} {subject}",
                "next": first.get("next", ""),
                "owner": first.get("owner", ""),
            }
        )
    out.sort(key=lambda r: (-len(r["blocks"]), r["where"]))
    return out


def owes_rows(owes: dict[str, Any] | None, taken: set[str], limit: int) -> list[dict[str, Any]]:
    out = []
    for row in (owes or {}).get("top", []):
        if row["function"] in taken:
            continue
        out.append(
            {
                "kind": "owes a match",
                "where": f"{row['function']} at {row['file']}",
                "error": f"{row['level']}, reached by {row['reached']} runs",
                "layer": "game source",
                "next": row["next"],
            }
        )
        if len(out) >= limit:
            break
    return out


def rank(
    cfg: dict[str, Any], health: dict[str, Any], movies: list[dict[str, Any]], owes: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Open work in the configured rule order; every row carries `rank`, the first is THE next action."""
    blocks = block_rows(movies)
    taken = {r["where"] for r in blocks} | {w for r in blocks for w in r["next"].split() if w.startswith("func_")}
    parts = {
        "build": lambda: build_rows(health, cfg),
        "stale": lambda: stale_rows(health),
        "blocks": lambda: blocks,
        "owes": lambda: owes_rows(owes, taken, cfg["owes_rows"]),
    }
    rows = [row for name in cfg["order"] if name in parts for row in parts[name]()]
    for number, row in enumerate(rows, 1):
        row["rank"] = number
    return rows


def render(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "ranked work: none\nnext: nothing open"
    lines = ["ranked work (row 1 is the next action):"]
    for row in rows:
        blocked = f"; blocks {len(row['blocks'])} movies: {', '.join(row['blocks'])}" if row.get("blocks") else ""
        owner = f" ({row['owner']})" if row.get("owner") else ""
        lines.append(f"  {row['rank']}. [{row['kind']}] {row['where']}{owner}: {row['error']}{blocked}")
        lines.append(f"     owner layer: {row['layer']}; next: {row['next']}")
    lines.append(f"next: {rows[0]['next']}")
    return "\n".join(lines)
