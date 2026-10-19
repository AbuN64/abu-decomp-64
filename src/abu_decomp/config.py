"""Explicit JSON seat profiles. The core checks the top level; each component validates its own section."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from abu_decomp import DecompError

# One section per component boundary (see CONTRACTS.md). Project facts live only in these values.
SECTIONS = frozenset(
    {
        "tools",
        "events",
        "tiles",
        "stream",
        "console",
        "lanes",
        "supervisor",
        "build",
        "cycle",
        "probe",
        "land",
        "behave",
    }
)
_REQUIRED = frozenset({"schema_version", "name"})
_ALLOWED = _REQUIRED | SECTIONS


class ConfigError(DecompError):
    def __init__(self, file: Path, key: str, reason: str) -> None:
        super().__init__("config.schema", f"{file}:{key}: {reason}")


@dataclass(frozen=True)
class Profile:
    name: str
    source_file: Path
    digest: str
    _sections: Mapping[str, Mapping[str, Any]] = field(repr=False)

    def section(self, name: str) -> dict[str, Any]:
        """A fresh copy of one component's settings, so a component cannot mutate the profile."""
        if name not in SECTIONS:
            raise ConfigError(self.source_file, name, "unknown component section")
        if name not in self._sections:
            raise ConfigError(self.source_file, name, "required section is absent")
        return copy.deepcopy(dict(self._sections[name]))

    def resolve_path(self, value: str) -> Path:
        """Resolve a path value as the moved stream and console code spells it."""
        return resolve(self.source_file, "path", value)

    def path(self, key: str, value: Any) -> Path:
        """Resolve a profile path; relative paths are relative to the profile file."""
        return resolve(self.source_file, key, value)

    def describe(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "name": self.name,
            "source_file": str(self.source_file),
            "profile_sha256": self.digest,
            "sections": sorted(self._sections),
        }


def resolve(file: Path, key: str, value: Any) -> Path:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ConfigError(file, key, "expected a nonempty path string")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else file.parent / path).resolve()


def check_keys(file: Path, key: str, table: Any, required: frozenset[str], allowed: frozenset[str]) -> None:
    if not isinstance(table, dict):
        raise ConfigError(file, key, "expected an object")
    missing, extra = required - table.keys(), table.keys() - allowed
    if missing:
        raise ConfigError(file, key, f"missing keys: {', '.join(sorted(missing))}")
    if extra:
        raise ConfigError(file, key, f"unknown keys: {', '.join(sorted(extra))}")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _nonfinite(value: str) -> Any:
    raise ValueError(f"nonfinite JSON number: {value}")


def load_profile(path: Path) -> Profile:
    file = path.expanduser().resolve()
    try:
        content = file.read_bytes()
        value = json.loads(content, object_pairs_hook=_unique, parse_constant=_nonfinite)
    except (OSError, ValueError, UnicodeError) as exc:
        raise ConfigError(file, "profile", str(exc)) from exc
    check_keys(file, "profile", value, _REQUIRED, _ALLOWED)
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ConfigError(file, "schema_version", "only version 1 is supported")
    name = value["name"]
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(file, "name", "expected a nonempty string")
    sections: dict[str, Mapping[str, Any]] = {}
    for key in sorted(SECTIONS & value.keys()):
        if not isinstance(value[key], dict):
            raise ConfigError(file, key, "expected an object")
        sections[key] = MappingProxyType(copy.deepcopy(value[key]))
    return Profile(name, file, hashlib.sha256(content).hexdigest(), MappingProxyType(sections))
