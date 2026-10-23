"""`decomp behave --precapture`: fill the oracle capture store for many functions in ONE oracle boot.
A `behave` on a new function spends minutes in `taste repro capture` (the oracle boots the game, plays the movie to the
function's entry and freezes RDRAM, the registers and what the callees did), then 18 s in behave itself. The capture is
per function and stub set, not per candidate, so every candidate check after the first is cached, and the first is the
whole cost. This command asks for the captures of a whole list at once: behave runs once per function without the
oracle (describe mode, `Options.plan`) and notes the stub sets its captures would ask for, then `taste repro
capture-batch` boots the oracle once for all of them and stores each under the key a single capture uses. A later
`behave --function F` finds F's capture cached (until the oracle host, ROM or movie changes: the store key holds
them). A function the plan or the oracle could not serve is listed with the finding and falls back to the live capture.
The list is `open-nonmatching` (every NON_MATCHING body of the project that is a symbol of the version), a file of
names (one per line, `#` comments), or names separated by commas.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, evidence, meter, owes, tools
from abu_decomp.behave import candidate as build_candidate
from abu_decomp.behave import run as behave_run
from abu_decomp.config import Profile

WIDTH = 4  # describe runs at once (the probes queue behind the one repro lock anyway)
CPU_BUDGET = 120.0  # CPU seconds the planning may spend; the oracle boot adds up to ~150 (the per-command cap is 300)
OPEN = "open-nonmatching"
PLAN_SCHEMA = 1  # bump when what the plan run notes changes (behave.run Options.plan)


def names_of(
    profile: Profile, spec: str, project: Path | None, version: str | None = None, host: Path | None = None
) -> list[str]:
    """The functions a --precapture value names. `open-nonmatching` keeps only the NON_MATCHING bodies that are
    symbols of `version` with an address (the scan also meets macro names and other versions' functions)."""
    if spec == OPEN:
        root = tools.project_dir(profile, project)
        found = owes.functions(root, owes.settings(profile))
        names = sorted(name for name, where in found.items() if where["kind"] == evidence.NM)
        if version is None:
            return names
        table = build_candidate.symbol_table(profile, version, project, host)
        return [n for n in names if table.get(n) and table[n][0] is not None]
    path = Path(spec)
    if path.is_file():
        words = [line.split("#", 1)[0].strip() for line in path.read_text().splitlines()]
        return [w for w in words if w]
    return [w.strip() for w in spec.split(",") if w.strip()]


def _memo_path(profile: Profile, options: behave_run.Options, name: str, version: str) -> Path:
    """Where the plan of `name` is kept: the stub sets follow from the ROM's code, the version, the behave settings and
    the pinned tools, so the next precapture reads them instead of running behave again. A stale one costs only a
    live capture later (the store key then differs), never a wrong answer."""
    config = behave_run.configuration(profile)
    key = json.dumps(
        [
            PLAN_SCHEMA,
            version,
            name,
            options.depth,
            options.cases or config["cases"],
            options.seed,
            config["depth"],
            sorted((c, p.commit) for c, p in tools.pins().items()),
        ]
    )
    return behave_run.work_dir(config) / "precapture" / f"{hashlib.sha256(key.encode()).hexdigest()[:24]}.json"


def plan_one(profile: Profile, options: behave_run.Options, name: str) -> dict[str, Any]:
    """The stub sets behave would ask the oracle for on `name`, or why it cannot be planned."""
    memo = _memo_path(profile, options, name, options.version or "")
    try:
        row = json.loads(memo.read_text())
        return {**row, "memo": True}
    except (OSError, ValueError):
        pass
    stubs: list[tuple[int, list[str]]] = []
    failure = ""
    try:
        _, _, findings = behave_run.run(profile, replace(options, function=name, plan=stubs))
    except Exception as exc:
        findings, failure = [], f"{type(exc).__name__}: {str(exc)[:200]}"
    if not stubs:
        keys = ", ".join(sorted({str(f.get("key")) for f in findings})) or "none"
        return {"function": name, "status": "unplanned", "message": failure or f"behave reached no capture ({keys})"}
    row = {"function": name, "status": "planned", "sets": stubs, **({"failure": failure} if failure else {})}
    memo.parent.mkdir(parents=True, exist_ok=True)
    memo.write_text(json.dumps(row))
    return row


def run(
    profile: Profile, options: behave_run.Options, spec: str, cpu_budget: float = CPU_BUDGET
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    started, cpu_began = time.monotonic(), meter.cpu()
    version = options.version or behave_run._project_version(tools.project_dir(profile, options.project))
    if not version:
        raise DecompError("behave.version", "pass --version: the project config names no names_from version")
    names = names_of(profile, spec, options.project, version, options.host)
    if not names:
        raise DecompError("behave.precapture", f"--precapture {spec}: no functions")
    base = replace(options, candidate=None, plan=None, version=version)

    def plan(name: str) -> dict[str, Any]:
        if meter.cpu() - cpu_began > cpu_budget:  # the next run continues: finished describe runs are cached
            return {"function": name, "status": "deferred", "message": f"the planning spent {cpu_budget:.0f} CPU s"}
        return plan_one(profile, base, name)

    with ThreadPoolExecutor(max_workers=WIDTH) as pool:
        planned = list(pool.map(plan, names))
    mark = time.monotonic()
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    config = behave_run.configuration(profile)
    findings: list[dict[str, Any]] = []
    for row in planned:
        # the movie the coverage table says reaches it, as a live `behave` would ask for: the same capture key
        own, no_movie = (
            behave_run.capture_movie(profile, config, row["function"], version) if row.get("sets") else (None, None)
        )
        if no_movie:
            findings.append({**no_movie, "message": f"{row['function']}: {no_movie['message']}"})
            continue
        for _, stubs in row.get("sets", []):
            if (row["function"], tuple(stubs)) not in seen:
                seen.add((row["function"], tuple(stubs)))
                rows.append(
                    {
                        "function": row["function"],
                        "stubs": stubs,
                        **({"env": own["env"], "seconds": own["seconds"]} if own else {}),
                    }
                )
    captured: list[dict[str, Any]] = []
    if rows:
        with tempfile.TemporaryDirectory(
            prefix="precapture-", dir=behave_run.work_dir(behave_run.configuration(profile))
        ) as tmp:
            plan = Path(tmp) / "plan.json"
            plan.write_text(json.dumps(rows))
            called = tools.invoke(
                "taste",
                profile,
                ["repro", "capture-batch", "--version", version, "--hit", str(options.hit), "--plan", str(plan)],
            )
        if not called.ok:
            why = "; ".join(str(f.get("message") or f) for f in called.document.get("findings", [])) or str(
                called.returncode
            )
            raise DecompError("behave.precapture", f"taste repro capture-batch failed: {why}")
        captured = called.document["result"]["captures"]
        oracle = {k: called.document["result"].get(k) for k in ("oracle_runs", "counts", "wall_seconds")}
    else:
        oracle = {"oracle_runs": 0, "counts": {}, "wall_seconds": 0.0}
    for row in captured:
        if row["status"] != "captured":
            findings.append(
                {
                    "key": "behave." + row["status"],
                    "blocking": False,
                    "message": f"{row['function']}: {row.get('message', row['status'])}",
                }
            )
    for row in planned:
        if row["status"] == "unplanned":
            findings.append(
                {"key": "behave.unplanned", "blocking": False, "message": f"{row['function']}: {row['message']}"}
            )
    deferred = [r["function"] for r in planned if r["status"] == "deferred"]
    if deferred:
        findings.append(
            {
                "key": "behave.deferred",
                "blocking": False,
                "message": f"{len(deferred)} functions are not planned yet (the CPU budget of {cpu_budget:.0f} s was "
                "spent); run the same command again",
            }
        )
    result = {
        "mode": "precapture",
        "list": spec,
        "version": version,
        "functions": len(names),
        "planned": sum(r["status"] == "planned" for r in planned),
        "captures": len(rows),
        "newly_captured": sum(r["status"] == "captured" and not r.get("cached") for r in captured),
        "already_cached": sum(r["status"] == "captured" and bool(r.get("cached")) for r in captured),
        "oracle": oracle,
        "deferred": deferred,
        "unplanned": [r for r in planned if r["status"] == "unplanned"],
        "not_captured": [r for r in captured if r["status"] != "captured"],
        "cpu_budget": cpu_budget,
        "timing": {
            "cpu_seconds": round(meter.cpu() - cpu_began, 1),
            "plan_seconds": round(mark - started, 3),
            "capture_seconds": round(time.monotonic() - mark, 3),
            "wall_seconds": round(time.monotonic() - started, 3),
        },
    }
    return result, 0, findings
