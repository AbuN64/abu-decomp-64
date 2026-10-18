"""`decomp lane launch|continue|steer|peek|end`: the lane lifecycle over the registry, ladder and systemd units."""

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

import abu_decomp
from abu_decomp import DecompError, config
from abu_decomp.config import Profile
from abu_decomp.lanes import Ladder, Lane, continuation, overlap_findings, peek, registry, steer

DONE_WHEN = ("identical", "result")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_KEYS = frozenset({"registry", "ladder", "codex", "socket_dir", "rules"})
# `active_file`: the NAME=PATH list lanepeek reads; launch, continue and end keep it current when it is set.
_OPTIONAL = frozenset({"active_file"})
# Spec keys a registry row keeps so `decomp lane continue` can relaunch the lane as it was.
INHERITED = ("model", "kind", "effort", "wall", "continues", "owns", "track", "done_when", "fanout", "max_agents")
RUNNER = Path(__file__).with_name("runner.py")


@dataclass(frozen=True)
class Settings:
    registry: Path
    ladder: Path
    codex: Path
    socket_dir: Path
    rules: Path
    active_file: Path | None = None


def settings(profile: Profile) -> Settings:
    """The `lanes` section: registry file, model ladder, codex binary, socket directory and brief rules."""
    table = profile.section("lanes")
    config.check_keys(profile.source_file, "lanes", table, _KEYS, _KEYS | _OPTIONAL)
    paths = [profile.path(f"lanes.{key}", table[key]) for key in ("registry", "ladder", "codex", "socket_dir", "rules")]
    active = profile.path("lanes.active_file", table["active_file"]) if "active_file" in table else None
    return Settings(*paths, active_file=active)


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


def launch(profile: Profile, spec: dict[str, Any], replaces: dict[str, Any] | None = None) -> dict[str, Any]:
    """Check the ladder and the overlap guard, record the lane in the registry, then start its unit.

    `replaces` ({"name", "dir"}) is the lane this one continues: its rows end in the same registry write.
    """
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
        if replaces:
            for old in rows:
                if old.get("dir") == str(replaces["dir"]) and old.get("state") != "replaced":
                    old.update(state="replaced", ended=old.get("ended") or time.time(), reason=f"continued by {name}")
            live = [row for row in live if row.get("state") == "live"]
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
            **{key: spec[key] for key in INHERITED if key not in {"model", "kind", "owns", "track"}},
            **({"continued_from": replaces["name"]} if replaces else {}),
        )
        command = [
            "systemd-run", "--user", "--collect", "--quiet", f"--unit={unit}", f"--working-directory={directory}",
            f"--property=StandardOutput=append:{directory / 'launch.log'}",
            f"--property=StandardError=append:{directory / 'launch.log'}",
            # The runner imports this same abu_decomp (its ladder), not whichever one the unit's python finds.
            f"--setenv=PYTHONPATH={Path(abu_decomp.__file__).parents[1]}",
            sys.executable, str(RUNNER),
            "--codex", str(seat.codex), "--model", spec["model"], "--effort", spec["effort"],
            "--fanout", spec["fanout"], "--max-agents", str(spec["max_agents"]), "--cwd", str(cwd),
            "--brief", str(brief), "--dir", str(directory), "--socket", str(socket), "--name", name,
            "--wall", str(spec["wall"]), "--continues", str(spec["continues"]), "--done-when", spec["done_when"],
            "--kind", spec["kind"], "--ladder", str(seat.ladder),
        ]  # fmt: skip
        if spec.get("owner_override"):
            command += ["--owner-override", spec["owner_override"]]
        for path in lane.owns:
            command += ["--owns", path]
        command += ["--track", spec["track"]]
        start_unit(command, directory)
        rows.append(row)
    if seat.active_file:
        old_dir = Path(replaces["dir"]) if replaces else None
        continuation.replace_active(seat.active_file, old_dir, name, lane.transcript)
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
    """Recent activity of the named live lanes (all live lanes when none are named) plus any NAME=PATH given.

    With `active_file` set the peek reads that registry as lanepeek.py does: NAME=PATH lanes plus `job` and `rig`
    lines (named lanes select NAME=PATH lines and skip the job and rig checks); the stage clock lives beside it in
    `.peek-stages.json`. Without it the live rows of the lane registry are the list."""
    seat = settings(profile)
    jobs: list[str] = []
    rigs: list[str] = []
    state = None
    if seat.active_file is not None:
        try:
            specs, jobs, rigs = peek.read_registry(seat.active_file)
        except OSError as exc:
            raise DecompError("lanes.peek", f"{seat.active_file}: {exc.strerror}") from exc
        state = seat.active_file.with_name(".peek-stages.json")
        found = {spec.partition("=")[0] for spec in specs}
    else:
        rows = [row for row in registry.load(seat.registry) if row.get("state") == "live"]
        specs = [f"{row['name']}={row['transcript']}" for row in rows]
        found = {row["name"] for row in rows}
    if names:
        missing = set(names) - found
        if missing:
            raise DecompError("lanes.unknown", f"no live lane {', '.join(sorted(missing))}")
        specs = [spec for spec in specs if spec.partition("=")[0] in names]
        jobs, rigs = [], []
    specs += extra
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        peek.run(minutes, specs, jobs, rigs, state)
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
    if seat.active_file:
        continuation.replace_active(seat.active_file, Path(row["dir"]), None, None)
    return {"lane": row, "event": announce(profile, row, "passed", f"ended {name}: {reason}")}


