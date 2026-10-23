"""Which recorded movie takes the original to a function, from the TAS coverage table, for `behave`'s oracle capture.

`behave` seeds every comparison with the state the real game entered the function with. The capture ran one fixed
input script for every function, so a function that script never reaches ended "entry not reached" after the whole
time limit and its cases stayed synthetic (25-40% untestable). The coverage table (`decomp tas coverage`,
runs/coverage-table.json) already says, per function, which movie the original enters it in and at which poll. This
module turns that row into the capture's own run: the movie (with its start point's polls, Expansion Pak and card)
written content-addressed beside the behave work, the oracle host's variables that replay it, and a time limit that
follows the poll. One rule for every function; the movie, the poll and the host's variables come from the profile
and the table. A function no movie reaches gets the named verdict `behave.no_movie` and no capture at all (the
fixed script could not reach it either, and the time limit is not spent).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, tas, tas_coverage, tas_oracle
from abu_decomp.config import Profile

MIN_SECONDS = 60  # the oracle boot and the shim set-up, before the first poll
NO_MOVIE = "behave.no_movie"


def _stored(directory: Path, data: bytes, suffix: str) -> Path:
    path = directory / f"{hashlib.sha256(data).hexdigest()[:20]}{suffix}"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(suffix + f".{id(data)}.part")
        part.write_bytes(data)
        part.replace(path)
    return path


def choose(
    profile: Profile, work: Path, function: str, version: str, poll_seconds: float
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(the capture's own run, a finding). The run is {env, seconds, movie, poll, count (entries in the movie)};
    None with no finding means the
    table cannot say (no TAS section, no table, another version): the profile's fixed script stands. A function
    the table says no movie reaches is None with the `behave.no_movie` finding."""
    try:
        cfg = tas.settings(profile)
        oracle = tas_oracle.settings(profile)
    except DecompError:
        return None, None
    table = tas_coverage.read_table(cfg)
    if not table or oracle is None or table.get("summary", {}).get("version") != version:
        return None, None
    row = next((r for r in table["functions"] if r["name"] == function), None)
    if row is None or not row["in_version"]:
        return None, None
    if not row["oracle"]["hit"]:
        return None, {
            "key": NO_MOVIE,
            "blocking": False,
            "message": f"no recorded movie reaches {function} in the original (coverage table, "
            f"{table['summary']['movies']} movies, state {row['state']}): "
            "its cases stay synthetic; record a movie that calls it, then run `decomp tas coverage`",
        }
    name, poll = row["oracle"]["movie"], int(row["oracle"]["poll"] or 0)
    try:
        polls = tas.movie_polls(cfg, name)
        pak = tas.movie_pak(cfg, name)
        start = tas.start_card(cfg, tas.movie_cpak(cfg, name))
    except DecompError as exc:
        return None, {
            "key": "behave.movie",
            "blocking": False,
            "message": f"{name} reaches {function} but cannot be replayed: {exc}",
        }
    directory = work / "movies"
    play = _stored(directory, tas.format_raw(polls).encode(), ".raw")
    card = _stored(directory, start, ".mpk") if start is not None else None
    env = tas_oracle.movie_env(play, pak, tas.pad_count(polls), cfg["exit_after"], card)
    seconds = MIN_SECONDS + int(poll * poll_seconds + 0.999)
    return {
        "env": env,
        "seconds": seconds,
        "movie": name,
        "poll": poll,
        "count": int(row["oracle"].get("count") or 0),
    }, None
