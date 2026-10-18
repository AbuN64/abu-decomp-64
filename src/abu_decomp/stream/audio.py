"""Shared PulseAudio sink captured by Sunshine."""

from __future__ import annotations

import subprocess
from typing import Any


def ensure_sink(name: str, env: dict[str, str], run: Any = subprocess.run) -> bool:
    """Create the null sink when absent; return True when it had to be created."""
    listing = run(["pactl", "list", "short", "sinks"], capture_output=True, text=True, env=env, check=True).stdout
    if any(line.split("\t")[1:2] == [name] for line in listing.splitlines()):
        return False
    run(
        ["pactl", "load-module", "module-null-sink", f"sink_name={name}", f"sink_properties=device.description={name}"],
        capture_output=True,
        env=env,
        check=True,
    )
    return True
