"""`decomp lane launch|steer|peek|end`: the lane lifecycle over the registry, the ladder and systemd user units."""

from __future__ import annotations

import contextlib
import io
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, config
from abu_decomp.config import Profile
from abu_decomp.lanes import Ladder, Lane, overlap_findings, peek, registry, steer

DONE_WHEN = ("identical", "result")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_KEYS = frozenset({"registry", "ladder", "codex", "socket_dir", "rules"})
RUNNER = Path(__file__).with_name("runner.py")


@dataclass(frozen=True)
class Settings:
    registry: Path
    ladder: Path
    codex: Path
    socket_dir: Path
    rules: Path


def settings(profile: Profile) -> Settings:
    """The `lanes` section: registry file, model ladder, codex binary, socket directory and brief rules."""
    table = profile.section("lanes")
    config.check_keys(profile.source_file, "lanes", table, _KEYS, _KEYS)
    return Settings(
        *(profile.path(f"lanes.{key}", table[key]) for key in ("registry", "ladder", "codex", "socket_dir", "rules"))
    )


def announce(profile: Profile, row: dict[str, Any], status: str, message: str) -> dict[str, Any] | None:
    """Append the LANE event for a lifecycle step if the seat has an events feed; a failure is reported."""
    from abu_decomp import events

    try:
        path = events.events_path(profile)
    except DecompError:
        return None
    lane = {key: row[key] for key in ("name", "kind", "model", "track")}
    try:
        return events.append(
            path, {"actor": row["name"], "status": status, "message": message, "kind": "LANE", "lane": lane}
        )
    except DecompError as exc:
        return {"error": exc.detail}


def unit_of(name: str) -> str:
    return f"lane-{name}"


def start_unit(command: list[str], directory: Path) -> None:
    """Run the lane as its own systemd user unit so it outlives the caller."""
    called = subprocess.run(command, capture_output=True, text=True, check=False)
    if called.returncode != 0:
        raise DecompError("lanes.systemd", f"systemd-run failed: {called.stderr.strip()}")


def stop_unit(unit: str) -> None:
    called = subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, text=True, check=False)
    if called.returncode != 0:
        raise DecompError("lanes.systemd", f"cannot stop {unit}: {called.stderr.strip()}")


def launch(profile: Profile, spec: dict[str, Any]) -> dict[str, Any]:
    """Check the ladder and the overlap guard, record the lane in the registry, then start its unit."""
    seat = settings(profile)
    name = spec["name"]
    if not _NAME.fullmatch(name):
        raise DecompError("lanes.name", f"lane name {name!r} must be letters, digits, dot, dash or underscore")
    if spec["done_when"] not in DONE_WHEN:
        raise DecompError("lanes.done_when", f"--done-when must be one of {', '.join(DONE_WHEN)}")
    if spec["wall"] <= 0 or spec["continues"] < 0:
        raise DecompError("lanes.arguments", "--wall must be positive and --continues nonnegative")
    if (spec["fanout"] == "enabled") != (spec["max_agents"] > 0):
        raise DecompError("lanes.arguments", "enabled fanout needs a positive --max-agents; disabled needs 0")
    directory, cwd = Path(spec["dir"]), Path(spec["cwd"])
    for label, path in (("--dir", directory), ("--cwd", cwd)):
        if not path.is_absolute() or not path.is_dir():
            raise DecompError("lanes.arguments", f"{label} must be an existing absolute directory: {path}")
    brief = directory / "brief.md"
    if not brief.is_file():
        raise DecompError("lanes.arguments", f"{brief} is missing; write the brief first")
    ladder = Ladder.load(seat.ladder)
    rung = ladder.check(spec["kind"], spec["model"], spec.get("owner_override"))
    unit = unit_of(name)
    socket = seat.socket_dir / f"{name}.sock"
    lane = Lane(
        name,
        spec["kind"],
        spec["model"],
        spec["track"],
        tuple(str(Path(path)) for path in spec["owns"]),
        unit=unit,
        transcript=str(directory / "transcript.jsonl"),
        socket=str(socket),
    )
    with registry.locked(seat.registry) as rows:
        units = registry.active_units()
        live = registry.live_rows(rows, units)
        findings = overlap_findings(lane, [registry.lane_of(row) for row in live], units)
        if findings:
            raise DecompError("lanes.overlap", "; ".join(findings))
        text = brief.read_text()
        if not text.startswith("LANE RULES") and "\nLANE RULES" not in text:
            brief.write_text(text.rstrip("\n") + "\n\n" + seat.rules.read_text())
        row = registry.row_of(
            lane,
            state="live",
            started=time.time(),
            dir=str(directory),
            cwd=str(cwd),
            rung=rung,
            owner_override=spec.get("owner_override") or None,
        )
        command = [
            "systemd-run", "--user", "--collect", "--quiet", f"--unit={unit}", f"--working-directory={directory}",
            f"--property=StandardOutput=append:{directory / 'launch.log'}",
            f"--property=StandardError=append:{directory / 'launch.log'}",
            sys.executable, str(RUNNER),
            "--codex", str(seat.codex), "--model", spec["model"], "--effort", spec["effort"],
            "--fanout", spec["fanout"], "--max-agents", str(spec["max_agents"]), "--cwd", str(cwd),
            "--brief", str(brief), "--dir", str(directory), "--socket", str(socket), "--name", name,
            "--wall", str(spec["wall"]), "--continues", str(spec["continues"]), "--done-when", spec["done_when"],
            "--kind", spec["kind"], "--ladder", str(seat.ladder),
        ]  # fmt: skip
        if spec.get("owner_override"):
            command += ["--owner-override", spec["owner_override"]]
        start_unit(command, directory)
        rows.append(row)
    return {
        "lane": row,
        "unit": unit,
        "event": announce(profile, row, "started", f"launched {name} on {spec['model']}"),
    }


