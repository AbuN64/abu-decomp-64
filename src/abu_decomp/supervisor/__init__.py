"""Supervisor: run a target under gdb with retry and capture, the boot gate, RUN-FAIL events. Headless.

The finite child runner lives in abu_decomp.files.
It writes `status_path` and RUN-FAIL through the Cake Taste writer; it draws
nothing. Section `supervisor`: {"runtime_root": path, "roles": {ROLE: {...}}}; a role is `supervised` or `artifact`
with the keys the moved code reads (status_path, runs_root, release_current, command, ...).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from abu_decomp import DecompError
from abu_decomp.config import Profile, check_keys

KINDS = frozenset({"supervised", "artifact"})


class SupervisorProfile:
    """The seat profile as the moved supervisor reads it: runtime_root, resolve_path and the seat feed path."""

    def __init__(self, profile: Profile) -> None:
        from abu_decomp import events

        table = profile.section("supervisor")
        keys = frozenset({"runtime_root", "roles"})
        check_keys(profile.source_file, "supervisor", table, keys, keys)
        self.runtime_root = profile.path("supervisor.runtime_root", table["runtime_root"])
        self.roles: dict[str, dict[str, Any]] = table["roles"]
        self._profile = profile
        try:
            self.events_path: Path | None = events.events_path(profile)
        except DecompError:
            self.events_path = None

    def resolve_path(self, value: str) -> Path:
        return self._profile.resolve_path(value)

    def role(self, role_id: str, kind: str) -> dict[str, Any]:
        if role_id not in self.roles:
            raise DecompError("supervisor.role", f"unknown role {role_id}; roles: {', '.join(sorted(self.roles))}")
        config = self.roles[role_id]
        if config.get("kind") != kind:
            raise DecompError("supervisor.role", f"role {role_id} is not {kind}")
        return config
