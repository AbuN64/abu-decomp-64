#!/usr/bin/env python3
"""Compact recent-activity view of lanes for the 5-minute supervisor peek.

lanepeek.py [--minutes N] NAME=PATH ...
PATH is a Claude subagent .output JSONL or a Codex lane transcript.jsonl.
Per lane: the last commands/edits (truncated) and counters that flag waste:
full test suites, full multi-version builds, cold syncs, and edit->test alternations
(code changes interleaved with test runs instead of all code first).
Registry lines `job NAME=[HOST:]LOG` flag a job whose open unbake stage runs past 5x its last run; `rig HOST REGEX` flags
commands matching REGEX alive 15+ minutes at under 5% CPU (starved on slots or locks); HOST may be `local`."""

import json
import re
import subprocess
import sys
import time
from datetime import datetime

TEST = re.compile(r"bin/test(?!\s+-k)|pytest(?!.*\s-k)|unittest discover|ctest|go test")
FOCUSED = re.compile(r"bin/test\s+-k|pytest .*-k|unittest\s+\S+\.test_")
FULLBUILD = re.compile(r"make\b(?!.*build/[\w-]+/).*\bcheck\b|make -j\d+ -k check|cmake --build(?!.*--target)")
SYNC = re.compile(r"unbake\b.*\b(sync|setup)\b")


def stamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def claude(lines):
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        msg = o.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        t = stamp(o.get("timestamp"))
        for c in msg.get("content") or []:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            i = c.get("input") or {}
            if "command" in i:
                yield t, "cmd", i["command"]
            elif "file_path" in i:
                yield t, "edit", f"{c.get('name')} {i['file_path']}"


def codex(lines):
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        t = stamp(o.get("at")) if "at" in o else None
        o = o.get("value", o)
        if o.get("method") != "item/completed":
            continue
        item = (o.get("params") or {}).get("item") or {}
        kind = item.get("type", "")
        if "command" in kind.lower() or "command" in item:
            cmd = item.get("command")
            yield t, "cmd", " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        elif "change" in kind.lower() or "patch" in kind.lower():
            paths = [c.get("path") for c in item.get("changes") or [] if isinstance(c, dict)]
            yield t, "edit", "patch " + " ".join(p for p in paths if p)


OPEN = re.compile(r"^(?P<stage>\S.*?) started \(last run (?P<last>[\d.]+)s\)$")


def read_log(where):
    """A job log, local or HOST:PATH over ssh; only its tail is read."""
    host, sep, path = where.partition(":")
    if sep and "/" not in host:
        out = subprocess.run(["ssh", host, "tail", "-c", "65536", path], capture_output=True, text=True, timeout=60)
        return out.stdout.splitlines()
    with open(where, errors="replace") as f:
        return f.read()[-65536:].splitlines()


def job(name, where, now, seen):
    """Flags a job whose innermost open stage runs past 5x its own last run (at least 5 minutes): output-led, no guess.
    The first time a stage is seen open is remembered across peeks, since the logs carry no clock."""
    try:
        lines = read_log(where)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"== JOB {name}: unreadable ({e})")
        return
    opened = [(i, m) for i, line in enumerate(lines) if (m := OPEN.match(line.strip()))]
    if any(line.strip().endswith(("ALL-OK", "LAND-OK")) for line in lines[-3:]):
        print(f"== JOB {name}: done ({lines[-1].strip()[:80]})")
        return
    if not opened:
        print(f"== JOB {name}: no open stage; last: {lines[-1].strip()[:120] if lines else 'empty'}")
        return
    i, m = opened[-1]
    key = f"{name}|{len(lines) - i}|{m.group('stage')}"
    first = seen.setdefault(key, now)
    ran, last = now - first, float(m.group("last"))
    limit = max(5 * last, 300)
    flag = (
        f"STUCK? {ran / 60:.0f} min vs last run {last:.0f}s"
        if ran > limit
        else f"{ran / 60:.0f} min (limit {limit / 60:.0f})"
    )
    print(f"== JOB {name}: {m.group('stage')} | {flag}")


