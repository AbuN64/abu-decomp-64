"""Start points: named places in the game a movie can begin, so an early crash no longer hides everything after it.

A start point is a movie kept under `<movies>/points/NAME.tas` that holds only the game's own way there: the
controller polls that navigate its menus (and enter its own cheat codes), plus the Controller Pak card it starts
with (header `cpak`, kept beside it as `<movies>/<sha256>.mpk` like any movie's). Nothing is copied out of the
original or the port: both sides replay the same polls from power-on, so the original and the port reach the same
place by the same inputs and the behaviour diff still works.
Header lines of a point: `area` (the game area it reaches, a name from `points/areas.txt`), `note`, `pak`, `cpak`,
and `start` (another point it continues from: a chain of short hops).
A movie with the header `start NAME` is the polls of that point followed by its own: replay, take, retry and the
oracle all see the one joined poll list; a take recorded from a point is saved without the prefix. The movie's own
`pak`/`cpak` lines win; when it has none it ran with the point's.
`points/areas.txt` is the catalogue of game areas, one `kind | name` line each; coverage says which have a point.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from abu_decomp import DecompError

DIR, AREAS = "points", "areas.txt"
MAX_CHAIN = 4
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
Poll = tuple[int, ...]


def folder(cfg: dict[str, Any]) -> Path:
    return cfg["movies"] / DIR


def path(cfg: dict[str, Any], name: str) -> Path:
    if not _NAME.fullmatch(name):
        raise DecompError("tas.point", f"start point names are lower-case words joined by '-', not {name!r}")
    return folder(cfg) / f"{name}.tas"


def exists(cfg: dict[str, Any], name: str) -> bool:
    return bool(_NAME.fullmatch(name)) and path(cfg, name).is_file()


def load(cfg: dict[str, Any], name: str, _seen: tuple[str, ...] = ()) -> dict[str, Any]:
    """A point with its whole chain resolved: `header`, `polls` (the joined polls from power-on), `own` (its own
    count), `chain` (names, first hop first)."""
    from abu_decomp import tas

    if name in _seen or len(_seen) >= MAX_CHAIN:
        raise DecompError("tas.point", f"start point chain too long or circular at {name!r}: {' > '.join(_seen)}")
    file = path(cfg, name)
    if not file.is_file():
        raise DecompError("tas.point", f"no start point {name!r} (see: decomp tas point list)")
    header, own = tas.decode(file.read_text())
    rom = tas._sha1(cfg["rom"])
    if header.get("rom-sha1") != rom:
        raise DecompError("tas.point", f"{file.name} was recorded on ROM {header.get('rom-sha1')}, not {rom}")
    before: list[Poll] = []
    chain = [name]
    if header.get("start"):
        parent = load(cfg, header["start"], (*_seen, name))
        before, chain = parent["polls"], [*parent["chain"], name]
        if before and own and len(before[0]) != len(own[0]):
            raise DecompError(
                "tas.point",
                f"{name!r} drives {len(own[0]) // 3} pad(s) after {header['start']!r} drove {len(before[0]) // 3}: "
                "name the same `pads` in both",
            )
    return {"name": name, "header": header, "polls": before + own, "own": len(own), "chain": chain}


def prefix_of(cfg: dict[str, Any], header: dict[str, str]) -> dict[str, Any] | None:
    """The point a movie header starts from (`start NAME`), None for a movie from power-on."""
    start = header.get("start")
    return load(cfg, start) if start else None


def inherit(cfg: dict[str, Any], header: dict[str, str], key: str, default: str) -> str:
    """A movie's own header value, else the one its start point ran with, else `default`."""
    if key in header:
        return header[key]
    point = prefix_of(cfg, header)
    return point["header"].get(key, default) if point else default


def joined(cfg: dict[str, Any], header: dict[str, str], own: list[Poll], start: str | None = None) -> list[Poll]:
    """The polls a replay feeds the game: the start point's, then the movie's own. `start` overrides the header's."""
    name = start if start is not None else header.get("start")
    before = load(cfg, name)["polls"] if name else []
    if before and own and len(before[0]) != len(own[0]):
        raise DecompError(
            "tas.point",
            f"the movie drives {len(own[0]) // 3} pad(s) after start point {name!r} drove {len(before[0]) // 3}: "
            "name the same `pads` in its header",
        )
    return before + own


