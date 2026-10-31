"""The lockstep stream of a recorded run: `decomp tas stream pack RUN` turns the oracle's `.ppm` shots into
`RUN/stream/{port,original}/POLL.jpg` and `RUN/stream/index.json` (polls, every, per-poll inputs from movie.raw).

A run played with `--oracle --oracle-pictures` dumps both sides every `tas.oracle.shot_every` polls and the rig prunes
the shots after a few minutes, so a pack only adds: polls already packed stay when their shots are gone. A poll is
in the stream when both sides pictured it, so the page shows the same poll on both sides by construction; the poll
index is the logical clock.
"""

from __future__ import annotations

import io
import json
import os
from itertools import pairwise
from pathlib import Path
from typing import Any

from PIL import Image

from abu_decomp import DecompError, tas, tas_oracle

STREAM = "stream"
INDEX = "index.json"
SIDES = ("port", "original")  # the stream's sides; the original is the oracle's folder on disk
DISK = {"port": "port", "original": "oracle"}
JPEG_QUALITY = 82


def folder(run: Path) -> Path:
    return run / STREAM


def frame_path(run: Path, side: str, poll: int) -> Path:
    return folder(run) / side / f"{poll}.jpg"


def packed(run: Path) -> list[int]:
    """The polls both sides hold a frame for, ascending."""
    both: set[int] | None = None
    for side in SIDES:
        here = {int(p.stem) for p in (folder(run) / side).glob("*.jpg") if p.stem.isdigit()}
        both = here if both is None else both & here
    return sorted(both or ())


def jpeg(path: Path) -> bytes:
    """One shot as a JPEG (the shot is a complete P6 picture: tas_oracle.shots only lists complete ones)."""
    with Image.open(path) as image, io.BytesIO() as out:
        image.convert("RGB").save(out, "JPEG", quality=JPEG_QUALITY, subsampling=0)
        return out.getvalue()


def write(path: Path, body: bytes) -> None:
    """Atomic: a viewer never reads half a frame."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_bytes(body)
    os.replace(part, path)


def inputs(run: Path, polls: list[int]) -> list[list[int] | None]:
    """[buttons, x, y] of the first pad at each poll, None past the end of the run's movie.raw."""
    raw = run / "movie.raw"
    movie = tas.read_raw(raw) if raw.is_file() else []
    return [list(movie[poll][:3]) if poll < len(movie) else None for poll in polls]


def every(run: Path, polls: list[int]) -> int:
    """The shot interval the run recorded, else the smallest gap between its polls."""
    record = tas.read_record(run) or {}
    given = (record.get("oracle") or {}).get("every")
    if given:
        return int(given)
    gaps = [b - a for a, b in pairwise(polls)]
    if not gaps:
        raise DecompError("tas.stream", f"run {run.name} has no oracle shot interval and fewer than two polls")
    return min(gaps)


def pack(run: Path) -> dict[str, Any]:
    """Add the run's complete shot pairs to its stream and rewrite index.json; refuse a run with no stream at all."""
    shots = {side: tas_oracle.shots(run, DISK[side]) for side in SIDES}
    added = 0
    for poll in sorted(set.intersection(*(set(found) for found in shots.values()))):
        for side, found in shots.items():
            if not (target := frame_path(run, side, poll)).is_file():
                write(target, jpeg(found[poll]))
                added += 1
    polls = packed(run)
    if not polls:
        raise DecompError(
            "tas.stream",
            f"run {run.name} has no pictured polls on both sides",
            "play it with --oracle --oracle-pictures",
        )
    index = {"run": run.name, "polls": polls, "inputs": inputs(run, polls), "every": every(run, polls)}
    write(folder(run) / INDEX, json.dumps(index, separators=(",", ":")).encode())
    return {"run": run.name, "polls": len(polls), "added": added, "every": index["every"]}


def pack_all(cfg: dict[str, Any], names: list[str]) -> list[dict[str, Any]]:
    """Pack the named runs, or every run folder that holds shots when none are named."""
    plays = cfg["runs"] / "plays"
    if names:
        runs = []
        for name in names:
            run = tas.run_folder(cfg, name)
            if run is None:
                raise DecompError("tas.stream", f"no run {name}")
            runs.append(run)
    else:
        runs = sorted(p for p in plays.iterdir() if (p / tas.SHOTS).is_dir()) if plays.is_dir() else []
    return [pack(run) for run in runs]
