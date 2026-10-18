"""Committed-source build and verified release build.

`decomp build once|status` reads the profile `build` section (SCHEMA.md); `BuildProfile` gives the moved runner the
project, runtime and output roots it used to read from a Cake Taste profile.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from abu_decomp.config import Profile, check_keys

ROOTS = ("project_root", "runtime_root", "output_root")


class BuildProfile:
    """The seat profile seen as the build runner reads it: three roots, the digest and the `build` table."""

    def __init__(self, profile: Profile) -> None:
        table = profile.section("build")
        check_keys(profile.source_file, "build", table, frozenset(ROOTS), table.keys() | frozenset(ROOTS))
        for key in ROOTS:
            setattr(self, key, profile.path(f"build.{key}", table.pop(key)))
        self.project_root: Path
        self.runtime_root: Path
        self.output_root: Path
        self.source_file = profile.source_file
        self.digest = profile.digest
        self._build = table
        self._profile = profile

    def section(self, name: str) -> dict[str, Any]:
        if name != "build":
            raise KeyError(name)
        return copy.deepcopy(self._build)

    def resolve_path(self, value: str) -> Path:
        return self._profile.resolve_path(value)
