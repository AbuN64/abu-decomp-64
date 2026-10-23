"""Buckets: the findings of a batch run grouped by root cause, so one cycle lane takes a whole bucket.
Input is the findings feed (module tas_findings); the root key of a finding is its strongest fact (the struct field or
datum touched, then the sweep rule, a null base in a function, the fault function and its caller, the writer's
signature,
the outcome). A bucket lists its members (kind, movie, poll, run, evidence), the movies it blocks and the sweep sites
that share its root. Buckets are ranked by class first (crash/stop/hang/exit, fn-differs and state-diverge, then
sanitizer reports, then sweep rules that only read source), then by reach (movies blocked), hits and sites. A
sanitizer-only bucket seen in nearly every sanitized run (AMBIENT_SHARE) is boot-wide noise: all of them collapse into
one trailing "ambient" bucket unless `--kind` names a sanitizer kind. `kinds` filters the findings before bucketing.
`next` is one command that runs as printed.
Read it with `decomp tas triage`; the TAS page Work tab shows the same rows (GET /api/board `triage`).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from abu_decomp import tas_findings

SANITIZER = ("ubsan", "asan", "msan", "tsan", "lsan", "sanitizer")  # finding kinds a sanitizer build reports
AMBIENT_SHARE = 0.9  # a sanitizer bucket in at least this share of the sanitized movies fires in every run
AMBIENT_MIN = 5  # ...and only counts as ambient with at least this many movies behind it
MEMBERS = 12  # members listed per bucket (the count says how many there are)


def _file_of(evidence: Any) -> str:
    return re.sub(r":\d+$", "", str(evidence or ""))


def _line_of(evidence: Any) -> int:
    match = re.search(r":(\d+)$", str(evidence or ""))
    return int(match[1]) if match else 0


def _next(kind: str, members: list[dict[str, Any]], profile_path: str) -> str:
    first = members[0]
    movie = next((m["movie"] for m in members if m.get("movie")), None)
    if movie and kind != "sweep":
        flag = f"--profile {profile_path} " if profile_path else ""
        return f"decomp {flag}tas play {movie} --speed 1"
    site = first.get("evidence")
    if site and _line_of(site):
        line = _line_of(site)
        return f"sed -n '{max(1, line - 3)},{line + 6}p' {_file_of(site)}"
    return "decomp tas triage"


def _title(level: str, key: str, bucket: dict[str, Any]) -> str:
    if level == "datum":
        offs = ", ".join(f"+{o:#x}" for o in bucket["offsets"][:4])
        decl = f" [{bucket['declared']}]" if bucket.get("declared") else ""
        return f"{key}{decl}" + (f" touched at {offs}" if offs else "")
    if level == "sweep":
        return f"{key}: {bucket['count']} sites in {bucket['sites']} files"
    return key


def _tier(kinds: Any) -> int:
    """0 a run broke (crash/stop/hang/exit, fn-differs, state-diverge, anything unknown), 1 sanitizer, 2 sweep."""
    kinds = set(kinds)
    if kinds - set(SANITIZER) - {"sweep"}:
        return 0
    return 1 if kinds & set(SANITIZER) else 2


def _ambient(
    out: list[dict[str, Any]], standing: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Split the sanitizer-only buckets seen in (nearly) every sanitized movie off `out`; one summary bucket."""
    sanitized = {r["movie"] for r in standing if r["kind"] in SANITIZER and r.get("movie")}
    need = max(AMBIENT_MIN, math.ceil(len(sanitized) * AMBIENT_SHARE))
    quiet = [b for b in out if set(b["kinds"]) <= set(SANITIZER) and len(b["movies"]) >= need]
    if not quiet:
        return out, None
    names = {id(b) for b in quiet}
    reports = sum(b["count"] for b in quiet)
    ambient = {
        "key": "ambient",
        "level": "ambient",
        "count": reports,
        "movies": [],
        "reach": min(len(b["movies"]) for b in quiet),
        "sites": len(quiet),
        "kinds": dict(Counter(k for b in quiet for k in b["kinds"] for _ in range(b["kinds"][k]))),
        "offsets": [],
        "members": [],
        "title": f"ambient: {len(quiet)} sanitizer reports seen in {len(sanitized)} sanitized movies "
        f"(each in at least {need}), boot-wide",
        "ambient": [b["title"] for b in quiet],
        "next": "decomp tas triage --kind " + sorted({k for b in quiet for k in b["kinds"]})[0],
    }
    return [b for b in out if id(b) not in names], ambient


