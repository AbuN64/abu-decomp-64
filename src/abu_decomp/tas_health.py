"""The port build's health, read before every tas run and inside every stop classification.

A port build that fails a unit may still exit 0: the unit's step leaves empty objects and a `<unit>.failed` marker
holding the first compile error, and the link denies the unit, so a function inside it later appears as a stop that
looks like a game defect. Likewise a binary older than its sources runs old code. Both are read here so neither can be
mistaken for a game stop. Section `tas.build`: {"units": folder holding the `*.failed` markers (the build's unit tree),
"sources": text file listing the absolute source paths the build used, "rebuild": the command that rebuilds}.
Nothing here blocks a play; it only makes the state impossible to miss.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from abu_decomp import tas_triage

BUILD_KEYS = frozenset({"units", "sources", "rebuild"})


def failed_units(units: Path) -> list[dict[str, str]]:
    """Every unit with a `.failed` marker: its path relative to `units` and the marker's first error line."""
    rows = []
    for marker in sorted(units.rglob("*.failed")) if units.is_dir() else []:
        text = marker.read_text(errors="replace").strip()
        unit = str(marker.relative_to(units))[: -len(".failed")]
        rows.append({"unit": unit, "error": text.splitlines()[0] if text else ""})
    return rows


def newest_source(listing: Path) -> tuple[float, str] | None:
    newest: tuple[float, str] | None = None
    for line in listing.read_text(errors="replace").splitlines():
        path = line.strip()
        if not path:
            continue
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, path)
    return newest


def read(cfg: dict[str, Any]) -> dict[str, Any]:
    """{failed: [...], stale: bool, stale_by, rebuild, summary}; {} when the profile configures no build section."""
    build = cfg.get("build")
    if not build:
        return {}
    out: dict[str, Any] = {"failed": [], "stale": False, "rebuild": build.get("rebuild", "")}
    if build.get("units"):
        out["failed"] = failed_units(Path(build["units"]))
    port, sources = Path(cfg["port"]), build.get("sources")
    if sources and Path(sources).is_file() and port.is_file():
        newest = newest_source(Path(sources))
        if newest and newest[0] > port.stat().st_mtime:
            out["stale"], out["stale_by"] = True, newest[1]
    parts = []
    if out["failed"]:
        parts.append(f"build: {len(out['failed'])} units failed")
    if out["stale"]:
        parts.append("stale: binary older than source")
    out["summary"] = "; ".join(parts) or "build: ok"
    return out


def header(health: dict[str, Any]) -> dict[str, Any]:
    """The compact form every run result carries (empty when nothing is configured)."""
    if not health:
        return {}
    out: dict[str, Any] = {"summary": health["summary"]}
    if health["failed"]:
        out["failed"] = health["failed"]
    if health["stale"]:
        out["stale"] = f"binary older than source: {health['stale_by']}"
        if health["rebuild"]:
            out["rebuild"] = health["rebuild"]
    return out


def _unit_of(project: Path | None, name: str, failed: list[dict[str, str]]) -> dict[str, str] | None:
    site = tas_triage.definition_site(project, name) or tas_triage.include_asm_site(project, name)
    if not site:
        return None
    file = site.rpartition(":")[0]
    for row in failed:
        if file.endswith(row["unit"]):
            return {**row, "site": site}
    return None


def apply(profile: Any, health: dict[str, Any], outcome: dict[str, Any], out: dict[str, Any]) -> dict[str, Any]:
    """Overlay a claim (tas_triage/tas_symbolize) with the build facts that explain a stop."""
    if not health or not out or (not health["failed"] and not health["stale"]):
        return out
    subject = str(outcome.get("subject") or "")
    unit = None
    if health["failed"] and tas_triage._FUNCTION.fullmatch(subject):
        unit = _unit_of(tas_triage._project(profile), subject, health["failed"])
    if unit:
        out.update(
            kind="unit failed to compile",
            cannot_help="input cannot pass this point: the unit holding the function is not in the binary",
            owner=f"{subject} at {unit['site']}",
            next=f"fix the compile error in {unit['site'].rpartition(':')[0]}: {unit['error']}",
            blocking=True,
        )
        out.pop("owes", None)
    if health["stale"]:
        out["stale"] = True
        if health["rebuild"] and unit is None:
            if out.get("kind") == "unknown":
                out["kind"] = "stale binary"
            out["then"] = out.get("next")
            out["next"] = health["rebuild"]
    out["build"] = health["summary"]
    out["claim"] = (
        f"{out.get('kind')}: {subject or outcome.get('outcome')}; blocking={str(out.get('blocking', True)).lower()}; "
        f"{out.get('cannot_help', '')}"
        + (f"; owner {out['owner']}" if "owner" in out else "")
        + f"; {health['summary']}; next: {out.get('next')}"
    )
    return out
