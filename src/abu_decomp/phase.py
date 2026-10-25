"""`decomp phase`: every phase gate (map, foundations, port/oracle, coverage, release ladder) from real measurements.

Generic: all project facts come from the seat profile section `phase` (CONTRACTS.md, module `phase`); a second game is a
second entry of `phase.games`, not new code. Read-only except `--refresh`, which writes `phase.out` and appends to
`phase.history`. Gate row: {phase, game, version, key, value, threshold, met, source, age_s}.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, nextcmd, tas, tas_coverage
from abu_decomp.config import Profile, load_profile

STALE_S = 6 * 3600
REFRESH_S = 3600
CATEGORIES = ("rng", "timing", "input", "audio", "gfx")
P3_DEFAULT = {"fn_reachable": 100, "line": 95, "branch": 85, "oracle_only": 0, "differs_missing": 0, "crash": 0}


def settings(profile: Profile) -> dict[str, Any]:
    cfg = profile.section("phase")
    for key in ("games", "focus", "ladder", "named_rule", "out"):
        if key not in cfg:
            raise DecompError("phase.config", f"phase.{key} is required")
    if cfg["focus"].get("game") not in cfg["games"]:
        raise DecompError("phase.config", "phase.focus.game is not in phase.games")
    return cfg


def row(
    phase: str,
    game: str,
    version: str,
    key: str,
    value: Any,
    threshold: Any,
    met: bool,
    source: str,
    age: float | None = None,
) -> dict[str, Any]:
    return {
        "phase": phase,
        "game": game,
        "version": version,
        "key": key,
        "value": value,
        "threshold": threshold,
        "met": bool(met),
        "source": source,
        "age_s": None if age is None else round(age),
    }


def _pct(part: float, whole: float) -> float:
    return round(100.0 * part / whole, 2) if whole else 0.0


def _show(tree: Path, ref: str, path: str) -> str:
    done = subprocess.run(["git", "-C", str(tree), "show", f"{ref}:{path}"], capture_output=True, text=True)
    if done.returncode:
        raise DecompError("phase.git", f"{ref}:{path}: {done.stderr.strip()[-200:]}")
    return done.stdout


def _age(tree: Path, ref: str) -> float | None:
    done = subprocess.run(["git", "-C", str(tree), "log", "-1", "--format=%ct", ref], capture_output=True, text=True)
    return time.time() - int(done.stdout) if done.returncode == 0 and done.stdout.strip() else None


def mapped_bytes(data: dict[str, Any]) -> tuple[int, int, int]:
    """(bytes mapped to a row, rom_size, fn bytes with C). A fn row counts as mapped only with a C file; spans and
    blobs are merged as intervals so overlapping rows count once."""
    spans: list[tuple[int, int]] = []
    fn_bytes = c_bytes = 0
    for fn in data["fns"]:
        fn_bytes += fn[2]
        if fn[4]:
            c_bytes += fn[2]
            spans.append((fn[1], fn[1] + fn[2]))
    for group in data.get("spans", {}).values():
        spans += [(a, b) for a, b in group]
    spans += [(b[1], b[1] + b[2]) for b in data.get("blobs", [])]
    size, total, end = int(data["rom_size"]), 0, 0
    for start, stop in sorted(spans):
        start, stop = max(start, end), min(stop, size)
        if stop > start:
            total += stop - start
            end = stop
    return total, size, c_bytes


def body(source: str, name: str) -> str | None:
    """The text of `name`'s definition after its own name, up to the closing brace at column 0."""
    lines = source.splitlines()
    head = re.compile(rf"^[^\s#/][^;]*\b{re.escape(name)}\s*\(")
    for i, text in enumerate(lines):
        match = head.match(text)
        if not match:
            continue
        out = [text[match.end() :]]
        for later in lines[i + 1 :]:
            out.append(later)
            if later.startswith("}"):
                break
        return "\n".join(out)
    return None