def build(
    rows: list[dict[str, Any]],
    profile_path: str = "",
    batch: str | None = None,
    top: int = 0,
    kinds: Any = (),
) -> list[dict[str, Any]]:
    """Ranked buckets of the standing findings (`rows` is the whole feed). `kinds` keeps only findings of those kinds
    (and shows sanitizer buckets as they are, not collapsed); `top` counts the ranked buckets, the ambient line is
    extra."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    kinds = {k for k in (kinds or ()) if k}
    standing = tas_findings.active(rows, batch)
    for row in standing:
        if kinds and row["kind"] not in kinds:
            continue
        groups.setdefault(tas_findings.root(row), []).append(row)
    out = []
    for (level, key), members in groups.items():
        movies = sorted({m["movie"] for m in members if m.get("movie")})
        files = {_file_of(m.get("evidence")) for m in members if m["kind"] == "sweep"}
        bucket: dict[str, Any] = {
            "key": key,
            "level": level,
            "count": len(members),
            "movies": movies,
            "sites": len(files) if files else len({m.get("run") or m.get("evidence") for m in members}),
            "kinds": dict(Counter(m["kind"] for m in members)),
            "offsets": sorted({int(m["offset"]) for m in members if isinstance(m.get("offset"), int)}),
        }
        for field in ("fn", "callers", "datum", "field"):
            value = next((m[field] for m in members if m.get(field)), None)
            if value:
                bucket[field] = value
        members.sort(key=lambda m: (m.get("movie") or "", m.get("poll") or 0, str(m.get("evidence") or "")))
        bucket["members"] = [
            {
                k: m.get(k)
                for k in ("kind", "movie", "poll", "run", "evidence", "outcome", "subject", "text")
                if m.get(k) is not None
            }
            for m in members[:MEMBERS]
        ]
        kind = "sweep" if set(bucket["kinds"]) == {"sweep"} else "run"
        bucket["next"] = _next(kind, members, profile_path)
        bucket["title"] = _title(level, key, {**bucket, "declared": None})
        out.append(bucket)
    ambient = None
    if not kinds & set(SANITIZER):
        out, ambient = _ambient(out, standing)
    out.sort(key=lambda b: (_tier(b["kinds"]), -len(b["movies"]), -b["count"], -b["sites"], b["key"]))
    out = out[:top] if top else out
    if ambient:
        out.append(ambient)
    for number, bucket in enumerate(out, 1):
        bucket["rank"] = number
    return out


def enrich(project: Path | None, buckets: list[dict[str, Any]], limit: int = 12) -> None:
    """Add the declaration (the type) and site of each datum bucket's global from the project source, for the first
    `limit` datum buckets; a miss leaves the bucket as it was."""
    from abu_decomp import tas_triage

    if project is None:
        return
    done = 0
    for bucket in buckets:
        if bucket["level"] != "datum" or done >= limit:
            continue
        done += 1
        name = bucket.get("datum", "")
        pattern = re.compile(rf"\b{re.escape(name)}\b\s*(?:\[[^\]]*\]\s*)*(?:=|;)")
        site = tas_triage._grep(project, name, pattern)
        if not site:
            continue
        bucket["site"] = site
        file, _, number = site.rpartition(":")
        try:
            text = (project / file).read_text(errors="replace").splitlines()[int(number) - 1]
        except (OSError, ValueError, IndexError):
            continue
        bucket["declared"] = re.sub(r"\s*(?:=.*|;)\s*$", "", text.strip())[:90]
        bucket["title"] = _title("datum", bucket["key"], bucket)


def render(buckets: list[dict[str, Any]]) -> str:
    if not buckets:
        return "buckets: none\nnext: nothing open"
    lines = ["buckets (strongest first; a cycle lane takes a whole bucket):"]
    for b in buckets:
        if b["level"] == "ambient":
            lines.append(f"  {b['rank']}. [ambient] {b['title']}: {b['count']} findings (hidden; `{b['next']}`)")
            continue
        reach = f"blocks {len(b['movies'])} movies: {', '.join(b['movies'])}" if b["movies"] else "source only"
        lines.append(f"  {b['rank']}. [{b['level']}] {b['title']}: {b['count']} findings, {reach}")
        if b.get("fn") or b.get("site"):
            lines.append(
                "     "
                + "; ".join(
                    x for x in (f"fn {b['fn']}" if b.get("fn") else "", f"at {b['site']}" if b.get("site") else "") if x
                )
            )
        for m in b["members"][:4]:
            who = m.get("movie") or m.get("evidence") or ""
            lines.append(f"       - {m['kind']} {who}" + (f" @{m['poll']}" if m.get("poll") is not None else ""))
        if len(b["members"]) > 4:
            lines.append(f"       ... {b['count'] - 4} more")
        lines.append(f"     next: {b['next']}")
    lines.append(f"next: {buckets[0]['next']}")
    return "\n".join(lines)
