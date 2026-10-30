"""The port build's health, read before every tas run and inside every stop classification.

A port build that fails a unit may still exit 0: the unit's step leaves empty objects and a `<unit>.failed` marker
holding the first compile error, and the link denies the unit, so a function inside it later appears as a stop that
looks like a game defect. Likewise a binary older than its sources runs old code. Both are read here so neither can be
mistaken for a game stop. Section `tas.build`: {"units": folder holding the `*.failed` markers (the build's unit tree),
"sources": text file listing the absolute source paths the build used, "graph": [build graph files of the port, e.g.
build.ninja; `{port_dir}` is the port executable's folder], "rebuild": the command that rebuilds}.
A marker counts as a failure only when its unit is named in the sources listing or a graph file of the port being
reported; every other marker is left over from a build without that unit: it is listed as `stale`, with its age.
Nothing here blocks a play. Whether a shared port seat may run at all is tas_seat's rule (stamped repo, SHA and shim pin
against the project's current main); `pins_stale` only reports a page port older than its tree: "pins" is the stamp
the page-port build writes ({"rage_ports": sha}); "pins_repo" is the port tree it must match.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, tas_triage

BUILD_KEYS = frozenset({"units", "sources", "rebuild", "pins", "pins_repo", "lock", "graph"})


def failed_units(units: Path) -> list[dict[str, Any]]:
    """Every unit with a `.failed` marker: its path relative to `units`, the marker's first error line and its mtime."""
    rows = []
    for marker in sorted(units.rglob("*.failed")) if units.is_dir() else []:
        text = marker.read_text(errors="replace").strip()
        unit = str(marker.relative_to(units))[: -len(".failed")]
        rows.append({"unit": unit, "error": text.splitlines()[0] if text else "", "mtime": marker.stat().st_mtime})
    return rows


def _named_in(unit: str, text: str) -> bool:
    """True when `unit` appears in `text` as a whole path tail (not inside a longer file or folder name)."""
    return re.search(rf"(?<![\w.-]){re.escape(unit)}(?!\w)", text) is not None


def current_units(markers: list[dict[str, Any]], build: dict[str, Any], port: Path | None) -> list[dict[str, Any]]:
    """The markers whose unit the port's build names (its sources listing or build graph); the rest are stale.
    Refuses when no build file can be read: nothing then says which markers belong to this build."""
    files = [str(build["sources"])] if build.get("sources") else []
    folder = str(port.parent) if port else ""
    files += [str(g).replace("{port_dir}", folder) for g in build.get("graph") or []]
    texts = []
    for name in files:
        try:
            texts.append(Path(name).read_text(errors="replace"))
        except OSError:
            continue
    if markers and not texts:
        raise DecompError(
            "tas.health",
            f"{len(markers)} *.failed markers but none of the build files naming the port's units is readable: "
            f"{', '.join(files) or 'tas.build.sources and tas.build.graph are not set'}",
        )
    return [{**m, "current": any(_named_in(m["unit"], text) for text in texts)} for m in markers]


