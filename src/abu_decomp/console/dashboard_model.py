"""Pure console rows derived from build receipts, gate output and mission evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .feed import canonical_kind, format_event

_ADDRESS = re.compile(r"(?<![0-9a-fA-F])(?:0x)?(80[0-9a-fA-F]{6})(?![0-9a-fA-F])")
_SOURCE = re.compile(r"(?:src/)?[A-Z_a-z][\w/.-]*\.(?:CPP|cpp|c|H|h|s)(?::\d+(?:-\d+)?)?")
_RUN = re.compile(r"\b\d{8}T\d{12}Z\b")


@dataclass(frozen=True)
class Row:
    key: str
    title: str
    details: str
    state: str = "event"
    open: bool = False
    when: float | None = None  # epoch seconds of the entry; the console shows its age


@dataclass
class Dashboard:
    heading: str
    summary: str
    rows: list[Row] = field(default_factory=list)
    focus_key: str | None = None
    footer: str = ""


def ago(seconds: float) -> str:
    """The compact age of an entry: 12s, 4m, 2h, 3d."""
    seconds = max(0, int(seconds))
    for limit, unit, size in ((60, "s", 1), (3600, "m", 60), (86400, "h", 3600)):
        if seconds < limit:
            return f"{seconds // size}{unit}"
    return f"{seconds // 86400}d"


def event_epoch(event: dict) -> float | None:
    try:
        return datetime.fromisoformat(str(event.get("time"))).timestamp()
    except ValueError:
        return None


def addresses(text: str) -> list[str]:
    return ["0x" + match.upper() for match in _ADDRESS.findall(text)]


def event_addresses(event: dict) -> frozenset[str]:
    """Addresses an event mentions; computed once per event and remembered on it when the event allows."""
    found = getattr(event, "addresses", None)
    if found is None:
        found = frozenset(addresses(str(event.get("message", "")) + " " + str(event.get("payload", {}))))
    return found


class Event(dict):
    """An event record that parses its addresses once, when it is read."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.addresses = event_addresses(dict(self))


class EventLog(list):
    """Ordered events with an owner address -> events map, built once per log instead of scanned per lookup."""

    @property
    def by_owner(self) -> dict[str, list[dict]]:
        if "_by_owner" not in self.__dict__:
            index: dict[str, list[dict]] = {}
            for event in self:
                for owner in event_addresses(event):
                    index.setdefault(owner, []).append(event)
            self._by_owner = index
        return self._by_owner


def stop_label(reason: str) -> str:
    match = re.search(r"(?:original\s+)?([^:]*?)\s+owner\s+(0x[0-9a-fA-F]+)", reason)
    if match:
        return f"{match[2]} {match[1].strip().removeprefix('original ')}"
    return reason.rsplit(": ", 1)[-1].split(". First")[0] or "not recorded"


def _evidence(events: list[dict] | EventLog, owner: str | None, revision: str, actual: str) -> str:
    lines = ["Build commit: " + revision, "Observed: " + (actual or "not observed in this attempt")]
    log = events if isinstance(events, EventLog) else EventLog(events)
    related = log.by_owner.get(owner, []) if owner else []
    probes, sources, commits = [], [], []
    for event in related:
        payload = event.get("payload") or {}
        message = str(event.get("message", ""))
        run = payload.get("run_id")
        if run:
            probes.append(f"{run}: {payload.get('checks', '?')} checks; {payload.get('differences', '?')} differences")
        else:
            probes.extend(_RUN.findall(message))
        source = payload.get("decomp_path")
        if source and source != "unknown":
            sources.append(f"{source} [{payload.get('match_state', 'unknown')}]")
        else:
            state = "stand-in" if "NON_MATCHING" in message and "outside NON_MATCHING" not in message else "unknown"
            sources.extend(f"{source} [{state}]" for source in _SOURCE.findall(message))
        if canonical_kind(event) == "LAND":
            commits.append(message)
    lines.append("Probe: " + ("; ".join(dict.fromkeys(probes)) or "not recorded"))
    lines.append("Decomp: " + ("; ".join(dict.fromkeys(sources)) or "not recorded [unknown]"))
    if commits:
        lines.append("Commit evidence: " + "; ".join(commits[-2:]))
    for event in related[-3:]:
        lines.extend(format_event(event))
    return "\n".join(lines)


def _verdict(step: dict | None) -> str:
    if not step:
        return "pending"
    code = step.get("returncode")
    if code is None:
        return "running"
    return "PASS" if code in step.get("expected_exit_codes", [0]) else f"FAIL ({code})"


