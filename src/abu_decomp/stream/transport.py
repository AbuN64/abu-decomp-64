"""Moonlight transport lifecycle, separate from the compositor and tile processes."""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any

from . import browser


def settings(stream: dict[str, Any]) -> dict[str, Any]:
    if stream.get("transport") != "moonlight":
        raise ValueError('stream.transport must be "moonlight"')
    config = stream.get("moonlight", {})
    units = config.get("units")
    if (
        not isinstance(units, list)
        or not units
        or not all(isinstance(unit, str) and re.fullmatch(r"[a-zA-Z0-9_.@-]+\.service", unit) for unit in units)
    ):
        raise ValueError("stream.moonlight.units must name user systemd services")
    runtime = config.get("systemd_runtime_dir")
    if not isinstance(runtime, str) or not runtime.startswith("/"):
        raise ValueError("stream.moonlight.systemd_runtime_dir must be an absolute path")
    return config


def change(stream: dict[str, Any], action: str) -> dict[str, Any]:
    config = settings(stream)
    if action not in {"start", "stop"}:
        raise ValueError("transport action must be start or stop")
    if action == "start" and "browser" in config:
        browser.install(config["browser"], stream["up"]["output"])
    runtime = config["systemd_runtime_dir"]
    env = {**os.environ, "XDG_RUNTIME_DIR": runtime, "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + runtime + "/bus"}
    units = config["units"] if action == "start" else list(reversed(config["units"]))
    subprocess.run(["systemctl", "--user", action, *units], env=env, check=True, timeout=30)
    return {"transport": "moonlight", "stopped": action == "stop"}