def _row(profile: Profile, name: str) -> dict[str, Any]:
    seat = settings(profile)
    rows = [row for row in registry.load(seat.registry) if row["name"] == name and row.get("state") == "live"]
    if not rows:
        raise DecompError("lanes.unknown", f"no live lane {name} in {seat.registry}")
    return rows[-1]


def steer_lane(
    profile: Profile, name: str | None, directory: Path | None, socket: Path | None, file: Path
) -> dict[str, Any]:
    """Append the file text to the lane active turn; a lane not in the registry takes --dir and --socket."""
    if name:
        row = _row(profile, name)
        directory, socket = Path(row["dir"]), Path(row["socket"])
    if directory is None or socket is None:
        raise DecompError("lanes.arguments", "pass --name, or both --dir and --socket for a lane launched elsewhere")
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            code = steer.main([str(directory), str(socket), str(file)])
    except SystemExit as exc:
        raise DecompError("lanes.steer", str(exc.code)) from exc
    except OSError as exc:
        raise DecompError("lanes.steer", str(exc)) from exc
    if code != 0:
        raise DecompError("lanes.steer", output.getvalue().strip())
    result = {"dir": str(directory), "socket": str(socket), "answer": output.getvalue().strip()}
    if name:
        result["event"] = announce(profile, row, "info", f"steered {name} from {file.name}")
    return result


def peek_lanes(profile: Profile, names: list[str], minutes: float, extra: list[str]) -> dict[str, Any]:
    """Recent activity of the named live lanes (all live lanes when none are named) plus any NAME=PATH given."""
    seat = settings(profile)
    rows = [row for row in registry.load(seat.registry) if row.get("state") == "live"]
    if names:
        rows = [row for row in rows if row["name"] in names]
        missing = set(names) - {row["name"] for row in rows}
        if missing:
            raise DecompError("lanes.unknown", f"no live lane {', '.join(sorted(missing))}")
    specs = [f"{row['name']}={row['transcript']}" for row in rows] + extra
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        peek.main(["--minutes", str(minutes), *specs])
    return {"lanes": [spec.partition("=")[0] for spec in specs], "text": output.getvalue()}


def end(profile: Profile, name: str, reason: str, stop: bool) -> dict[str, Any]:
    """Mark a lane ended. A lane whose unit is still active is only ended with --stop, which stops that unit."""
    seat = settings(profile)
    with registry.locked(seat.registry) as rows:
        row = next((r for r in reversed(rows) if r["name"] == name and r.get("state") == "live"), None)
        if row is None:
            raise DecompError("lanes.unknown", f"no live lane {name} in {seat.registry}")
        if row["unit"] in registry.active_units():
            if not stop:
                raise DecompError("lanes.live", f"{row['unit']} is still running; pass --stop to stop it")
            stop_unit(row["unit"])
        row.update(state="ended", ended=time.time(), reason=reason)
    return {"lane": row, "event": announce(profile, row, "passed", f"ended {name}: {reason}")}
