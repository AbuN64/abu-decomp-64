"""`decomp stream up`: headless compositor, role tiles in columns and the Moonlight browser endpoint."""

from __future__ import annotations

import os
import select
import signal
import sys
from pathlib import Path
from typing import Any

from . import audio, cursor, layout, services, transport
from .control import pidfile
from .inotify import wait_for_path
from .supervise import FatalUnitExit, Group, RoleRunner, Unit
from .sway import SwayIPC, _receive
from .tiles import up_config

# A service that exits is started again after this many seconds; `"restart_seconds": null` runs it once.
DEFAULT_RESTART = 5.0


def restart_seconds(service: dict[str, Any]) -> float | None:
    value = service.get("restart_seconds", DEFAULT_RESTART)
    return None if value is None else float(value)


def endpoint_url(up: dict[str, Any]) -> str:
    scheme = up.get("http_scheme", "http")
    if scheme not in {"http", "https"}:
        raise ValueError("stream URL scheme must be http or https")
    return f"{scheme}://{up['public_host']}:{up['http_port']}/stream.html"


def substitutions(profile: Any, up: dict[str, Any], run_dir: Path, display: str = "") -> dict[str, str]:
    return {
        "python": services.stable_python(),
        "profile": str(profile.source_file),
        "run_dir": str(run_dir),
        "wayland_display": display,
        "output_name": str(up["output"]["name"]),
        "http_port": str(up["http_port"]),
    }


def format_command(values: list[str], mapping: dict[str, str]) -> list[str]:
    if not values or not all(isinstance(item, str) for item in values):
        raise ValueError("configured command must be a nonempty string list")
    return [item.format_map(mapping) for item in values]


def format_env(values: dict[str, Any], mapping: dict[str, str]) -> dict[str, str]:
    return {str(key): str(value).format_map(mapping) for key, value in values.items()}


def role_command(up: dict[str, Any], role: str, mapping: dict[str, str]) -> list[str]:
    return format_command(up["roles"][role]["command"], mapping)


def wait_window(ipc: SwayIPC, wanted: str, timeout: float = 60.0) -> None:
    """Block on sway window events until a window with this app_id exists."""

    def present(node: dict[str, Any]) -> bool:
        if node.get("app_id") == wanted:
            return True
        return any(present(child) for child in node.get("nodes", []) + node.get("floating_nodes", []))

    connection = ipc.subscribe()
    try:
        while not present(ipc.tree()):
            if not select.select([connection], [], [], timeout)[0]:
                raise TimeoutError("window never appeared: " + wanted)
            _receive(connection)
    finally:
        connection.close()


