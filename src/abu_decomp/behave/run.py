"""`decomp behave`: orchestrate one behaviour comparison (or description) through the pinned tools."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, narrate
from abu_decomp.behave import abi, compare, describe, inputs, prototype
from abu_decomp.behave import candidate as build_candidate
from abu_decomp.config import Profile
from abu_decomp.tools import drain_cache, invoke, settings

DEFAULTS: dict[str, Any] = {
    "cases": 48,
    "depth": 1,
    "candidate_base": 0x80800000,
    "arena_base": 0x80A00000,
    "region_bytes": 0x400,
    "timeout_seconds": 2,
    "event_budget": 20000,  # call/stub/store events one case may write; a case over it ends fatal event-budget
    "event_budget_cap": 300000,  # the most the budget is raised to for a function that needs more
    "depth_cap": 3,  # calls deep the original callees may run when no case ran to completion with fewer
    "learn_cases": 8,
    "learn_rounds": 8,
    "headers": ["include/**/*.h", "include/**/*.H", "src/**/*.c", "src/**/*.C", "src/**/*.CPP", "src/**/*.cpp"],
}


PROBE_KEYS = ("closure", "stubs", "spilled", "statuses", "wall_seconds", "cached", "event_budget", "events_max")


class BehaveError(DecompError):
    pass


@dataclass
class Options:
    function: str
    candidate: Path | None
    version: str | None
    cases: int | None
    seed: int
    project: Path | None
    host: Path | None
    depth: int | None = None
    cache: bool = True
    lenient: bool = False  # unmapped reads answer zero and calls outside the closure are stubs (set when no case ran)
    deepened: bool = False  # depth was raised by this command (not given), so it may be raised again


def configuration(profile: Profile) -> dict[str, Any]:
    """The optional `behave` profile section over the defaults; unknown keys are refused."""
    try:
        given = profile.section("behave")
    except DecompError:
        given = {}
    extra = set(given) - set(DEFAULTS) - {"work"}
    if extra:
        raise BehaveError("config.schema", f"{profile.source_file}:behave: unknown keys: {', '.join(sorted(extra))}")
    return {**DEFAULTS, **given}


def work_dir(config: dict[str, Any]) -> Path:
    base = config.get("work") or os.path.join(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache", "abu-decomp")
    path = Path(base).expanduser() / "behave"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _project_version(project: Path) -> str | None:
    try:
        return tomllib.loads((project / "config.toml").read_text()).get("names_from")
    except (OSError, tomllib.TOMLDecodeError):
        return None


def probe(profile: Profile, work: Path, name: str, request: dict[str, Any], function: str) -> dict[str, Any]:
    """One `taste repro probe --kind cases` call; the record plus the per-case results it froze."""
    request["story"]["narrate"] = False  # decomp writes the one verdict event itself (narrate.behave_event)
    path = work / f"{name}.json"
    path.write_text(json.dumps(request, sort_keys=True))
    called = invoke(
        "taste",
        profile,
        [
            "repro",
            "probe",
            "--kind",
            "cases",
            "--function",
            function,
            "--input",
            str(path),
            "--question",
            request["story"]["question"],
        ],
    )
    if not called.ok:
        findings = called.document.get("findings") or [{"message": f"taste exited {called.returncode}"}]
        raise BehaveError("behave.probe", "; ".join(str(f.get("message") or f) for f in findings))
    record = called.document["result"]["probe"]
    results = json.loads(Path(record["cases_path"]).read_text())
    return {"record": record, "cases": results}


def _request(
    *,
    version: str,
    address: int,
    cases: list[dict[str, Any]],
    config: dict[str, Any],
    depth: int,
    memory_end: int,
    question: str,
    candidate: dict[str, Any] | None = None,
    budget: int | None = None,
    lenient: bool = False,
) -> dict[str, Any]:
    rows = [{k: v for k, v in case.items() if k != "labels"} for case in cases]
    request: dict[str, Any] = {
        "version": version,
        "function": f"0x{address:08X}",
        "depth": depth,
        "cases": rows,
        "memory_end": memory_end,
        "timeout_seconds": config["timeout_seconds"],
        "event_budget": budget or config["event_budget"],
        **({"lenient": True} if lenient else {}),
        "story": {"question": question},
    }
    if candidate:
        request["candidate"] = candidate
    return request


out_of_budget = compare.out_of_budget


def _stack(record: dict[str, Any]) -> tuple[int, int]:
    """The frame range excluded from the memory effect: the stack window below the entry sp and the home slots."""
    sp = int(record["sp"], 0) if isinstance(record["sp"], str) else record["sp"]
    return sp - int(record["stack_window"]), sp + 0x10


def _name_index(symbols: dict[str, list[Any]]) -> list[tuple[int, str]]:
    return sorted((row[0], name) for name, row in symbols.items() if row[0] is not None)


def timing(stages: dict[str, float], started: float, cache: dict[str, Any]) -> dict[str, float]:
    """The stage seconds, the total, and the unbake cache lookups this command made: misses with no hits is a cold
    cache (the cost of a score or a build context is then the whole build, not a lookup)."""
    return {
        **stages,
        "total": round(time.monotonic() - started, 3),
        "cache_hits": cache["hits"],
        "cache_misses": cache["misses"],
    }


def run(profile: Profile, options: Options) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    started = time.monotonic()
    drain_cache()  # the lookups of earlier commands in this process are not this one's
    stages: dict[str, float] = {}
    findings: list[dict[str, Any]] = []

    def lap(name: str, since: float) -> float:
        stages[name] = round(time.monotonic() - since, 3)
        return time.monotonic()

    waited = 0.0
    config = configuration(profile)
    budget = config["event_budget"]
    hopeless = False  # a case that did not finish at the cap: later probes do not measure it again

    def ask(name: str, request: dict[str, Any]) -> dict[str, Any]:
        """One probe; the seconds it queued behind another isolated repro are added to the timing. A case that
        runs out of its event budget is measured alone at the cap: when it finishes there, the budget is raised
        to twice what it wrote (for this and every later probe) and the probe repeats; when it still does not,
        it never returns in any budget we would record, and stays fatal."""
        nonlocal waited, budget, hopeless
        request = dict(request, event_budget=budget, **({"lenient": True} if options.lenient else {}))
        done = probe(profile, work, name, request, options.function)
        waited += done["record"].get("lock_wait_seconds", 0.0)
        for _ in range(2):
            spent = [i for i, c in enumerate(done["cases"]) if out_of_budget(c)]
            if not spent or hopeless or budget >= config["event_budget_cap"]:
                break
            pilot = dict(request, cases=[request["cases"][spent[0]]], event_budget=config["event_budget_cap"])
            alone = probe(profile, work, name + "-measure", pilot, options.function)
            waited += alone["record"].get("lock_wait_seconds", 0.0)
            if out_of_budget(alone["cases"][0]):
                hopeless = True
                break
            budget = min(config["event_budget_cap"], max(budget, 2 * alone["cases"][0].get("events_written", budget)))
            request = dict(request, event_budget=budget)
            done = probe(profile, work, name, request, options.function)
            waited += done["record"].get("lock_wait_seconds", 0.0)
        stages["lock_wait"] = round(waited, 3)
        return done

    chosen = settings(profile, options.project, options.host)
    version = options.version or _project_version(chosen.project)
    if not version:
        raise BehaveError("behave.version", "pass --version: the project config names no names_from version")
    count = options.cases or config["cases"]
    depth = options.depth if options.depth is not None else config["depth"]
    mark = time.monotonic()
    symbols = build_candidate.symbol_table(profile, version, options.project, options.host)
    fact = symbols.get(options.function)
    if fact is None or fact[0] is None:
        raise BehaveError("behave.function", f"{options.function} is not a symbol of version {version}")
    address = fact[0]
    mark = lap("symbols", mark)
    candidate_file = options.candidate.resolve() if options.candidate else None
    proto = prototype.find(chosen.project, options.function, tuple(config["headers"]), candidate_file)
    if proto is None:
        proto = prototype.inferred(options.function)
        findings.append(
            {
                "key": "behave.prototype",
                "blocking": False,
                "message": f"no declaration of {options.function} found; four unknown argument words assumed",
            }
        )
    elif proto.blind:
        findings.append(
            {
                "key": "behave.prototype",
                "blocking": False,
                "message": "the prototype has unknown or by-value types; the return value and those "
                "arguments are compared as notes only",
            }
        )
    mark = lap("prototype", mark)
    layout = inputs.Layout(config["arena_base"], 0x1000, config["region_bytes"])
    work = work_dir(config)
    question = f"Behaviour of {options.function} over {count} input combinations"

    # 1. short probes of the original show which offsets of each pointed-to region it touches, and which
    #    fields it dereferences (those get a region of their own); repeated while it finds new ones
    learned = inputs.Learned()
    for _ in range(config["learn_rounds"]):
        first, regions = inputs.seeded_cases(
            proto, min(config["learn_cases"], count), options.seed, layout, learned, learn=True
        )
        result = ask(
            "learn",
            _request(
                version=version,
                address=address,
                cases=first,
                config=config,
                depth=depth,
                memory_end=layout.reach(regions),
                question=question + " (reads)",
            ),
        )
        stack = _stack(result["record"])
        hot = describe.learn(result["cases"], regions)
        new, answers = describe.pointer_fields(result["cases"], inputs.Tags.of(learned), learned)
        seen, bss = describe.read_globals(result["cases"], regions, stack)
        cut = [name for name, why in result["record"].get("stubs", {}).items() if why == "boundary"]
        before = learned
        learned = inputs.Learned(
            tuple((i, tuple(v)) for i, v in sorted(hot.items())),
            (*learned.pointers, *new),
            tuple(dict.fromkeys((*learned.globals, *seen)))[:16],
            tuple(dict.fromkeys((*learned.state, *bss)))[:8],
            tuple(dict.fromkeys((*learned.stubs, *sorted(cut))))[:16],
            (*learned.returns, *answers),
        )
        if learned == before:
            break
    mark = lap("learn", mark)
    cases, regions = inputs.seeded_cases(proto, count, options.seed, layout, learned)
    memory_end = layout.reach(regions)
    image = None
    if candidate_file:
        try:
            image = build_candidate.build(
                profile,
                version=version,
                function=options.function,
                source=candidate_file,
                work=work / f"cand-{hashlib.sha256(candidate_file.read_bytes()).hexdigest()[:16]}-{options.function}",
                base=config["candidate_base"],
                project=options.project,
                host=options.host,
            )
        except build_candidate.CandidateError as exc:
            return _refused(profile, options, version, address, proto, exc, stages, started, findings)
        memory_end = max(memory_end, config["candidate_base"] + image.size + 0x1000)
    mark = lap("candidate", mark)
    original = ask(
        "original",
        _request(
            version=version,
            address=address,
            cases=cases,
            config=config,
            depth=depth,
            memory_end=memory_end,
            question=question,
        ),
    )
    mark = lap("original", mark)
    record = original["record"]
    cut = [name for name, why in record.get("stubs", {}).items() if why == "boundary"]
    if (
        not options.lenient
        and not any(c["status"] == "ok" for c in original["cases"])
        and any(c["status"] == "unmapped" or "indirect-call" in c["detail"] for c in original["cases"])
    ):
        # no case got through: the input memory lacks state the program built at run time (null pointers and
        # function pointers the original dereferences). Both bodies run again over a memory where unmapped reads
        # answer zero, so what each does with the same garbage is still compared.
        again, code, more = run(profile, replace(options, lenient=True))
        more.append(
            {
                "key": "behave.lenient",
                "blocking": False,
                "message": "no case ran with strict memory; compared over a lenient memory (unmapped reads zero)",
            }
        )
        if again.get("probe") is not None:
            again["probe"]["lenient"] = True
        return again, code, [*findings, *more]
    if (
        not any(c["status"] == "ok" or out_of_budget(c) for c in original["cases"])  # a loop cut by the budget ran
        and cut
        and depth < config["depth_cap"]
        and (options.depth is None or options.deepened)
    ):
        # callees cut off at the boundary answer zero and starve loops and pointer reads: let the next level run
        deeper, code, more = run(profile, replace(options, depth=depth + 1, deepened=True))
        more.append(
            {
                "key": "behave.depth",
                "blocking": False,
                "message": f"no case completed at depth {depth} ({len(cut)} callees stubbed); ran at depth {depth + 1}",
            }
        )
        return deeper, code, [*findings, *more]
    stack = _stack(record)
    common = {
        "function": options.function,
        "version": version,
        "address": f"0x{address:08X}",
        "prototype": proto.describe(),
        "cases": len(cases),
        "seed": options.seed,
        "depth": depth,
        "probe": {k: record[k] for k in PROBE_KEYS if k in record},
    }
    cache = drain_cache()
    if image is None:
        names = _name_index(symbols)
        result = {
            "mode": "describe",
            "verdict": "described",
            **common,
            **describe.describe(original["cases"], regions, proto, names, stack, learned.pointers, learned.returns),
            "timing": timing(stages, started, cache),
            "cache": cache,
        }
        if not result["testable_cases"]:
            reasons = {}
            for case in original["cases"]:
                key = compare.reason_key(f"original {case['status']}: {case['detail']}")
                reasons[key] = reasons.get(key, 0) + 1
            top = max(reasons, key=reasons.get) if reasons else "no cases"
            result.update(verdict=compare.UNTESTABLE, reason=f"no case ran to completion; most common: {top}")
            findings.append({"key": "behave.untestable", "blocking": False, "message": result["reason"]})
        return result, 0, findings
    try:
        changed = ask(
            "candidate",
            _request(
                version=version,
                address=address,
                cases=cases,
                config=config,
                depth=depth,
                memory_end=memory_end,
                question=question + " (candidate)",
                candidate=image.spec(options.function),
            ),
        )
    except BehaveError as exc:
        return _refused(profile, options, version, address, proto, exc, stages, started, findings)
    lap("candidate_run", mark)
    callees = {}
    own = (image.vram, image.vram + image.size)
    called = {e["name"] for c in original["cases"] for e in c["events"] if e["k"] in ("call", "stub")}
    # hand-written callees, and the candidate's own helpers (inlining a callee is not a different call order)
    helpers = {f["name"] for f in image.functions if f["name"] != options.function}
    opaque = frozenset(abi.opaque(chosen.project, version, {n.removeprefix(compare.PREFIX) for n in called}) | helpers)
    for name in called:
        callee = prototype.find(chosen.project, name, tuple(config["headers"]), None)
        if callee:
            callees[name] = callee
    rows = [
        compare.compare_case(a, b, proto, callees, stack, own, opaque)
        for a, b in zip(original["cases"], changed["cases"], strict=True)
    ]
    labels = [c["labels"] for c in cases]
    summary = compare.overall(rows, labels)
    notes = sorted({n for row in rows for n in row["notes"]})
    shown = [dict(r, inputs=labels[i]) for i, r in enumerate(rows) if r["verdict"] != compare.SAME][:12]
    result = {
        "mode": "compare",
        **common,
        **summary,
        "candidate": {
            "file": str(candidate_file),
            "object_sha256": image.object_sha256,
            "address": f"0x{image.vram:08X}",
            "functions": [f["name"] for f in image.functions],
            "image_bytes": image.size,
            "closure": changed["record"]["closure"],
        },
        "notes": notes[:20],
        **({"opaque_callees": sorted(opaque)} if opaque else {}),
        "cases_detail": shown,
        "timing": timing(stages, started, cache),
        "cache": cache,
    }
    announce(profile, result)
    return result, 0, findings


def announce(profile: Profile, result: dict[str, Any]) -> None:
    """The verdict as one PROBE event for the stream; never fails the command (no events section, unwritable feed)."""
    first = result.get("first_difference") or {}
    counts = result.get("counts") or {}
    verdict = {"same": "same", "differs": "differs"}.get(result["verdict"], "error")
    try:
        narrate.behave_event(
            profile,
            function=result["function"],
            verdict=verdict,
            checks=counts.get("same", 0) + counts.get("differs", 0),
            differences=counts.get("differs", 0),
            inputs=first.get("inputs"),
            expected=first.get("expected"),
            actual=first.get("got"),
            reason=result.get("reason"),
        )
    except DecompError as exc:
        sys.stderr.write(f"behave: no event written: {exc.detail}\n")


def _refused(
    profile: Profile,
    options: Options,
    version: str,
    address: int,
    proto: prototype.Prototype,
    exc: DecompError,
    stages: dict[str, float],
    started: float,
    findings: list[dict[str, Any]],
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    done = _untestable(options, version, address, proto, str(exc), stages, started, findings, exc.key)
    announce(profile, done[0])
    return done


def _untestable(
    options: Options,
    version: str,
    address: int,
    proto: prototype.Prototype,
    reason: str,
    stages: dict[str, float],
    started: float,
    findings: list[dict[str, Any]],
    key: str,
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    cache = drain_cache()
    result = {
        "mode": "compare",
        "verdict": compare.UNTESTABLE,
        "reason": reason,
        "function": options.function,
        "version": version,
        "address": f"0x{address:08X}",
        "prototype": proto.describe(),
        "timing": timing(stages, started, cache),
        "cache": cache,
    }
    return result, 0, [*findings, {"key": key, "blocking": False, "message": reason}]