def _age(seconds: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return f"{int(seconds)}s"


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


def building(lock: Any) -> bool:
    """True while page-port.py ensure holds its lock (a rebuild is in progress; it publishes the new binary by an atomic
    swap, so plays started meanwhile use the previous one instead of being refused)."""
    import fcntl

    try:
        with open(lock, "rb") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
    except OSError:
        return False
    return False


def pins_lag(build: dict[str, Any], key: str = "rage_ports", what: str = "page port", tree: str = "rage-ports") -> str:
    """Empty when the binary was built at its tree's HEAD (or no stamp is configured), else why not. The stamp holds
    the built sha under `key`; the page port (rage-ports) and the page oracle (cake_taste) share this check."""
    if not (build.get("pins") and build.get("pins_repo")):
        return ""
    try:
        built = json.loads(Path(build["pins"]).read_text()).get(key, "")
    except (OSError, ValueError, AttributeError):
        built = ""
    done = subprocess.run(
        ["git", "-C", str(build["pins_repo"]), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    now = done.stdout.strip()
    if done.returncode or not now or built == now:
        return ""
    if build.get("lock") and building(build["lock"]) and built:
        return ""  # the rebuild is running: the last published binary stays valid for new plays until the swap
    return f"{what} older than pins: built at {tree} {built[:9] or 'unknown'}, pins are at {now[:9]}"


def read(cfg: dict[str, Any], profile: Any = None, now: float | None = None) -> dict[str, Any]:
    """{failed, stale_markers, stale: bool, stale_by, rebuild, summary}; {} when the profile configures no build
    section. `failed` holds only units of the port's current build; `stale_markers` the leftovers, with their age."""
    build = cfg.get("build")
    if not build:
        return {}
    out: dict[str, Any] = {"failed": [], "stale_markers": [], "stale": False, "rebuild": build.get("rebuild", "")}
    port = Path(cfg["port"]) if cfg.get("port") else None
    sources = build.get("sources")
    if build.get("units"):
        when = time.time() if now is None else now
        for row in current_units(failed_units(Path(build["units"])), build, port):
            age = max(0.0, when - row.pop("mtime"))
            if row.pop("current"):
                out["failed"].append(row)
            else:
                out["stale_markers"].append({**row, "age_seconds": int(age), "age": _age(age)})
    if port and sources and Path(sources).is_file() and port.is_file():
        newest = newest_source(Path(sources))
        if newest and newest[0] > port.stat().st_mtime:
            out["stale"], out["stale_by"] = True, newest[1]
    out["pins_stale"] = pins_lag(build)
    parts = []
    if out["pins_stale"]:
        parts.append(out["pins_stale"])
    if out["failed"]:
        parts.append(f"build: {len(out['failed'])} units failed")
    if out["stale"]:
        parts.append("stale: binary older than source")
    if out["stale_markers"]:
        oldest = max(m["age_seconds"] for m in out["stale_markers"])
        parts.append(
            f"{len(out['stale_markers'])} stale *.failed markers of units not in this build (oldest {_age(oldest)})"
        )
    out["summary"] = "; ".join(parts) or "build: ok"
    return with_pin_lag(profile, cfg, out)


def header(health: dict[str, Any]) -> dict[str, Any]:
    """The compact form every run result carries (empty when nothing is configured)."""
    if not health:
        return {}
    out: dict[str, Any] = {"summary": health["summary"]}
    if health["failed"]:
        out["failed"] = health["failed"]
    if health.get("stale_markers"):
        out["stale_markers"] = health["stale_markers"]
    if health.get("pins_stale"):
        out["pins_stale"] = health["pins_stale"]
        if health["rebuild"]:
            out["rebuild"] = health["rebuild"]
    if health["stale"]:
        out["stale"] = f"binary older than source: {health['stale_by']}"
        if health["rebuild"]:
            out["rebuild"] = health["rebuild"]
        if health.get("repin"):
            out["then"] = health["repin"]["then"]
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


def with_pin_lag(profile: Any, cfg: dict[str, Any], health: dict[str, Any]) -> dict[str, Any]:
    """A stale binary whose port tree pins an older source commit than the source's ref cannot rebuild (its configure
    refuses the moved source): the `rebuild` next becomes the repin, and the build command follows as `then`."""
    if not (health and health.get("stale") and health.get("rebuild") and profile is not None):
        return health
    from abu_decomp import repin

    try:
        lag = repin.lag(profile, Path(cfg["port"]))
    except (OSError, DecompError, KeyError):
        lag = None
    if not lag:
        return health
    return {**health, "repin": {**lag, "then": health["rebuild"]}, "rebuild": lag["next"]}


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
            if health.get("repin"):
                out["pin_lag"] = health["repin"]
                out["then"] = health["repin"]["then"]
    out["build"] = health["summary"]
    out["claim"] = (
        f"{out.get('kind')}: {subject or outcome.get('outcome')}; blocking={str(out.get('blocking', True)).lower()}; "
        f"{out.get('cannot_help', '')}"
        + (f"; owner {out['owner']}" if "owner" in out else "")
        + f"; {health['summary']}; next: {out.get('next')}"
    )
    return out