def up(profile: Any) -> dict[str, Any]:
    stream = profile.section("stream")
    transport.settings(stream)
    config = up_config(profile)
    layout.validate(config)
    run_dir = profile.resolve_path(config["run_dir"])
    # An existing compositor belongs to the tile supervisor. Never unlink its
    # sockets or restart its applications when changing the streaming transport.
    sockets = list(run_dir.glob("sway-ipc.*.sock"))
    if sockets:
        if len(sockets) != 1:
            raise RuntimeError("ambiguous stream compositor sockets")
        SwayIPC(sockets[0]).tree()  # Fail closed on stale or inaccessible sockets.
        return {**transport.change(stream, "start"), "url": endpoint_url(config)}
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.chmod(0o700)
    conf = run_dir / "sway.conf"
    ready = run_dir / "tiles-ready"
    ready.unlink(missing_ok=True)
    for stale in [*run_dir.glob("sway-ipc.*"), *run_dir.glob("wayland-*")]:
        stale.unlink(missing_ok=True)
    base_mapping = substitutions(profile, config, run_dir)
    base_env = {"XDG_RUNTIME_DIR": str(run_dir), **format_env(config.get("env", {}), base_mapping)}
    hidden = config.get("cursor") == "hidden"
    if hidden:
        base_env.update(
            XCURSOR_PATH=str(cursor.write_theme(run_dir / "cursors")), XCURSOR_THEME=cursor.THEME, XCURSOR_SIZE="24"
        )
    if "audio" in config:
        audio.ensure_sink(config["audio"]["sink"], {**os.environ, **base_env})
    bar = config.get("bar")
    conf.write_text(
        layout.sway_config(
            config, format_command(bar["command"], base_mapping) if bar else None, cursor.THEME if hidden else None
        )
    )
    group = Group()
    unit_dir = services.units_dir(run_dir)
    unit_dir.mkdir(parents=True, exist_ok=True)
    for old in unit_dir.glob("*.json"):
        old.unlink(missing_ok=True)  # records of a previous stream up; every unit writes its own again
    services.record(
        run_dir, services.UP, os.getpid(), kind="up", venv=os.path.realpath(sys.prefix), python=services.stable_python()
    )
    named = set(config.get("services", {}))
    group.on_spawn = lambda unit: (
        services.record(run_dir, unit.name, unit.process.pid, kind="service") if unit.name in named else None
    )
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        sway_env = {**base_env, **format_env(config.get("compositor_env", {}), substitutions(profile, config, run_dir))}
        sway = Unit("compositor", [*config.get("compositor", ["sway"]), "-c", str(conf)], sway_env, fatal=True)
        group.start(sway)
        assert sway.process is not None
        ipc_path = run_dir / f"sway-ipc.{os.getuid()}.{sway.process.pid}.sock"
        if not wait_for_path(ipc_path, 30):
            raise RuntimeError("compositor IPC socket never appeared: " + str(ipc_path))
        sockets = sorted(p.name for p in run_dir.glob("wayland-*") if not p.name.endswith(".lock"))
        if not sockets:
            raise RuntimeError("compositor created no wayland socket")
        display = sockets[0]
        mapping = substitutions(profile, config, run_dir, display)
        child_env = {**base_env, "WAYLAND_DISPLAY": display, "SWAYSOCK": str(ipc_path)}

        def start_service(name: str, service: dict[str, Any]) -> None:
            group.start(
                Unit(
                    name,
                    format_command(service["command"], mapping),
                    {**child_env, **format_env(service.get("env", {}), mapping)},
                    restart_seconds(service),
                )
            )

        for name, service in config.get("services", {}).items():
            if not service.get("after_tiles"):
                start_service(name, service)
        ipc = SwayIPC(ipc_path)
        prefix = config.get("app_id_prefix", "tile-")
        terminal = config.get("terminal", ["foot", "--app-id={app_id}"])

        def spawn(role: str) -> None:
            argv = format_command(terminal, {**mapping, "app_id": layout.app_id(role, prefix)})
            argv += [
                services.stable_python(),
                "-m",
                "abu_decomp",
                "--profile",
                str(profile.source_file),
                "stream",
                "role",
                "--id",
                role,
            ]
            group.start(Unit("tile-" + role, argv, {**child_env, "STREAM_READY": str(ready)}))

        layout.build_tree(ipc, config, spawn, lambda wanted: wait_window(ipc, wanted))
        ready.touch()
        for role, spec in config["roles"].items():
            if spec.get("headless"):
                argv = [services.stable_python(), "-m", "abu_decomp", "--profile", str(profile.source_file)]
                group.start(Unit("role-" + role, [*argv, "stream", "role", "--id", role], child_env))
        for name, service in config.get("services", {}).items():
            if service.get("after_tiles"):
                start_service(name, service)
        transport.change(stream, "start")
        events = ipc.subscribe()

        def fix_overlays() -> None:
            change = _receive(events)[1]
            if isinstance(change, dict) and change.get("change") != "close":
                focus = layout.focus_command(change, config)
                if focus:
                    ipc.command(focus)
                for command in layout.overlay_fixes(ipc.tree(), config):
                    ipc.command(command)

        group.watch(events.fileno(), fix_overlays)
        print(endpoint_url(config), file=sys.stderr, flush=True)
        group.run_forever()
    except (KeyboardInterrupt, FatalUnitExit) as exc:
        group.stop()
        if isinstance(exc, FatalUnitExit):
            print(str(exc), file=sys.stderr, flush=True)
            raise SystemExit(1) from exc
    finally:
        group.stop()
        transport.change(stream, "stop")
    return {"stopped": True}


def run_role(profile: Any, role: str) -> dict[str, Any]:
    """Executed inside a tile terminal: run the role command forever, holding its last screen on exit."""
    config = up_config(profile)
    if role not in config["roles"]:
        raise ValueError("unknown stream role: " + role)
    spec = config["roles"][role]
    gate = os.environ.get("STREAM_READY")
    if gate and not wait_for_path(Path(gate), 120):
        raise TimeoutError("tile tree never became ready")
    run_dir = profile.resolve_path(config["run_dir"])
    mapping = substitutions(profile, config, run_dir, os.environ.get("WAYLAND_DISPLAY", ""))
    os.environ.update(format_env(spec.get("env", {}), mapping))
    pid_path = pidfile(run_dir, role)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))
    try:
        python = services.stable_python()
        RoleRunner(
            role_command(config, role, mapping),
            title=spec.get("title", role),
            on_start=lambda: services.record(run_dir, role, os.getpid(), kind="role", python=python),
        ).serve()
    finally:
        pid_path.unlink(missing_ok=True)
    return {"role": role}
