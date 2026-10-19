"""`decomp lane launch|register|continue|steer|end`: the lane lifecycle over the registry, ladder and systemd units.

The registry (`lanes.registry`) is the one list of lanes: Codex lanes enter it at launch, a Claude lane (no unit; its
transcript is the agent's task output) enters it with `register`, and the orchestrator's peek reads it."""

from __future__ import annotations

import contextlib
import io
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import abu_decomp
from abu_decomp import DecompError, config, tools
from abu_decomp.config import Profile
from abu_decomp.lanes import Ladder, Lane, continuation, jobs, overlap_findings, registry, steer

DONE_WHEN = ("identical", "result")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_KEYS = frozenset({"registry", "ladder", "codex", "socket_dir", "rules"})
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


def settings(profile: Profile) -> Settings:
    """The `lanes` section: registry file, model ladder, codex binary, socket directory and brief rules."""
    table = profile.section("lanes")
    config.check_keys(profile.source_file, "lanes", table, _KEYS, _KEYS)
    return Settings(
        *[profile.path(f"lanes.{key}", table[key]) for key in ("registry", "ladder", "codex", "socket_dir", "rules")]
    )


def announce(profile: Profile, row: dict[str, Any], status: str, message: str) -> dict[str, Any] | None:
    """Append the LANE event for a lifecycle step if the seat has an events feed; a failure is reported."""
    from abu_decomp import events

    try:
        path = events.events_path(profile)
    except DecompError:
        return None
    lane = {key: row[key] for key in ("name", "kind", "model", "track") if row.get(key)}
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


