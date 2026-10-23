"""`decomp behave`: orchestrate one behaviour comparison (or description) through the pinned tools."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, arity
from abu_decomp.behave import abi, classify, compare, describe, inputs, movie, prototype
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
    "capture_poll_seconds": 0.5,  # oracle seconds a movie needs per poll up to the function's first call (time limit)
    "learn_cases": 8,
    "learn_rounds": 8,
    "min_compared": compare.MIN_COMPARED,  # cases that must compare before the verdict may be `same`
    "headers": ["include/**/*.h", "include/**/*.H", "src/**/*.c", "src/**/*.C", "src/**/*.CPP", "src/**/*.cpp"],
}


PROBE_KEYS = ("closure", "stubs", "spilled", "statuses", "wall_seconds", "cached", "event_budget", "events_max")


NULL_PAGE = 0x1000  # reads below this are null-pointer dereferences


def _unmapped(case: dict[str, Any], floor: int = 0) -> bool:
    """The case stopped on memory it was not given: a read at or above `floor`, or a call through such a pointer."""
    if "indirect-call" in case["detail"]:
        return True
    if case["status"] != "unmapped":
        return False
    words = case["detail"].split()
    try:
        return int(words[1], 16) >= floor
    except (IndexError, ValueError):
        return True


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
    capture: bool = True  # seed every comparison with the oracle's real entry state (profile repro.capture)
    hit: int = 1  # which entry of the function the capture stops at
    recaptured: bool = False  # the capture was repeated for this probe's stubs (once per run: no loop)
    seeded: dict[str, Any] | None = None  # the capture's seed (see capture_seed): RDRAM image, sp, entry case
    plan: list[tuple[int, list[str]]] | None = (
        None  # precapture: collect the (depth, stubs) every capture would ask for
    )


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
    request["story"]["narrate"] = False  # no Taste narration event: decomp reports the verdict itself
    # one file per process: behave runs in parallel share the work dir, and a name alone let one run's request be
    # replaced by another's between the write and taste reading it
    path = work / f"{name}-{os.getpid()}.json"
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


# capture findings that are the setup's, not the function's: every comparison would run on a wrong or missing oracle
SETUP_FAILURES = ("repro.capture.stale_host", "repro.capture.host_build", "repro.capture.display")


def capture_movie(
    profile: Profile, config: dict[str, Any], name: str, version: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The movie the coverage table says reaches `name` (its host variables and time limit), or the finding that
    none does (behave.no_movie); (None, None) when the table cannot say and the profile's fixed script stands."""
    return movie.choose(
        profile,
        work_dir(config),
        name,
        version,
        float(config.get("capture_poll_seconds", DEFAULTS["capture_poll_seconds"])),
    )


def capture_seed(
    profile: Profile, version: str, function: str, hit: int, stubs: list[str], own: dict[str, Any] | None = None
) -> tuple[dict[str, Any] | None, str, str]:
    """One `taste repro capture`: the oracle run to the function's entry, with what each of `stubs` (the callees the
    probe cuts off) returned and wrote on the way out. The seed (RDRAM image, entry stack pointer and the entry
    registers as one case whose stubs answer as the real ones did) with the capture record that names it, or none with
    the reason and its finding key. A stale host, a failed host build or a missing display fail the command: no
    comparison runs over a host that is not the current oracle."""
    called = invoke(
        "taste",
        profile,
        [
            "repro",
            "capture",
            "--function",
            function,
            "--version",
            version,
            "--hit",
            str(hit),
            *(["--stubs", ",".join(stubs)] if stubs else []),
            *(a for k, v in sorted((own or {}).get("env", {}).items()) for a in ("--env", f"{k}={v}")),
            *(["--seconds", str(own["seconds"])] if own else []),
        ],
    )
    if not called.ok:
        findings = called.document.get("findings") or [{"message": f"taste exited {called.returncode}"}]
        why = "; ".join(str(f.get("message") or f) for f in findings)
        key = next((str(f["key"]) for f in findings if isinstance(f, dict) and f.get("key")), "")
        if key in SETUP_FAILURES:
            raise BehaveError("behave.capture_setup", f"{key}: {why}")
        return None, why, key
    result = called.document["result"]
    return {**result["seed"], "capture": result["capture"]}, "", ""


def _no_seed(why: str, key: str, stubs: bool = False) -> dict[str, Any]:
    """The finding for a comparison that runs without the oracle's entry state: unreached is its own key (what would
    reach the function is in the message), anything else is behave.seed."""
    if key == "repro.capture.unreached":
        return {"key": "behave.unreached", "blocking": False, "message": f"the oracle run never reached it: {why}"}
    what = "no oracle capture for these stubs" if stubs else "no oracle capture to seed memory"
    return {"key": "behave.seed", "blocking": False, "message": f"{what}: {why}"}


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


_HELD: dict[Path, Any] = {}  # candidate locks this process holds (a run re-enters run() for its retries)