def save_point(
    profile: Any,
    name: str,
    *,
    movie: str | None = None,
    run: str | None = None,
    at: int | None = None,
    area: str = "",
    note: str = "",
    replace: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Keep the first `at` polls of a movie (or of a replay run's recording) as start point `name`. A movie that
    starts from a point yields a point that continues from it."""
    from abu_decomp import tas

    cfg = tas.settings(profile)
    target = path(cfg, name)
    if target.exists() and not replace:
        raise DecompError("tas.point", f"{target.name} exists; pass --replace to overwrite it")
    if (movie is None) == (run is None):
        raise DecompError("tas.point", "give exactly one of --movie and --run")
    start, prefix_len = None, 0
    if movie is not None:
        file = tas._movie(cfg, movie)
        if not file.is_file():
            raise DecompError("tas.point", f"no movie {movie!r}")
        header, own = tas.decode(file.read_text())
        start = header.get("start")
        eff = joined(cfg, header, own)
        pak = inherit(cfg, header, "pak", "on")
        cpak = inherit(cfg, header, "cpak", "none")
    else:
        folder_ = tas.run_folder(cfg, run or "")
        row = tas.read_record(folder_) if folder_ else None
        if not folder_ or not row:
            raise DecompError("tas.point", f"no run {run!r}")
        eff = tas.read_raw(folder_ / "polls.raw")
        start = row.get("start") or None
        pak, cpak = str(row.get("pak") or "on"), str(row.get("cpak") or "none")
    if start:
        prefix_len = len(load(cfg, start)["polls"])
    if at is None:
        at = len(eff)
    if not prefix_len + 1 <= at <= len(eff):
        raise DecompError("tas.point", f"--at is a poll {prefix_len + 1}..{len(eff)}, not {at}")
    out: dict[str, str] = {
        "rom-sha1": tas._sha1(cfg["rom"]),
        "recorded": tas._stamp(now),
        "polls": str(at - prefix_len),
        "pak": pak,
        "cpak": cpak,
        "area": area,
    }
    if start:
        out["start"] = start
    if note:
        out["note"] = " ".join(note.split())
    if not area:
        del out["area"]
    folder(cfg).mkdir(parents=True, exist_ok=True)
    target.write_text(tas.encode(eff[prefix_len:at], out))
    return {"point": name, "path": str(target), "polls": at, "own": at - prefix_len, "area": area, "start": start}


def keep_card(profile: Any, run: str) -> dict[str, Any]:
    """Keep the Controller Pak card a replay run ended with, as <movies>/<sha256>.mpk: the save data the game wrote
    itself (a profile made through its own menus), ready to be the start card (header cpak SHA256) of a start point."""
    from abu_decomp import tas

    cfg = tas.settings(profile)
    folder_ = tas.run_folder(cfg, run)
    card = folder_ / tas.CARD if folder_ else None
    if card is None or not card.is_file():
        raise DecompError("tas.point", f"run {run!r} has no card (it ran without a Controller Pak)")
    image = card.read_bytes()
    if len(image) != tas.CARD_BYTES:
        raise DecompError("tas.point", f"{card} is not a 32 KiB Controller Pak image")
    sha = hashlib.sha256(image).hexdigest()
    cfg["movies"].mkdir(parents=True, exist_ok=True)
    (cfg["movies"] / f"{sha}.mpk").write_bytes(image)
    return {"cpak": sha, "bytes": len(image), "run": run}


def areas(cfg: dict[str, Any]) -> list[dict[str, str]]:
    file = folder(cfg) / AREAS
    rows = []
    for line in file.read_text().splitlines() if file.is_file() else []:
        if not line.strip() or line.startswith("#"):
            continue
        kind, _, name = (part.strip() for part in line.partition("|"))
        if name:
            rows.append({"kind": kind, "name": name})
    return rows


def names(cfg: dict[str, Any]) -> list[str]:
    return sorted(p.stem for p in folder(cfg).glob("*.tas")) if folder(cfg).is_dir() else []


def listing(profile: Any) -> dict[str, Any]:
    """Every point (polls from power-on, the area it reaches, its last replay's outcome on each side) and the area
    coverage: which catalogued areas have a start point and which still have none."""
    from abu_decomp import tas

    cfg = tas.settings(profile)
    last: dict[str, dict[str, Any]] = {}
    for _, row in tas.run_rows(cfg):
        if row.get("status") == "done" and row.get("outcome"):
            last[str(row.get("movie"))] = row
    rows = []
    for point in names(cfg):
        try:
            data = load(cfg, point)
        except DecompError as error:
            rows.append({"point": point, "error": str(error)})
            continue
        run = last.get(point) or {}
        oracle = run.get("oracle") or {}
        rows.append(
            {
                "point": point,
                "area": data["header"].get("area", ""),
                "polls": len(data["polls"]),
                "own": data["own"],
                "chain": data["chain"],
                "cpak": data["header"].get("cpak", "none"),
                "pads": tas.pad_count(data["polls"]),
                "note": data["header"].get("note", ""),
                "port": run.get("outcome"),
                "port_polls": run.get("polls_reached"),
                "oracle": oracle.get("outcome"),
                "max_diff": oracle.get("max_diff"),
            }
        )
    by_area: dict[str, list[str]] = {}
    for row in rows:
        if row.get("area"):
            by_area.setdefault(row["area"].lower(), []).append(row["point"])
    catalogue = areas(cfg)
    covered = [{**a, "points": by_area[a["name"].lower()]} for a in catalogue if a["name"].lower() in by_area]
    missing = [a for a in catalogue if a["name"].lower() not in by_area]
    known = {a["name"].lower() for a in catalogue}
    other = sorted(a for a in by_area if a not in known)
    return {
        "points": rows,
        "covered": covered,
        "missing": missing,
        "uncatalogued": other,
        "summary": f"{len(covered)}/{len(catalogue)} catalogued areas have a start point, {len(rows)} points",
    }


def render(result: dict[str, Any]) -> str:
    lines = [result["summary"]]
    for row in result["points"]:
        if "error" in row:
            lines.append(f"  {row['point']}: {row['error']}")
            continue
        lines.append(
            f"  {row['point']:<28} {row['area'] or '-':<26} {row['polls']:>6} polls"
            f"  port {row['port'] or '-'}  oracle {row['oracle'] or '-'}"
        )
    if result["missing"]:
        lines.append("no start point yet: " + ", ".join(f"{a['name']}" for a in result["missing"]))
    return "\n".join(lines)
