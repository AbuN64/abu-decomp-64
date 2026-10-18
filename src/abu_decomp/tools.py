"""The pinned decomp tools: pin verification and isolated calls to each public CLI.

The pins are the direct requirements of this installed distribution (pyproject.toml), read back from its
metadata, so no pin is written twice. A tool is accepted only when its installed direct_url.json names the
same repository and commit.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from abu_decomp import DecompError
from abu_decomp.config import Profile, check_keys

DISTRIBUTION = "abu-decomp-64"


@dataclass(frozen=True)
class Tool:
    distribution: str
    module: str
    envelope: bool  # emits the ok/result/findings/stages/invocation envelope


# Command name -> installed distribution. Order is the dependency order.
TOOLS = {
    "unbake": Tool("unbake", "unbake.cli", envelope=True),
    "recipe": Tool("recipemaker", "recipemaker.cli", envelope=False),
    "taste": Tool("cake-taste-64", "cake_taste.cli", envelope=True),
}
ENVELOPE = frozenset({"ok", "result", "findings", "stages", "invocation"})
_REQUIREMENT = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)@git\+(?P<url>https://[^@]+)@(?P<commit>[0-9a-f]{40})$")


class DependencyError(DecompError):
    pass


@dataclass(frozen=True)
class Pin:
    distribution: str
    url: str
    commit: str

    @property
    def requirement(self) -> str:
        return f"{self.distribution}@git+{self.url}@{self.commit}"


def _normal(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirements(distribution: metadata.Distribution) -> list[str]:
    return ["".join(row.split(";")[0].split()) for row in distribution.requires or []]


def pins() -> dict[str, Pin]:
    """The direct VCS pins this installed package declares, keyed by tool command."""
    try:
        own = metadata.distribution(DISTRIBUTION)
    except metadata.PackageNotFoundError as exc:
        raise DependencyError("dependency.self", f"{DISTRIBUTION} is not installed; run pip install -e .") from exc
    found: dict[str, Pin] = {}
    for row in _requirements(own):
        match = _REQUIREMENT.match(row)
        if match:
            found[_normal(match["name"])] = Pin(match["name"], match["url"], match["commit"])
    result: dict[str, Pin] = {}
    for command, tool in TOOLS.items():
        pin = found.get(_normal(tool.distribution))
        if pin is None:
            raise DependencyError("dependency.pin", f"{DISTRIBUTION} declares no commit pin for {tool.distribution}")
        result[command] = pin
    return result


def verify_dependencies() -> dict[str, dict[str, str]]:
    """Require every installed tool at its pinned commit, one unbake for all, and a standalone Cake Taste."""
    declared = pins()
    report: dict[str, dict[str, str]] = {}
    installed: dict[str, metadata.Distribution] = {}
    for command, pin in declared.items():
        try:
            distribution = metadata.distribution(pin.distribution)
        except metadata.PackageNotFoundError as exc:
            raise DependencyError("dependency.missing", f"{pin.distribution} is not installed") from exc
        raw = distribution.read_text("direct_url.json")
        if raw is None:
            raise DependencyError("dependency.provenance", f"{command} has no direct_url.json VCS provenance")
        try:
            origin = json.loads(raw)
            url, vcs = origin["url"], origin["vcs_info"]
            valid = url.removesuffix(".git") == pin.url and vcs["vcs"] == "git" and vcs["commit_id"] == pin.commit
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise DependencyError("dependency.provenance", f"{command} has invalid VCS provenance") from exc
        if not valid:
            raise DependencyError("dependency.pin", f"{command} must come from {pin.url} at {pin.commit}")
        installed[command] = distribution
        report[command] = {
            "distribution": pin.distribution,
            "version": distribution.version,
            "url": pin.url,
            "commit": pin.commit,
        }
    if declared["unbake"].requirement not in _requirements(installed["recipe"]):
        raise DependencyError("dependency.compatibility", "Recipe Maker pins a different unbake than this package")
    stray = {_normal(row.split("@")[0]) for row in _requirements(installed["taste"])} & {
        _normal(TOOLS["unbake"].distribution),
        _normal(TOOLS["recipe"].distribution),
    }
    if stray:
        raise DependencyError("dependency.compatibility", f"Cake Taste must be standalone; it requires {sorted(stray)}")
    return report


@dataclass(frozen=True)
class Settings:
    project: Path
    host: Path
    timeout_seconds: float
    taste_profile: Path | None


def settings(profile: Profile) -> Settings:
    """The `tools` section: the decomp project, the unbake host file, one timeout, and the Taste profile."""
    file = profile.source_file
    table = profile.section("tools")
    check_keys(
        file,
        "tools",
        table,
        frozenset({"project", "host", "timeout_seconds"}),
        frozenset({"project", "host", "timeout_seconds", "taste_profile"}),
    )
    project, host = profile.path("tools.project", table["project"]), profile.path("tools.host", table["host"])
    timeout = table["timeout_seconds"]
    try:
        finite = type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0
    except OverflowError:
        finite = False
    if not finite:
        raise DependencyError("config.schema", f"{file}:tools.timeout_seconds: expected a finite positive number")
    if not project.is_dir() or not host.is_file():
        raise DependencyError("config.schema", f"{file}:tools: project directory and host file must exist")
    taste = profile.path("tools.taste_profile", table["taste_profile"]) if "taste_profile" in table else None
    return Settings(project, host, float(timeout), taste)


@dataclass(frozen=True)
class ToolResult:
    tool: str
    argv: tuple[str, ...]
    returncode: int
    document: dict[str, Any]
    stderr: str
    provenance: dict[str, dict[str, str]]
    context: dict[str, str]

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.document["ok"] is True

    def describe(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "argv": list(self.argv),
            "exit_code": self.returncode,
            "document": self.document,
            "provenance": self.provenance,
            "context": self.context,
        }


def _command(tool: str, chosen: Settings, given: tuple[str, ...]) -> list[str]:
    owned = ("--profile",) if tool == "taste" else ("--host", "--project")
    if any(arg in owned or arg.startswith(tuple(f"{flag}=" for flag in owned)) for arg in given):
        raise DependencyError("dependency.arguments", f"{tool} {'/'.join(owned)} come only from the seat profile")
    if tool == "taste":
        if chosen.taste_profile is None:
            raise DependencyError("dependency.configuration", "tools.taste_profile is not set in the seat profile")
        return ["--profile", str(chosen.taste_profile), *given]
    command = ["--project", str(chosen.project), given[0]]
    if tool == "recipe":
        command.append("--json")
    return [*command, *given[1:]]


def invoke(tool: str, profile: Profile, args: Sequence[str], *, stdin: bytes = b"") -> ToolResult:
    """Run the installed tool's public main(argv) in a clean interpreter; one JSON document on stdout."""
    if tool not in TOOLS:
        raise DependencyError("dependency.unknown", f"unknown tool: {tool}")
    given = tuple(args)
    if given and given[0] == "--":
        given = given[1:]
    if not given or given[0].startswith("-") or any("\0" in arg for arg in given):
        raise DependencyError("dependency.arguments", "provide a public command followed by its arguments")
    chosen = settings(profile)
    command = _command(tool, chosen, given)
    provenance = verify_dependencies()
    bootstrap = f"from {TOOLS[tool].module} import main; raise SystemExit(main())"
    environment = dict(os.environ)
    # unbake and Recipe Maker read UNBAKE_HOST; the seat profile is its only source.
    environment["UNBAKE_HOST"] = str(chosen.host)
    environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
    try:
        completed = subprocess.run(
            [str(Path(sys.executable).absolute()), "-I", "-c", bootstrap, *command],
            cwd=chosen.project,
            input=stdin,
            capture_output=True,
            timeout=chosen.timeout_seconds,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise DependencyError("dependency.timeout", f"{tool} exceeded {chosen.timeout_seconds:g}s") from exc
    except OSError as exc:
        raise DependencyError("dependency.process", f"cannot execute {tool}: {exc}") from exc
    stderr = completed.stderr.decode("utf-8", errors="replace")
    try:
        document = json.loads(completed.stdout.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise DependencyError(
            "dependency.output", f"{tool} exited {completed.returncode} without one JSON document; {stderr.strip()}"
        ) from exc
    if not isinstance(document, dict) or type(document.get("ok")) is not bool:
        raise DependencyError("dependency.output", f"{tool} JSON requires an explicit boolean ok")
    if "findings" in document and not isinstance(document["findings"], list):
        raise DependencyError("dependency.output", f"{tool} JSON findings must be a list")
    if TOOLS[tool].envelope and not document.keys() >= ENVELOPE:
        raise DependencyError("dependency.output", f"{tool} omitted fields of its public result envelope")
    return ToolResult(
        tool,
        (tool, *command),
        completed.returncode,
        document,
        stderr,
        provenance,
        {"project": str(chosen.project), "host": str(chosen.host), "profile_sha256": profile.digest},
    )
