"""Port boilerplate: a native port repository skeleton generated from one port spec.

The skeleton fixes the shape every port shares: layout, build presets, the platform boundary, the boot chain
with an explicit stop at the first unimplemented owner, the boot gate test, ci/check and the README. What goes
inside the platform and the game is the port's own work. Project facts come only from the spec, which is
copied into the new repository as port.json.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from abu_decomp import DecompError

TEMPLATE = "template"
LICENSES = {"CC0-1.0": "Released under [CC0 1.0](LICENSE)."}
_PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]*$")
_SPEC_KEYS = frozenset({"schema_version", "name", "title", "executable", "license", "boot", "decomp"})
_BOOT_KEYS = frozenset({"subsystem", "label", "owner"})
_DECOMP_KEYS = frozenset({"repo", "version"})
# Output names that cannot be stored under their real name in the package.
_RENAMES = {"gitignore": ".gitignore"}


class PortError(DecompError):
    def __init__(self, detail: str) -> None:
        super().__init__("port.spec", detail)


@dataclass(frozen=True)
class Spec:
    values: dict[str, Any]

    def placeholders(self) -> dict[str, str]:
        value, boot = self.values, self.values["boot"]
        return {
            "name": value["name"],
            "title": value["title"],
            "executable": value["executable"],
            "cmake_project": re.sub(r"[^A-Za-z0-9]", "", value["name"].title()),
            "license_line": LICENSES[value["license"]],
            "boot_subsystem": boot["subsystem"],
            "boot_label": boot["label"],
            "boot_owner": boot["owner"],
        }


def _text(table: dict[str, Any], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip() or any(c in value for c in '"\\\n{}'):
        raise PortError(f"{where}.{key}: expected a nonempty one-line string without quotes, braces or backslashes")
    return value


def _keys(table: Any, where: str, required: frozenset[str]) -> dict[str, Any]:
    if not isinstance(table, dict):
        raise PortError(f"{where}: expected an object")
    missing, extra = required - table.keys(), table.keys() - required
    if missing or extra:
        raise PortError(f"{where}: missing {sorted(missing)}, unknown {sorted(extra)}")
    return table


def load_spec(path: Path) -> Spec:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise PortError(f"{path}: {exc}") from exc
    _keys(value, "spec", _SPEC_KEYS)
    if value["schema_version"] != 1:
        raise PortError("spec.schema_version: only version 1 is supported")
    for key in ("name", "executable"):
        if not _IDENTIFIER.match(_text(value, key, "spec")):
            raise PortError(f"spec.{key}: expected lowercase letters, digits and hyphens")
    _text(value, "title", "spec")
    if value["license"] not in LICENSES:
        raise PortError(f"spec.license: expected one of {sorted(LICENSES)}")
    boot = _keys(value["boot"], "spec.boot", _BOOT_KEYS)
    for key in sorted(_BOOT_KEYS):
        _text(boot, key, "spec.boot")
    if not re.fullmatch(r"0x[0-9A-F]{8}", boot["owner"]):
        raise PortError("spec.boot.owner: expected an 8-digit uppercase hex address such as 0x80071000")
    decomp = _keys(value["decomp"], "spec.decomp", _DECOMP_KEYS)
    for key in sorted(_DECOMP_KEYS):
        _text(decomp, key, "spec.decomp")
    return Spec(value)


def render(text: str, values: dict[str, str], where: str) -> str:
    def replace(match: re.Match[str]) -> str:
        if match[1] not in values:
            raise PortError(f"template {where}: unknown placeholder {match[1]}")
        return values[match[1]]

    return _PLACEHOLDER.sub(replace, text)


def generate(spec: Spec, destination: Path) -> dict[str, Any]:
    """Write the skeleton into an empty or absent directory; never overwrite."""
    if destination.exists() and any(destination.iterdir()):
        raise DecompError("port.destination", f"{destination} is not empty")
    values = spec.placeholders()
    root = resources.files(__package__).joinpath(TEMPLATE)
    written: list[str] = []
    pending: list[tuple[Path, str, bool]] = []
    stack = [(root, Path())]
    while stack:
        node, relative = stack.pop()
        for child in sorted(node.iterdir(), key=lambda item: item.name):
            target = relative / child.name
            if child.is_dir():
                stack.append((child, target))
                continue
            name = str(target)
            if name.startswith("LICENSE."):
                if name != "LICENSE." + spec.values["license"]:
                    continue
                target = Path("LICENSE")
            target = target.with_name(_RENAMES.get(target.name, target.name))
            text = child.read_text()
            pending.append(
                (target, text if target.name == "LICENSE" else render(text, values, name), name == "ci/check")
            )
    destination.mkdir(parents=True, exist_ok=True)
    for target, text, executable in sorted(pending):
        path = destination / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if executable:
            path.chmod(0o755)
        written.append(str(target))
    (destination / "port.json").write_text(json.dumps(spec.values, indent=2) + "\n")
    written.append("port.json")
    return {"destination": str(destination), "files": sorted(written)}
