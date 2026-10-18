"""Lanes boundary: launch, steer and end AI lanes; the model ladder; the registry and its overlap guard.

The launcher and steer move here 1:1 from the orchestration scripts. The ladder values and the registry
file are workspace configuration; this module holds only the mechanism.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from abu_decomp import DecompError

LaneKind = Literal["tooling", "impl"]
Track = Literal["official", "experiment"]


@dataclass(frozen=True)
class Lane:
    """One registry row. `owns` are the paths this lane may write; no two live lanes may overlap."""

    name: str
    kind: LaneKind
    model: str
    track: Track
    owns: tuple[str, ...]
    unit: str | None = None
    transcript: str | None = None
    socket: str | None = None


@dataclass(frozen=True)
class Ladder:
    order: tuple[str, ...]
    models: dict[str, str]
    kinds: dict[str, dict[str, Any]]

    @classmethod
    def load(cls, path: Path) -> Ladder:
        try:
            value = tomllib.loads(path.read_text())
            ladder = cls(tuple(value["ladder"]), dict(value["models"]), dict(value["kind"]))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DecompError("lanes.ladder", f"{path}: {exc}") from exc
        for name, rule in ladder.kinds.items():
            if rule.get("min") not in ladder.order or rule.get("max") not in ladder.order:
                raise DecompError("lanes.ladder", f"{path}: kind.{name} min/max must be ladder rungs")
        return ladder

    def check(self, kind: str, model: str, owner_override: str | None = None) -> str:
        """The model's rung, or a refusal. The top rung and any rung outside the kind's range need the owner."""
        if kind not in self.kinds:
            raise DecompError("lanes.kind", f"unknown lane kind {kind}; expected one of {sorted(self.kinds)}")
        if model not in self.models:
            raise DecompError("lanes.model", f"model {model} is not on the ladder")
        rung, rule = self.models[model], self.kinds[kind]
        low, high, at = self.order.index(rule["min"]), self.order.index(rule["max"]), self.order.index(rung)
        top = rung == self.order[-1] and rule.get("opus_needs_owner", True)
        if (at < low or at > high or top) and not (owner_override and owner_override.strip()):
            raise DecompError(
                "lanes.ladder",
                f"{kind} lanes run {rule['min']}..{rule['max']}; {model} ({rung}) needs an owner override",
            )
        return rung


def _overlaps(first: str, second: str) -> bool:
    a, b = PurePosixPath(first), PurePosixPath(second)
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def overlap_findings(candidate: Lane, live: Iterable[Lane], active_units: Iterable[str] = ()) -> list[str]:
    """Why `candidate` may not launch beside the live lanes: shared written paths or an already active unit."""
    findings = []
    if not candidate.owns:
        findings.append(f"{candidate.name} declares no owned paths")
    for path in candidate.owns:
        if not PurePosixPath(path).is_absolute():
            findings.append(f"{candidate.name} owns a relative path {path}")
    for lane in live:
        if lane.name == candidate.name:
            findings.append(f"{candidate.name} is already live")
        for mine in candidate.owns:
            for theirs in lane.owns:
                if _overlaps(mine, theirs):
                    findings.append(f"{candidate.name} {mine} overlaps {lane.name} {theirs}")
    if candidate.unit and candidate.unit in set(active_units):
        findings.append(f"unit {candidate.unit} is already active")
    return findings