def idle(spec, now):
    """`rig HOST REGEX`: flags commands matching REGEX (whose parent does not match: workers idle by design) alive 15+
    minutes at under 5% CPU, waiting on slots or locks rather than working. HOST `local` reads this machine."""
    host, _, pattern = spec.partition(" ")
    match = re.compile(pattern.strip())
    ps = ["ps", "-eo", "pid=,ppid=,etimes=,times=,wchan:32=,args="]
    out = subprocess.run(
        ps if host == "local" else ["ssh", host, *ps], capture_output=True, text=True, timeout=60
    ).stdout
    rows = [r.split(None, 5) for r in out.splitlines()]
    tool = {int(r[0]) for r in rows if len(r) == 6 and match.search(r[5])}
    for parts in rows:
        if len(parts) < 6 or int(parts[0]) not in tool or int(parts[1]) in tool:
            continue
        pid, wall, cpu, wchan, args = int(parts[0]), int(parts[2]), int(parts[3]), parts[4], parts[5]
        if wall >= 900 and cpu < 0.05 * wall:
            print(
                f"== IDLE {host} pid {pid}: {wall // 60} min wall, {cpu}s CPU ({100 * cpu // wall}%), "
                f"wchan {wchan}: {args[:90]}"
            )


def read_registry(path):
    """The registry file's lines as (NAME=PATH specs, job lines, rig lines); comments and blanks are skipped."""
    specs, jobs, rigs = [], [], []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("job "):
                jobs.append(line[4:])
            elif line.startswith("rig "):
                rigs.append(line[4:])
            else:
                specs.append(line)
    return specs, jobs, rigs


def run(minutes, specs, jobs=(), rigs=(), state=None):
    """Print the job flags, the idle-rig flags, then each lane's recent actions. `state` remembers when a job's open
    stage was first seen; with no state path nothing is remembered between peeks."""
    now = time.time()
    seen = {}
    if state is not None:
        try:
            with open(state) as f:
                seen = json.load(f)
        except (OSError, ValueError):
            seen = {}
    for spec in jobs:
        name, _, where = spec.partition("=")
        job(name, where, now, seen)
    if state is not None:
        with open(state, "w") as f:
            json.dump({k: v for k, v in seen.items() if now - v < 86400}, f)
    for host in rigs:
        try:
            idle(host, now)
        except subprocess.SubprocessError as e:
            print(f"== IDLE {host}: unreadable ({e})")
    activity(minutes, specs, now)


def activity(minutes, argv, now):
    for spec in argv:
        name, _, path = spec.partition("=")
        try:
            with open(path, errors="replace") as f:
                lines = f.readlines()[-4000:]
        except OSError as e:
            print(f"== {name}: unreadable ({e.strerror})")
            continue
        events = list((codex if path.endswith("transcript.jsonl") else claude)(lines))
        recent = [e for e in events if e[0] is None or now - e[0] <= minutes * 60] or events[-12:]
        tests = sum(1 for _, k, s in recent if k == "cmd" and TEST.search(s))
        focused = sum(1 for _, k, s in recent if k == "cmd" and FOCUSED.search(s))
        builds = sum(1 for _, k, s in recent if k == "cmd" and FULLBUILD.search(s))
        syncs = sum(1 for _, k, s in recent if k == "cmd" and SYNC.search(s))
        flips, last = 0, None
        for _, k, s in recent:
            kind = "edit" if k == "edit" else "test" if (TEST.search(s) or FOCUSED.search(s)) else None
            if kind and last and kind != last:
                flips += 1
            last = kind or last
        print(
            f"== {name}: {len(recent)} actions | full-tests {tests} focused {focused} full-builds {builds} "
            f"syncs {syncs} edit<->test flips {flips}"
        )
        for t, k, s in recent[-8:]:
            when = datetime.fromtimestamp(t).strftime("%H:%M") if t else "  ?  "
            print(f"  {when} {k:4} {' '.join(s.split())[:150]}")


def main(argv, state=None):
    minutes = 5.0
    specs, jobs, rigs = [], [], []
    while argv:
        flag, argv = argv[0], argv[1:]
        if flag == "--minutes":
            minutes, argv = float(argv[0]), argv[1:]
        elif flag == "--registry":
            found = read_registry(argv[0])
            specs, jobs, rigs = specs + found[0], jobs + found[1], rigs + found[2]
            argv = argv[1:]
        else:
            specs.append(flag)
    run(minutes, specs, jobs, rigs, state)


if __name__ == "__main__":
    main(sys.argv[1:])