def foundations(
    tree: Path, ref: str, tsv: str, scores: dict[str, tuple[float, str | None]], rule: str
) -> dict[str, Any]:
    """Per category: bytes, exact % B (map score 100: unbake scores a FAKEMATCH build below it), named % B (rule finds
    nothing in the body)."""
    named, cache = re.compile(rule), {}
    cats: dict[str, dict[str, Any]] = {}
    for text in tsv.splitlines():
        if not text.strip() or text.startswith("#") or text.startswith("category\t"):
            continue
        cat, name, size, *_ = text.split("\t")
        score, file = scores.get(name, (0.0, None))
        src = None
        if file:
            if file not in cache:
                try:
                    cache[file] = _show(tree, ref, file)
                except DecompError:
                    cache[file] = ""
            src = body(cache[file], name)
        exact = score == 100.0
        clean = src is not None and not named.search(src)
        entry = cats.setdefault(
            cat, {"fns": 0, "bytes": 0, "exact_bytes": 0, "named_bytes": 0, "clean_bytes": 0, "failing": []}
        )
        entry["fns"] += 1
        entry["bytes"] += int(size)
        entry["exact_bytes"] += int(size) * exact
        entry["named_bytes"] += int(size) * clean
        entry["clean_bytes"] += int(size) * (exact and clean)
        if not (exact and clean):
            entry["failing"].append({"function": name, "bytes": int(size), "exact": exact, "named": clean})
    return cats