def continue_lane(profile: Profile, old_dir: Path, name: str, given: dict[str, Any]) -> dict[str, Any]:
    """Launch NAME as the continuation of the closed lane in OLD_DIR, in the sibling directory NAME.

    Each spec key comes from the first source that records it: OLD's latest registry row, then OLD/LAUNCH.json
    (written by the runner at start), then OLD's unit ExecStart (gone once a `--collect` unit ends). `given`
    (the command's flags) fills what no source recorded and otherwise overrides; nothing is guessed.
    """
    seat = settings(profile)
    old_dir = old_dir.expanduser().resolve()
    if not (old_dir / "brief.md").is_file():
        raise DecompError("lanes.arguments", f"{old_dir / 'brief.md'} is missing")
    keys = (*INHERITED, "cwd", "owner_override")
    spec: dict[str, Any] = {}
    units: list[str] = []
    names: list[str] = []
    sources: list[str] = []

    def fill(source: str, recorded: dict[str, Any], unit: str | None, old_name: str | None) -> None:
        taken = [key for key in keys if spec.get(key) in (None, "", []) and recorded.get(key) not in (None, "", [])]
        spec.update({key: recorded[key] for key in taken})
        units.extend([unit] if unit else [])
        names.extend([old_name] if old_name else [])
        sources.append(source)

    rows = [row for row in registry.load(seat.registry) if row.get("dir") == str(old_dir)]
    if rows:
        fill("registry", rows[-1], rows[-1].get("unit"), rows[-1].get("name"))
    record = continuation.launch_record(old_dir)
    if record is not None:
        fill("LAUNCH.json", record, f"lane-{record['name']}" if record.get("name") else None, record.get("name"))
    if any(spec.get(key) in (None, "", []) for key in INHERITED):
        found = continuation.old_unit(old_dir, units)
        if found is not None:
            fill("ExecStart", found[1], found[0], found[1].get("name"))
    if not sources and not any(value not in (None, [], ()) for value in given.values()):
        raise DecompError(
            "lanes.unknown",
            f"no registry row, LAUNCH.json or lane unit records {old_dir}; pass every setting as a flag",
        )
    old_name = names[0] if names else old_dir.name
    candidates = list(dict.fromkeys([*units, f"lane-{old_dir.name}", f"lane-{old_dir.name.lower()}"]))
    unit = candidates[0]
    active = [candidate for candidate in candidates if candidate in registry.active_units()]
    if active:
        raise DecompError("lanes.live", f"{active[0]} is still active; continue a lane only after it has closed")
    spec.update({key: value for key, value in given.items() if value not in (None, [], ())})
    continuation.require(spec, INHERITED)
    new_dir = old_dir.parent / name
    if new_dir.exists() and any(new_dir.iterdir()):
        raise DecompError("lanes.arguments", f"{new_dir} already exists and is not empty")
    old_cwd = Path(spec.get("cwd") or old_dir)
    brief = continuation.continuation_brief(
        (old_dir / "brief.md").read_text(), seat.rules.read_text(), old_name, old_dir, new_dir
    )
    new_dir.mkdir(exist_ok=True)
    (new_dir / "brief.md").write_text(brief)
    spec.update(
        name=name,
        dir=str(new_dir),
        cwd=str(new_dir if old_cwd == old_dir else old_cwd),
        owns=[str(path) for path in spec["owns"]],
        owner_override=spec.get("owner_override") or None,
    )
    result = launch(profile, spec, replaces={"name": old_name, "dir": str(old_dir)})
    return {**result, "continued_from": {"name": old_name, "dir": str(old_dir), "unit": unit, "sources": sources}}
