"""Profile-driven Wayland stream services, endpoint checks, and real capture."""

from __future__ import annotations

import argparse
import os
import socket
from datetime import UTC, datetime
from typing import Any

from abu_decomp import DecompError

from . import tiles
from .support import run_logged, sha256_file


def _command(values: list[str], substitutions: dict[str, str]) -> list[str]:
    if not values or not all(isinstance(item, str) for item in values):
        raise ValueError("configured command must be a nonempty string list")
    return [item.format_map(substitutions) for item in values]


def serve(profile: Any, name: str) -> dict[str, Any]:
    config = profile.section("stream")
    service = config["services"][name]
    argv = _command(service["command"], {})
    env = os.environ.copy()
    env.update({str(key): str(value) for key, value in service.get("env", {}).items()})
    os.execvpe(argv[0], argv, env)
    raise RuntimeError("exec returned unexpectedly")


def capture(profile: Any, kind: str, seconds: int = 8, publish: bool = False) -> dict[str, Any]:
    if kind not in {"image", "video"}:
        raise ValueError("capture kind must be image or video")
    if seconds <= 0 or seconds > 600:
        raise ValueError("capture seconds must be 1..600")
    config = profile.section("stream")
    root = profile.resolve_path(config["capture_root"])
    root.mkdir(parents=True, exist_ok=True)
    extension = ".png" if kind == "image" else ".mp4"
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
    output = root / (kind + "-" + timestamp + extension)
    substitutions = {"output": str(output), "seconds": str(seconds)}
    argv = _command(config["capture_" + kind + "_command"], substitutions)
    run_logged(argv, stage="capture", failure_key="stream.capture_failed")
    if not output.is_file() or output.stat().st_size == 0:
        raise DecompError("stream.capture_failed", "capture command produced no nonempty file")
    result = {
        "kind": kind,
        "path": str(output),
        "bytes": output.stat().st_size,
        "sha256": sha256_file(output),
        "command": argv,
    }
    if publish:
        publish_argv = _command(config["publish_command"], substitutions)
        run_logged(publish_argv, stage="publish", failure_key="stream.publish_failed")
        result["publish_command"] = publish_argv
    return result


def status(profile: Any) -> dict[str, Any]:
    config = profile.section("stream")
    endpoints = []
    for value in config.get("endpoints", []):
        host, port = str(value["host"]), int(value["port"])
        try:
            with socket.create_connection((host, port), timeout=1):
                active = True
        except OSError:
            active = False
        endpoints.append({"name": value["name"], "host": host, "port": port, "active": active})
    runtime = profile.resolve_path(config["runtime_dir"])
    display = str(config["wayland_display"])
    url = None
    if "up" in config:
        from .up import endpoint_url

        url = endpoint_url(config["up"])
    return {
        "url": url,
        "wayland_socket": str(runtime / display),
        "wayland_socket_present": (runtime / display).is_socket(),
        "endpoints": endpoints,
    }


def run(profile: Any, argv: tuple[str, ...]) -> dict[str, Any]:
    parser = argparse.ArgumentParser(prog="decomp stream")
    commands = parser.add_subparsers(dest="action", required=True)
    serving = commands.add_parser("serve")
    serving.add_argument("name")
    shot = commands.add_parser("capture")
    shot.add_argument("kind", choices=["image", "video"])
    shot.add_argument("--seconds", type=int, default=8)
    shot.add_argument("--publish", action="store_true")
    commands.add_parser("status")
    commands.add_parser("up")
    applying = commands.add_parser("apply")
    applying.add_argument("--watch", action="store_true", help="keep overlay geometry current using window events")
    commands.add_parser("down")
    for name in ("restart", "stop"):
        acting = commands.add_parser(name)
        acting.add_argument("role")
        acting.add_argument("--owner", action="store_true", help="also act on the viewer's playable role")
    sending = commands.add_parser("send")
    sending.add_argument("role")
    sending.add_argument("--owner", action="store_true", help="also type into the viewer's playable role")
    sending.add_argument("keys", nargs="+")
    role = commands.add_parser("role")
    role.add_argument("--id", required=True)
    args = parser.parse_args(argv)
    if args.action == "serve":
        return serve(profile, args.name)
    if args.action == "capture":
        return capture(profile, args.kind, args.seconds, args.publish)
    if args.action in {"restart", "stop", "send"}:
        from . import control

        up_config = tiles.up_config(profile)
        run_dir = profile.resolve_path(up_config["run_dir"])
        control.guard_player(up_config, args.role, args.action, args.owner)
        if args.action == "restart" and args.role in up_config.get("services", {}):
            from . import services

            return services.signal_unit(run_dir, up_config, args.role)
        if args.action == "send":
            return control.send(run_dir, up_config, args.role, args.keys)
        return getattr(control, args.action)(run_dir, args.role)
    if args.action == "down":
        from .transport import change

        return change(profile.section("stream"), "stop")
    if args.action == "up":
        from .up import up

        return up(profile)
    if args.action == "apply":
        from .apply import apply

        return apply(profile, watch=args.watch)
    if args.action == "role":
        from .up import run_role

        return run_role(profile, args.id)
    return status(profile)
