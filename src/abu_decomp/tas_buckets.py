"""Buckets: the findings of a batch run grouped by root cause, so one cycle lane takes a whole bucket.
Input is the findings feed (module tas_findings); the root key of a finding is its strongest fact (the struct field or
datum touched, then the sweep rule, a null base in a function, the fault function and its caller, the writer's
signature,
the outcome). A bucket lists its members (kind, movie, poll, run, evidence), the movies it blocks and the sweep sites
that share its root; buckets are ranked by reach (movies blocked), then hits, then sites. Strongest first: a bucket a
real run reaches outranks a rule that only reads source. `next` is one command that runs as printed.
Read it with `decomp tas triage`; the TAS page Work tab shows the same rows (GET /api/board `triage`).
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from abu_decomp import tas_findings

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


def build(
    rows: list[dict[str, Any]], profile_path: str = "", batch: str | None = None, top: int = 0
) -> list[dict[str, Any]]:
    """Ranked buckets of the standing findings (`rows` is the whole feed)."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in tas_findings.active(rows, batch):
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
    out.sort(key=lambda b: (-len(b["movies"]), -b["count"], -b["sites"], b["key"]))
    for number, bucket in enumerate(out, 1):
        bucket["rank"] = number
    return out[:top] if top else out


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