def boot_model(
    profile: dict,
    status: dict,
    receipt: dict,
    gate_log: str,
    runtime: dict,
    events: list[dict],
    *,
    test_log: str = "",
    plan: str = "",
    plan_path: str = "",
    plan_after: str | list[str] = "",
    plan_before: str = "",
    release: dict | None = None,
    raw_log: str = "",
    build_step: str = "build",
    test_step: str = "ctest",
) -> Dashboard:
    events = events if isinstance(events, EventLog) else EventLog(events)
    commands = profile.get("build", {}).get("commands", [])
    expected = [str(value) for command in commands for value in command.get("expect_all", [])]
    sources = status.get("sources") or receipt.get("sources") or {}
    revisions = [f"{Path(item.get('repo', key)).name} {item.get('commit', '?')[:7]}" for key, item in sources.items()]
    revision = ", ".join(revisions) or "revision unknown"
    steps = {step["name"]: step for step in receipt.get("steps", [])}
    tests = re.search(r"(\d+)% tests passed, (\d+) tests failed out of (\d+)", test_log)
    test_summary = f"{int(tests[3]) - int(tests[2])}/{tests[3]}" if tests else _verdict(steps.get(test_step))
    gate_count = sum(needle in gate_log for needle in expected)
    stamp = str(status.get("started_at") or receipt.get("started_at") or "time unknown")
    phase = status.get("phase", receipt.get("status", "unknown"))
    heading = (
        f"{revision} · {stamp[11:19]} UTC · build {_verdict(steps.get(build_step))} · "
        f"tests {test_summary} · gate {gate_count}/{len(expected)} · {phase}"
    )
    failures = [line for line in gate_log.splitlines() if "ERROR:" in line or "has no native implementation" in line]
    reason = failures[-1] if failures else runtime.get("failure_reason", "")
    stop_addresses = addresses(reason)
    stop_owner = stop_addresses[0] if stop_addresses else None
    rows, passed, stop_index = [], 0, None
    actual_lines = gate_log.splitlines()
    for index, needle in enumerate(expected):
        owner = next(iter(addresses(needle)), None)
        actual = next((line for line in actual_lines if needle in line), "")
        is_stop = bool(
            reason
            and (
                needle in reason
                or (owner == stop_owner and ("has no native implementation" in needle or "ERROR:" in needle))
            )
        )
        state = "stop" if is_stop else "passed" if actual else "planned"
        if is_stop:
            stop_index = index
        elif state == "passed":
            passed += 1
        label = stop_label(reason) if is_stop else needle
        if owner and not is_stop:
            # Address casing in evidence is not significant.
            prefix = re.split(re.escape(owner), needle, maxsplit=1, flags=re.I)[0]
            label = owner + " " + re.sub(r"\bowner\b", "", prefix).strip()
        rows.append(Row(f"gate-{index}", label, _evidence(events, owner, revision, actual), state))
    if reason and stop_index is None:
        rows.append(Row("current-stop", stop_label(reason), _evidence(events, stop_owner, revision, reason), "stop"))
    seen = {owner for needle in expected for owner in addresses(needle)}
    markers = [plan_after] if isinstance(plan_after, str) else plan_after
    planned_text = next((plan.partition(marker)[2] for marker in markers if marker and marker in plan), plan)
    if plan_before:
        planned_text = planned_text.partition(plan_before)[0]
    for owner in addresses(planned_text):
        if owner not in seen:
            seen.add(owner)
            rows.append(
                Row(
                    "plan-" + owner,
                    owner + " planned owner",
                    f"Planned, not executed.\nSource: {plan_path}\n{planned_text.strip()}",
                    "planned",
                )
            )
    release_revision = (release or {}).get("revision", "unknown")
    summary = (
        f"Boot step {stop_index + 1 if stop_index is not None else passed}/{len(expected)} · "
        f"{passed} passed · STOP {stop_label(reason)} · release {release_revision}"
    )
    if not reason:
        summary = f"Boot {passed}/{len(expected)} observed · release {release_revision}"
    rows.append(Row("raw-log", "Raw supervisor log", raw_log or "No supervisor output recorded.", "raw"))
    focus = f"gate-{stop_index}" if stop_index is not None else "current-stop" if reason else None
    return Dashboard(heading, summary, rows, focus)


def mission_model(
    events: list[dict],
    *,
    title: str,
    frames: str,
    graphics: str,
    run_id: str,
    raw_log: str,
) -> Dashboard:
    probes = [event for event in events if event.get("kind") == "PROBE"]
    question = (
        ((probes[-1].get("payload") or {}).get("question") or probes[-1].get("message")) if probes else "none yet"
    )
    rows = [Row("raw-log", "Raw host log", raw_log or "No host output recorded.", "raw")]
    for event in events:
        payload = event.get("payload") or {}
        message = str(payload.get("question") or event.get("message", ""))
        kind = event.get("kind", event.get("actor", "event"))
        label = f"{kind} · {message}"
        key = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
        rows.append(
            Row(
                key,
                label,
                "\n".join(format_event(event)),
                "stop" if event.get("status") in {"failed", "blocked"} else "event",
                when=event_epoch(event),
            )
        )
    return Dashboard(
        f"{title} · run {run_id} · frames {frames} · graphics {graphics}",
        "Last probe: " + str(question),
        rows,
        rows[-1].key,
    )
