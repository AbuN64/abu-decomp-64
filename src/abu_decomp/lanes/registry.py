"""The lane registry (lanes.json), the one list of lanes. `decomp lane launch|register|continue|end` write it."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from abu_decomp import DecompError
from abu_decomp.lanes import Lane


def active_units() -> set[str]:
    """Names of the active `lane-*` systemd user units (whoever launched them)."""
    called = subprocess.run(
        ["systemctl", "--user", "list-units", "lane-*", "--state=active", "--no-legend", "--plain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if called.returncode != 0:
        raise DecompError("lanes.systemd", f"cannot list lane units: {called.stderr.strip()}")
    return {row.split()[0].removesuffix(".service") for row in called.stdout.splitlines() if row.strip()}


def row_of(lane: Lane, **extra: Any) -> dict[str, Any]:
    return {**asdict(lane), "owns": list(lane.owns), **extra}


def lane_of(row: dict[str, Any]) -> Lane:
    fields = Lane.__dataclass_fields__.keys()
    value = {key: row[key] for key in fields if key in row}
    value["owns"] = tuple(row.get("owns", ()))
    return Lane(**value)


def load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        document = json.loads(path.read_text())
        rows = document["lanes"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DecompError("lanes.registry", f"{path}: {exc}") from exc
    if document.get("schema") != 1 or not isinstance(rows, list):
        raise DecompError("lanes.registry", f"{path}: expected schema 1 with a lanes list")
    return rows


@contextmanager
def locked(path: Path) -> Iterator[list[dict[str, Any]]]:
    """Exclusive access to the rows; the caller mutates the list and it is written back atomically on success."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        rows = load(path)
        yield rows
        temporary = path.with_name(path.name + ".new")
        temporary.write_text(json.dumps({"schema": 1, "lanes": rows}, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, path)


def live_rows(rows: list[dict[str, Any]], units: set[str]) -> list[dict[str, Any]]:
    """Rows still live. A live row whose unit has stopped is closed here, so a dead lane never blocks a launch; a row
    with no unit (a registered Claude lane) stays live until `lane end`."""
    live = []
    for row in rows:
        if row.get("state") != "live":
            continue
        if row.get("unit") is None or row["unit"] in units:
            live.append(row)
        else:
            row.update(state="ended", ended=time.time(), reason="unit inactive")
    return live