@contextmanager
def _exclusive(path: Path) -> Iterator[None]:
    """Hold `path` locked for one candidate's whole run: runs in parallel share the work dir, and a second run of the
    same candidate rebuilt the image dir under the first (half-written objects, a link of another run's object)."""
    if path in _HELD:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _HELD[path] = lock
        try:
            yield
        finally:
            del _HELD[path]


def _candidate_dir(work: Path, candidate: Path, function: str) -> Path:
    return work / f"cand-{hashlib.sha256(candidate.read_bytes()).hexdigest()[:16]}-{function}"


def run(profile: Profile, options: Options) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    if options.candidate is None:
        return _run(profile, options)
    where = _candidate_dir(work_dir(configuration(profile)), options.candidate.resolve(), options.function)
    with _exclusive(where.with_name(where.name + ".lock")):
        return _run(profile, options)


def _run(profile: Profile, options: Options) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
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
        if options.seeded:  # real run-time memory under every generated case, the entry stack where it was
            request = dict(request, seed={"memory": options.seeded["memory"]}, sp=options.seeded["sp"])
        done = probe(profile, work, name, request, target)
        waited += done["record"].get("lock_wait_seconds", 0.0)
        for _ in range(2):
            spent = [i for i, c in enumerate(done["cases"]) if out_of_budget(c)]
            if not spent or hopeless or budget >= config["event_budget_cap"]:
                break
            pilot = dict(request, cases=[request["cases"][spent[0]]], event_budget=config["event_budget_cap"])
            alone = probe(profile, work, name + "-measure", pilot, target)
            waited += alone["record"].get("lock_wait_seconds", 0.0)
            if out_of_budget(alone["cases"][0]):
                hopeless = True
                break
            budget = min(config["event_budget_cap"], max(budget, 2 * alone["cases"][0].get("events_written", budget)))
            request = dict(request, event_budget=budget)
            done = probe(profile, work, name, request, target)
            waited += done["record"].get("lock_wait_seconds", 0.0)
        stages["lock_wait"] = round(waited, 3)
        return done

    # the oracle's table can name a function otherwise than the project does (a rename the oracle has not seen), so the
    # tools are always asked for the address the project's own symbol facts gave
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
    target = f"0x{address:08X}"
    defect = abi.entry_defect(chosen.project, version, options.function)
    if defect:
        raise BehaveError("behave.entry_mid_frame", defect)
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
            learned.finite or any("NaN operand" in str(c.get("detail", "")) for c in result["cases"]),
        )
        if learned == before:
            break
    mark = lap("learn", mark)
    cases, regions = inputs.seeded_cases(proto, count, options.seed, layout, learned)
    if options.seeded:  # the registers the real game entered the function with, over the captured memory, and the
        # same entry with one argument moved off its real value at a time
        entry = options.seeded["case"]
        cases = [*cases, {**entry, "labels": {"seed": entry["name"]}}, *inputs.around(entry, proto, max(1, count // 8))]
    memory_end = layout.reach(regions)
    image = None
    if candidate_file:
        try:
            image = build_candidate.build(
                profile,
                version=version,
                function=options.function,
                source=candidate_file,
                work=_candidate_dir(work, candidate_file, options.function),
                base=config["candidate_base"],
                project=options.project,
                host=options.host,
            )
        except build_candidate.CandidateError as exc:
            return _refused(profile, options, version, address, proto, exc, stages, started, findings)
        if image.kept:
            findings.append(
                {
                    "key": "behave.sibling",
                    "blocking": False,
                    "message": f"{', '.join(image.kept)} run from the candidate, not the original: the file branches "
                    "to them PC-relative, which cannot reach their original address",
                }
            )
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
    # a case the original could not finish for want of memory is not compared. Widening the memory leaves the cases
    # that ran unchanged (they never read what it adds), so it is done when no case ran, or when a case chased a
    # pointer that is not a null one (garbage built from the input, a function pointer the game filled in): a null
    # dereference is a crash on the console and stays untestable with that reason.
    starved = not options.lenient and (
        (not any(c["status"] == "ok" for c in original["cases"]) and any(_unmapped(c) for c in original["cases"]))
        or any(_unmapped(c, floor=NULL_PAGE) for c in original["cases"])
    )
    if options.plan is not None:
        # precapture (behave.precapture): note the stub set the capture below would be asked for and go on without
        # the oracle, so a depth step or a lenient re-run notes its own set the same way
        options.plan.append((depth, sorted(record.get("stubs", {}))))
        options = replace(options, capture=False)
    if options.capture and options.seeded is None:
        # every comparison runs over the state the real game entered the function with: the ROM is run to its entry
        # and both bodies run again over that memory, with its entry registers (and variations around them) as more
        # cases. Only when that is not available do the static image and, for a starved case, the lenient memory below
        # stand in, under a named finding.
        own, no_movie = capture_movie(profile, config, options.function, version)
        seed, why, key = (
            (None, "", "")
            if no_movie  # nothing recorded reaches it: the cases stay synthetic and the time limit is not spent
            else capture_seed(profile, version, target, options.hit, sorted(record.get("stubs", {})), own)
        )
        if no_movie:
            findings.append(no_movie)
            options = replace(options, capture=False)
        elif seed is None:
            findings.append(_no_seed(why, key))
            options = replace(options, capture=False)
        else:
            again, code, more = run(profile, replace(options, seeded=seed))
            shown = seed["capture"]
            more.append(
                {
                    "key": "behave.seed",
                    "blocking": False,
                    "message": f"seeded from the oracle at entry {shown['hit']} of {shown['function']} (rdram "
                    f"{shown['rdram_sha256'][:12]}, rom sha1 {shown['rom_sha1'][:12]})"
                    + (f"; movie {own['movie']} poll {own['poll']}" if own else ""),
                }
            )
            return again, code, [*findings, *more]
    if options.seeded and options.capture and not options.recaptured:
        # the recorded calls hold for the callees that were stubs when the oracle ran (a call inside a stub is not
        # the probe's call): a probe with other stubs (another depth) needs its own capture
        asked = set(options.seeded["capture"].get("stubs", []))
        if set(record.get("stubs", {})) - asked:
            seed, why, key = capture_seed(
                profile,
                version,
                target,
                options.hit,
                sorted(record["stubs"]),
                capture_movie(profile, config, options.function, version)[0],
            )
            if seed is None:
                findings.append(_no_seed(why, key, stubs=True))
                options = replace(options, capture=False)
            else:
                again, code, more = run(profile, replace(options, seeded=seed, recaptured=True))
                return again, code, [*findings, *more]
    if starved:
        # a case did not get through: the input memory lacks state the program built at run time (null pointers,
        # garbage pointers and function pointers the original dereferences), even over the captured memory. Both
        # bodies run again over a memory where unmapped reads answer zero, so what each does with the same
        # garbage is still compared.
        again, code, more = run(profile, replace(options, lenient=True))
        more.append(
            {
                "key": "behave.lenient",
                "blocking": False,
                "message": "a case ran out of mapped memory; compared over a lenient memory (unmapped reads zero)",
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
    seed_used = None
    if options.seeded:
        shown = options.seeded["capture"]
        seed_used = {
            k: shown[k]
            for k in (
                "function",
                "hit",
                "rom_sha1",
                "rdram_sha256",
                "sp",
                "rdram_path",
                "cached",
                "calls",
                "calls_complete",
                "calls_unresolved",
                "calls_dropped_writes",
            )
            if k in shown
        }
    common = {
        "function": options.function,
        "version": version,
        "address": f"0x{address:08X}",
        "prototype": proto.describe(),
        "cases": len(cases),
        "seed": options.seed,
        "depth": depth,
        "probe": {k: record[k] for k in PROBE_KEYS if k in record},
        **({"oracle_seed": seed_used} if seed_used else {}),
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
    summary = compare.overall(rows, labels, int(config["min_compared"]))
    notes = sorted({n for row in rows for n in row["notes"]})
    shown = [dict(r, inputs=labels[i]) for i, r in enumerate(rows) if r["verdict"] != compare.SAME][:12]
    classification = classify.classify(
        summary,
        original["cases"],
        changed["cases"],
        stack,
        own,
        opaque,
        options.function,
        candidate_file,
        profile.source_file,
    )
    disagreement = arity.notes(profile, [options.function], chosen.project)
    if disagreement and classification:
        classification["arity_first"] = disagreement[0]["first"] + f": {disagreement[0]['next']}"
    result = {
        "mode": "compare",
        **common,
        **summary,
        "candidate": {
            "file": str(candidate_file),
            "object_sha256": image.object_sha256,
            "function_sha256": _function_sha256(image, options.function),
            "address": f"0x{image.vram:08X}",
            "functions": [f["name"] for f in image.functions],
            "image_bytes": image.size,
            "closure": changed["record"]["closure"],
        },
        "notes": notes[:20],
        **({"classification": classification} if classification else {}),
        **({"arity": disagreement} if disagreement else {}),
        **({"opaque_callees": sorted(opaque)} if opaque else {}),
        "cases_detail": shown,
        "timing": timing(stages, started, cache),
        "cache": cache,
    }
    return result, 0, findings


def _function_sha256(image: Any, function: str) -> str | None:
    """Digest of the linked bytes of the function under test alone: equal digests mean a byte-identical body."""
    row = next((f for f in image.functions if f["name"] == function), None)
    if row is None:
        return None
    try:
        blob = image.path.read_bytes()[row["offset"] : row["offset"] + row["size"]]
    except OSError:
        return None
    return hashlib.sha256(blob).hexdigest()


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