def p1(profile: Profile, cfg: dict[str, Any], game: str, version: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    spec = cfg["games"][game]
    tree, ref = profile.path("phase.tree", spec["tree"]), spec["ref"]
    mp = spec["map"].format(version=version)
    data = json.loads(_show(tree, ref, mp))
    age = _age(tree, ref)
    source = f"{ref}:{mp}"
    total, size, c_bytes = mapped_bytes(data)
    fn_total = sum(f[2] for f in data["fns"])
    rows = [
        row("p1", game, version, "mapped_pct", _pct(total, size), 100.0, total >= size, source, age),
        row("p1", game, version, "fuzzy_pct", _pct(c_bytes, fn_total), 100.0, c_bytes >= fn_total, source, age),
    ]
    detail: dict[str, Any] = {}
    if spec.get("foundations"):
        tsv_path = profile.resolve_path(spec["foundations"].format(version=version))
        scores = {f[0]: (f[3], f[4]) for f in data["fns"]}
        try:
            cats = foundations(tree, ref, tsv_path.read_text(), scores, cfg["named_rule"])
        except OSError as exc:
            raise DecompError("phase.foundations", f"{tsv_path}: {exc}") from exc
        detail = cats
        order = [c for c in CATEGORIES if c in cats] + [c for c in cats if c not in CATEGORIES]
        for cat in order:
            c = cats[cat]
            for key, field in (("exact", "exact_bytes"), ("named", "named_bytes")):
                value = _pct(c[field], c["bytes"])
                rows.append(
                    row(
                        "p1",
                        game,
                        version,
                        f"foundation.{cat}.{key}_pct_b",
                        value,
                        100.0,
                        value >= 100.0,
                        str(tsv_path),
                        age,
                    )
                )
        clean = sum(c["clean_bytes"] for c in cats.values())
        floor = max(sum(c["bytes"] for c in cats.values()), fn_total * cfg.get("foundation_floor_pct", 10) / 100)
        rows.append(
            row("p1", game, version, "foundations_clean_bytes", clean, round(floor), clean >= floor, str(tsv_path), age)
        )
    return rows, detail


def _newest_runs(runs: Path, with_oracle: bool = False) -> dict[str, tuple[Path, dict[str, Any]]]:
    """The newest finished run of each movie; with `with_oracle`, the newest one that carries an oracle record."""
    newest: dict[str, tuple[Path, dict[str, Any]]] = {}
    for run, rec in tas.run_rows({"runs": runs}):
        if rec.get("status") == "running" or rec.get("input") == "human" or not rec.get("outcome"):
            continue
        if with_oracle and not rec.get("oracle"):
            continue
        name = str(rec.get("movie") or "")
        if name and (name not in newest or run.name > newest[name][0].name):
            newest[name] = (run, rec)
    return newest


def lit(frame: Path) -> bool:
    """A BMP frame with any non-zero pixel byte."""
    try:
        raw = frame.read_bytes()
        pixels = raw[int.from_bytes(raw[10:14], "little") :]
    except (OSError, ValueError):
        return False
    return pixels.count(0) != len(pixels)


def _runs_dir(profile: Profile, game: dict[str, Any]) -> Path | None:
    port = game.get("port")
    if not port:
        return None
    seat = load_profile(profile.resolve_path(port["profile"])) if port.get("profile") else profile
    return seat.path("tas.runs", seat.section("tas")["runs"])


def p2(profile: Profile, game: str, version: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    runs = _runs_dir(profile, spec)
    movie = (spec.get("port") or {}).get("movie")
    if runs is None or not movie:
        return [
            row("p2", game, version, k, "absent", True, False, "phase.games port: null") for k in ("port", "oracle")
        ]
    found = _newest_runs(runs).get(movie)
    if not found:
        return [
            row("p2", game, version, k, f"no finished run of {movie}", True, False, str(runs))
            for k in ("port", "oracle")
        ]
    run, rec = found
    age = time.time() - (run / tas.RECORD).stat().st_mtime
    ok = rec.get("outcome") == "ok" and lit(run / tas.VIEW)
    # the oracle row reads the newest run that was played with --oracle, not merely the newest run
    orun, orun_rec = _newest_runs(runs, True).get(movie) or (None, {})
    orec = orun_rec.get("oracle") or {}
    # an oracle record is {"status": "done", "outcome": "ok"}; "status" alone never equals "ok"
    oracle = (
        "ok"
        if orec.get("status") == "done" and orec.get("outcome") == "ok"
        else (orec.get("outcome") or orec.get("status") or "no oracle record")
    )
    oage = time.time() - (orun / tas.RECORD).stat().st_mtime if orun else None
    return [
        row(
            "p2", game, version, "port", run.name if ok else f"{rec.get('outcome')} {run.name}", True, ok, str(run), age
        ),
        row(
            "p2",
            game,
            version,
            "oracle",
            orun.name if oracle == "ok" else f"{oracle} {orun.name if orun else movie}",
            True,
            oracle == "ok",
            str(orun or runs),
            oage,
        ),
    ]


def p3(profile: Profile, cfg: dict[str, Any], game: str, version: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    spec = cfg["games"][game]
    runs = _runs_dir(profile, spec)
    want = {**P3_DEFAULT, **cfg.get("p3", {})}
    table = tas_coverage.read_table({"runs": runs}) if runs else None
    if not table:
        return [row("p3", game, version, "coverage_report", "absent", "present", False, str(runs))], {}
    age = time.time() - float(table.get("generated") or 0)
    summary, gcovr = table["summary"], table.get("gcovr") or {}
    port_game = summary["game"]["port"]
    unreachable = len(cfg.get("unreachable", []))
    denominator = max(summary["game"]["functions"] - unreachable, 1)
    states, behaviour = summary.get("states", {}), summary.get("behaviour", {})
    newest = _newest_runs(runs)
    crash = sum(1 for _, rec in newest.values() if rec.get("outcome") in ("crash", "hang"))
    gate_file = runs / "corpus-gate.json"
    try:
        failed = len(json.loads(gate_file.read_text()).get("failed", []))
    except (OSError, ValueError):
        failed = None
    src = str(runs / tas_coverage.TABLE)
    values = {
        "byte_reach": port_game["bytes_pct"],
        "fn_reachable": _pct(port_game["functions"], denominator),
        "line": gcovr.get("line_percent") or 0.0,
        "branch": gcovr.get("branch_percent") or 0.0,
        "oracle_only": states.get("oracle-only", 0),
        "neither": states.get("neither", 0),
        "differs_missing": behaviour.get("differs", 0) + behaviour.get("missing", 0),
        "crash": crash,
    }
    rows = [
        row(
            "p3",
            game,
            version,
            k,
            round(values[k], 2),
            want[k],
            values[k] <= want[k] if k in ("oracle_only", "differs_missing", "crash") else values[k] >= want[k],
            src,
            age,
        )
        for k in ("fn_reachable", "line", "branch", "oracle_only", "differs_missing", "crash")
    ]
    rows.append(
        row(
            "p3",
            game,
            version,
            "corpus_gate",
            "pass" if failed == 0 else f"{failed} failed",
            "pass",
            failed == 0,
            str(gate_file),
        )
    )
    return rows, {
        "values": values,
        "behaviour": behaviour,
        "age_s": round(age),
        "states": states,
        "game_functions": summary["game"]["functions"],
        "port_functions": port_game["functions"],
    }


def _evidence_time(rec: dict[str, Any], path: Path) -> float:
    """Epoch seconds of a record's `at` (epoch number or ISO 8601); the file mtime when absent or unreadable."""
    at = rec.get("at")
    if isinstance(at, (int, float)):
        return float(at)
    if isinstance(at, str):
        try:
            return datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return path.stat().st_mtime


def platform_rows(profile: Profile, cfg: dict[str, Any], game: str, version: str) -> list[dict[str, Any]]:
    """Phase 4 release rows, one per `phase.platforms` entry: `<platform>.r0` is met when the platform's
    evidence.json says installs and title and iface "5/5". Value {installs, title, iface}; age_s counts from `at`."""
    rows = []
    for name, location in sorted((cfg.get("platforms") or {}).items()):
        path = profile.resolve_path(location)
        try:
            rec = json.loads(path.read_text())
            if not isinstance(rec, dict):
                raise ValueError("not an object")
            age = time.time() - _evidence_time(rec, path)
        except (OSError, ValueError):
            rows.append(row("p4", game, version, f"{name}.r0", "no evidence", "R0", False, str(path)))
            continue
        value = {k: rec.get(k) for k in ("installs", "title", "iface")}
        met = bool(value["installs"]) and bool(value["title"]) and value["iface"] == "5/5"
        rows.append(row("p4", game, version, f"{name}.r0", value, "R0", met, str(path), age))
    return rows


def coverage_unknown(d3: dict[str, Any], previous: dict[str, Any] | None) -> str | None:
    """Why the coverage report cannot place a ladder stage: absent, empty, shrank since the last out, or > 6 h old."""
    if not d3:
        return "coverage report absent"
    if not d3.get("game_functions") or not d3.get("port_functions"):
        return "coverage report empty"
    before = ((previous or {}).get("detail") or {}).get("p3", {}).get("port_functions")
    if isinstance(before, int) and d3["port_functions"] < before:
        return f"coverage report shrank ({before} -> {d3['port_functions']} port functions)"
    if d3.get("age_s", 0) > STALE_S:
        return f"coverage report is {d3['age_s'] // 3600} h old"
    return None


def p4(ladder: list[dict[str, Any]], values: dict[str, float], corpus_ok: bool = True) -> dict[str, Any]:
    """stage = last ladder row whose every threshold holds; next_stage = the first failing row with its failing keys."""
    low = ("crash", "oracle_only", "differs_missing")
    stage, nxt = None, None
    for rung in ladder:
        failing = []
        for key, want in rung.items():
            if key == "stage":
                continue
            got = values.get(key)
            if got is None or (got > want if key in low else got < want):
                failing.append(
                    {
                        "key": key,
                        "value": got,
                        "threshold": want,
                        "gap": None if got is None else round(abs(want - got), 2),
                    }
                )
        if not corpus_ok:
            failing.append({"key": "corpus_gate", "value": "fail", "threshold": "pass", "gap": None})
        if failing:
            nxt = {"stage": rung["stage"], "failing": failing}
            break
        stage = rung["stage"]
    return {"stage": stage, "next_stage": nxt}


def next_items(
    rows: list[dict[str, Any]], detail: dict[str, Any], profile: Profile, cfg: dict[str, Any], limit: int = 5
) -> list[dict[str, Any]]:
    """Top work items for the lowest unmet phase of the focus; each `next` is a runnable command."""
    focus = cfg["focus"]
    mine = [r for r in rows if r["game"] == focus["game"] and r["version"] == focus.get("version", r["version"])]
    unmet = sorted({r["phase"] for r in mine if not r["met"]})
    if not unmet:
        return []
    phase, head = unmet[0], ["decomp", "--profile", str(profile.source_file)]
    spec = cfg["games"][focus["game"]]
    items: list[dict[str, Any]] = []
    if phase == "p1":
        failing = []
        for cat in [c for c in CATEGORIES if c in detail.get("foundations", {})]:
            failing += [
                {**f, "category": cat} for f in sorted(detail["foundations"][cat]["failing"], key=lambda f: -f["bytes"])
            ]
        for f in failing[:limit]:
            items.append(
                {
                    "fn": f["function"],
                    "item": f["function"],
                    "bytes": f["bytes"],
                    "category": f["category"],
                    "next": " ".join([*head, "recipe", "name", "--target", f["function"]]),
                }
            )
        if not items:
            items.append({"item": "map", "next": " ".join([*head, "unbake", "report"])})
    elif phase == "p2":
        movie = (spec.get("port") or {}).get("movie") or "boot-idle"
        for r in [r for r in mine if r["phase"] == "p2" and not r["met"]][:limit]:
            flag = ["--oracle"] if r["key"] == "oracle" else []
            items.append({"item": r["key"], "next": " ".join([*head, "tas", "play", movie, *flag])})
    elif phase == "p3":
        table = tas_coverage.read_table({"runs": _runs_dir(profile, spec)}) if _runs_dir(profile, spec) else None
        shown = tas_coverage.lists(table, limit) if table else {}
        for state in ("oracle_only", "neither"):
            for f in shown.get(state, []):
                if len(items) < limit:
                    items.append(
                        {
                            "fn": f["name"],
                            "item": f["name"],
                            "bytes": f["size"],
                            "state": state,
                            "next": f"grep -rn -w {f['name']} {spec['tree']}/src",
                        }
                    )
        if not items:
            items.append({"item": "coverage", "next": " ".join([*head, "tas", "coverage"])})
    else:
        stage = detail.get("p4", {}).get("next_stage") or {}
        for f in (stage.get("failing") or [])[:limit]:
            items.append({"item": f["key"], "gap": f["gap"], "next": " ".join([*head, "tas", "coverage"])})
    for item in items:
        bad = nextcmd.problems(item["next"])
        if bad:
            raise DecompError("phase.next", f"{item['next']}: {bad[0]}")
    return [{"phase": phase, **i} for i in items]


def run(
    profile: Profile, game: str | None, version: str | None, refresh: bool
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cfg = settings(profile)
    focus = cfg["focus"]
    findings: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    detail: dict[str, Any] = {"foundations": {}}
    try:
        previous = json.loads(profile.resolve_path(cfg["out"]).read_text())
    except (OSError, ValueError):
        previous = None
    for name, spec in cfg["games"].items():
        if game and name != game:
            continue
        versions = [version] if version else [focus["version"]] if name == focus["game"] else spec["versions"][:1]
        for v in versions:
            r, found = p1(profile, cfg, name, v)
            rows += r
            if found:
                detail["foundations"] = found
            rows += p2(profile, name, v, spec)
            if name == focus["game"] and v == focus.get("version", v):
                r3, d3 = p3(profile, cfg, name, v)
                rows += r3
                detail["p3"] = d3
                gate_ok = all(x["met"] for x in r3 if x["key"] == "corpus_gate")
                detail["p4"] = p4(cfg["ladder"], d3["values"], gate_ok) if d3 else {"stage": None, "next_stage": None}
                why = coverage_unknown(d3, previous)
                if why:
                    detail["p4"] = {"stage": None, "next_stage": None, "unknown": why}
                    findings.append(
                        {"key": "phase.coverage_unknown", "message": f"ladder stage unknown: {why}", "blocking": False}
                    )
                rows += platform_rows(profile, cfg, name, v)
                rows.append(
                    row(
                        "p4",
                        name,
                        v,
                        "stage",
                        "coverage unknown" if why else detail["p4"]["stage"] or "none",
                        cfg["ladder"][-1]["stage"],
                        not why and detail["p4"]["next_stage"] is None,
                        "phase.ladder",
                    )
                )
                if refresh and d3.get("age_s", 0) > REFRESH_S:
                    findings.append(
                        {
                            "key": "phase.refresh",
                            "message": "coverage union not rerun by phase (TODO): run `decomp tas coverage`",
                            "blocking": False,
                        }
                    )
    for r in rows:
        if r["age_s"] is not None and r["age_s"] > STALE_S and r["phase"] in ("p2", "p3"):
            findings.append(
                {
                    "key": "phase.stale",
                    "message": f"{r['phase']}.{r['key']} source is {r['age_s'] // 3600} h old: {r['source']}",
                    "blocking": False,
                }
            )
    result = {"focus": focus, "rows": rows, "detail": detail, "next": next_items(rows, detail, profile, cfg)}
    if refresh:
        out = profile.resolve_path(cfg["out"])
        hist = profile.resolve_path(cfg["history"]) if cfg.get("history") else None
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, sort_keys=True) + "\n")
        tmp.replace(out)
        if hist:
            with hist.open("a") as handle:
                handle.write(json.dumps({"time": time.time(), "rows": rows}, sort_keys=True) + "\n")
    return result, findings
