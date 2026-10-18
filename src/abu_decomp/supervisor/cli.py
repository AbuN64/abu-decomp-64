"""`decomp supervisor run|status|show-artifact|viewer` over the profile `supervisor` roles."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from abu_decomp.config import Profile

from . import SupervisorProfile


def run(
    profile: Profile, action: str, role_id: str, source: Path | None = None, name: str | None = None
) -> dict[str, Any]:
    seat = SupervisorProfile(profile)
    if action == "run":
        from .supervisor import run_supervisor

        return run_supervisor(seat, role_id, seat.role(role_id, "supervised"))
    if action == "status":
        path = seat.resolve_path(seat.role(role_id, "supervised")["status_path"])
        return {
            "role": role_id,
            "status_path": str(path),
            "status": json.loads(path.read_text()) if path.is_file() else None,
        }
    from . import artifact

    config = seat.role(role_id, "artifact")
    if action == "show-artifact":
        assert source is not None and name is not None
        return artifact.show(seat, config, source, name)
    selected = artifact.current(seat, config)
    mapping = {"image": selected["display_copy"], "name": selected["name"], "runtime_root": str(seat.runtime_root)}
    command = [str(item).format_map(mapping) for item in config["command"]]
    env = os.environ.copy()
    env.update({str(key): str(value).format_map(mapping) for key, value in config.get("env", {}).items()})
    os.execvpe(command[0], command, env)
    raise RuntimeError("exec returned")