def launch(
    profile: Profile, spec: dict[str, Any], replaces: dict[str, Any] | None = None, new_brief: str | None = None
) -> dict[str, Any]:
    """Check the ladder and the overlap guard, record the lane in the registry, then start its unit.

    `replaces` ({"name", "dir"}) is the lane this one continues: its rows end in the same registry write.
    `new_brief` is a continuation's brief: --dir does not exist yet, and it and brief.md are written only after every
    check passed, so a refusal leaves nothing behind.
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
    for export in spec.get("export") or []:
        path = Path(export)
        if not path.is_absolute() or not any(path.is_relative_to(Path(owned)) for owned in spec["owns"]):
            raise DecompError("lanes.arguments", f"--export must be an absolute path under an --owns path: {export}")
    directory, cwd = Path(spec["dir"]), Path(spec["cwd"])
    brief = directory / "brief.md"
    if new_brief is None:
        for label, path in (("--dir", directory), ("--cwd", cwd)):
            if not path.is_absolute() or not path.is_dir():
                raise DecompError("lanes.arguments", f"{label} must be an existing absolute directory: {path}")
        if not brief.is_file():
            raise DecompError("lanes.arguments", f"{brief} is missing; write the brief first")
    else:
        if not directory.is_absolute() or (directory.exists() and any(directory.iterdir())):
            raise DecompError("lanes.arguments", f"{directory} already exists and is not empty")
        if not cwd.is_absolute() or not (cwd == directory or cwd.is_dir()):
            raise DecompError("lanes.arguments", f"--cwd must be an existing absolute directory: {cwd}")
    ladder = Ladder.load(seat.ladder)
    rung = ladder.check(spec["kind"], spec["model"], spec.get("owner_override"))
    # The lane runs the tool installed with decomp, so it must be the version its project's own pin names.
    if refused := tools.pin_findings(profile, cwd):
        raise DecompError("lanes.tool_pin", "; ".join(refused))
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
        created = new_brief is not None
        if created:
            directory.mkdir(parents=True, exist_ok=True)
            brief.write_text(new_brief)
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
            "--kind", spec["kind"], "--ladder", str(seat.ladder), "--profile", str(profile.source_file),
        ]  # fmt: skip
        if spec.get("owner_override"):
            command += ["--owner-override", spec["owner_override"]]
        for path in lane.owns:
            command += ["--owns", path]
        for path in spec.get("export") or []:
            command += ["--export", path]
        command += ["--track", spec["track"]]
        try:
            start_unit(command, directory)
        except DecompError:
            if created:  # the failed start leaves the directory this call made, so the retry is not refused
                shutil.rmtree(directory, ignore_errors=True)
            raise
        rows.append(row)
    return {
        "lane": row,
        "unit": unit,
        "event": announce(profile, row, "started", f"launched {name} on {spec['model']}"),
    }


def register(profile: Profile, spec: dict[str, Any]) -> dict[str, Any]:
    """Record a lane that has no unit (a Claude agent) as live, after the ladder check: its transcript is the agent's
    task output, which the peek reads. `lane end` closes it like any other row."""
    seat = settings(profile)
    name, transcript = spec["name"], Path(spec["transcript"])
    if not _NAME.fullmatch(name):
        raise DecompError("lanes.name", f"lane name {name!r} must be letters, digits, dot, dash or underscore")
    if not transcript.is_absolute():
        raise DecompError("lanes.arguments", f"--transcript must be an absolute path: {transcript}")
    rung = Ladder.load(seat.ladder).check(spec["kind"], spec["model"], spec.get("owner_override"))
    lane = Lane(name, spec["kind"], spec["model"], spec["track"], (), transcript=str(transcript))
    with registry.locked(seat.registry) as rows:
        if any(r["name"] == name and r.get("state") == "live" for r in rows):
            raise DecompError("lanes.overlap", f"{name} is already live in {seat.registry}")
        row = registry.row_of(
            lane,
            state="live",
            started=time.time(),
            agent="claude",
            rung=rung,
            owner_override=spec.get("owner_override") or None,
        )
        rows.append(row)
    return {"lane": row, "event": announce(profile, row, "started", f"registered {name} on {spec['model']}")}


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


def end(profile: Profile, name: str, reason: str, stop: bool) -> dict[str, Any]:
    """Mark a lane ended. A lane whose unit is still active is only ended with --stop, which stops that unit."""
    seat = settings(profile)
    with registry.locked(seat.registry) as rows:
        row = next((r for r in reversed(rows) if r["name"] == name and r.get("state") == "live"), None)
        if row is None:
            raise DecompError("lanes.unknown", f"no live lane {name} in {seat.registry}")
        if row.get("unit") and row["unit"] in registry.active_units():
            if not stop:
                raise DecompError("lanes.live", f"{row['unit']} is still running; pass --stop to stop it")
            stop_unit(row["unit"])
        row.update(state="ended", ended=time.time(), reason=reason)
    return {"lane": row, "event": announce(profile, row, "passed", f"ended {name}: {reason}")}


def continue_lane(profile: Profile, old_dir: Path, name: str, given: dict[str, Any], wait: int = 0) -> dict[str, Any]:
    """Launch NAME as the continuation of the closed lane in OLD_DIR, in the sibling directory NAME.

    `wait` seconds are allowed for OLD's unit to close (the runner continues its own lane while its unit winds down).

    Each spec key comes from the first source that records it: OLD's latest registry row, then OLD/LAUNCH.json
    (written by the runner at start), then OLD's unit ExecStart (gone once a `--collect` unit ends). `given`
    (the command's flags) fills what no source recorded and otherwise overrides; nothing is guessed.
    """
    seat = settings(profile)
    old_dir = old_dir.expanduser().resolve()
    if not (old_dir / "brief.md").is_file():
        raise DecompError("lanes.arguments", f"{old_dir / 'brief.md'} is missing")
    keys = (*INHERITED, "cwd", "owner_override", "export")
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
    deadline = time.monotonic() + wait
    while True:
        active = [candidate for candidate in candidates if candidate in registry.active_units()]
        if not active or time.monotonic() >= deadline:
            break
        time.sleep(1)
    if active:
        raise DecompError("lanes.live", f"{active[0]} is still active; continue a lane only after it has closed")
    spec.update({key: value for key, value in given.items() if value not in (None, [], ())})
    continuation.require(spec, INHERITED)
    new_dir = old_dir.parent / name
    if new_dir.exists() and any(new_dir.iterdir()):
        raise DecompError("lanes.arguments", f"{new_dir} already exists and is not empty")
    old_cwd = Path(spec.get("cwd") or old_dir)
    brief = continuation.continuation_brief(
        (old_dir / "brief.md").read_text(), seat.rules.read_text(), old_name, old_dir, new_dir, jobs.adopt_text(old_dir)
    )
    spec.update(
        name=name,
        dir=str(new_dir),
        cwd=str(new_dir if old_cwd == old_dir else old_cwd),
        owns=[str(path) for path in spec["owns"]],
        owner_override=spec.get("owner_override") or None,
    )
    result = launch(profile, spec, replaces={"name": old_name, "dir": str(old_dir)}, new_brief=brief)
    return {**result, "continued_from": {"name": old_name, "dir": str(old_dir), "unit": unit, "sources": sources}}
