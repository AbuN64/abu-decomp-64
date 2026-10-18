#!/usr/bin/env python3
"""Compact recent-activity view of lanes for the 5-minute supervisor peek.

lanepeek.py [--minutes N] NAME=PATH ...
PATH is a Claude subagent .output JSONL or a Codex lane transcript.jsonl.
Per lane: the last commands/edits (truncated) and counters that flag waste:
full test suites, full multi-version builds, cold syncs, and edit->test alternations
(code changes interleaved with test runs instead of all code first).
Registry lines `job NAME=[HOST:]LOG` flag a job whose open unbake stage runs past 5x its last run; `rig HOST REGEX` flags
commands matching REGEX alive 15+ minutes at under 5% CPU (starved on slots or locks); HOST may be `local`.
Per lane, from the registry row or its LAUNCH.json: `STALE` when the last recorded action is older than the stale
threshold while the lane still runs, and `IDLE` for any process (local or on a rig host) whose argv or cwd is under
the lane's owned dirs and that sits under the idle CPU share beyond the idle age. Thresholds come from the seat's
`lanes` section (`stale_minutes`, `idle_minutes`, `idle_cpu_percent`)."""

import json
import os
import re
import subprocess
import sys
import time
from itertools import pairwise
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

TEST = re.compile(r"bin/test(?!\s+-k)|pytest(?!.*\s-k)|unittest discover|ctest|go test")
FOCUSED = re.compile(r"bin/test\s+-k|pytest .*-k|unittest\s+\S+\.test_")
FULLBUILD = re.compile(r"make\b(?!.*build/[\w-]+/).*\bcheck\b|make -j\d+ -k check|cmake --build(?!.*--target)")
SYNC = re.compile(r"unbake\b.*\b(sync|setup)\b")


@dataclass(frozen=True)
class Thresholds:
    """The staleness rules; the seat's `lanes` section overrides each (keys stale_minutes, idle_minutes, idle_cpu_percent)."""

    stale_minutes: float = 10.0
    idle_minutes: float = 15.0
    idle_cpu_percent: float = 5.0


DEFAULT = Thresholds()


@dataclass(frozen=True)
class LaneFacts:
    """What the peek needs beyond the transcript: the dirs a lane owns and whether its unit is still running."""

    owns: tuple[str, ...] = ()
    running: bool = True


def stamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def claude(lines):
    """Actions with their time. A streamed message is split over several lines and only some carry a timestamp,
    so a line without one takes the latest earlier stamp in the file rather than none."""
    t = None
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        t = stamp(o.get("timestamp")) or t
        msg = o.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        for c in msg.get("content") or []:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            i = c.get("input") or {}
            if "command" in i:
                yield t, "cmd", i["command"]
            elif "file_path" in i:
                yield t, "edit", f"{c.get('name')} {i['file_path']}"


def codex(lines):
    t = None
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        t = (stamp(o.get("at")) if "at" in o else None) or t
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


FAILURE = (re.compile(r"Refused:"), re.compile(r"Error\b|Traceback"))


def log_state(where, now):
    """(minutes since the log last grew, whether a process holds it open or names it in argv) for a local or HOST:PATH
    log. The one liveness rule: a log that stopped growing with nothing writing it belongs to a dead job."""
    host, sep, path = where.partition(":")
    if not sep or "/" in host:
        host, path = "local", where
    if host == "local":
        age = now - os.path.getmtime(path)
        held = any(os.path.realpath(f"{fd}") == path for fd in _fds())
    else:
        age = now - float(
            subprocess.run(
                ["ssh", host, "stat", "-c", "%Y", path], capture_output=True, text=True, timeout=60, check=True
            ).stdout
        )
        held = (
            subprocess.run(["ssh", host, f"ls -l /proc/[0-9]*/fd 2>/dev/null | grep -qF {path}"], timeout=60).returncode
            == 0
        )
    held = held or any(path in r[5] for r in process_rows(host) if len(r) == 6)
    return age / 60, held


def _fds():
    import glob

    return glob.glob("/proc/[0-9]*/fd/*")


def job(name, where, now, seen, th=DEFAULT):
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
    try:
        quiet, held = log_state(where, now)
    except (OSError, ValueError, subprocess.SubprocessError):
        quiet, held = 0.0, True
    if quiet > th.stale_minutes and not held:
        why = next((line.strip() for pattern in FAILURE for line in reversed(lines) if pattern.search(line)), None)
        why = why or (lines[-1].strip() if lines else "empty")
        print(f"== DEAD {name}: log idle {quiet:.0f} min, no process holds it, no success marker; last: {why[:120]}")
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


def process_rows(host):
    """`ps` rows (pid, ppid, wall s, cpu s, wchan, args) of this machine or of HOST over ssh."""
    ps = ["ps", "-eo", "pid=,ppid=,etimes=,times=,wchan:32=,args="]
    out = subprocess.run(
        ps if host == "local" else ["ssh", host, *ps], capture_output=True, text=True, timeout=60
    ).stdout
    return [r.split(None, 5) for r in out.splitlines()]


def starved(rows, selected, th, label, where=""):
    """Prints an IDLE line for each selected top-level process (its parent is not selected: workers idle by design)
    that is old enough and under the CPU share. The one idle rule, shared by `rig` lines and lane-owned processes."""
    tool = {int(r[0]) for r in rows if len(r) == 6 and selected(r)}
    for parts in rows:
        if len(parts) < 6 or int(parts[0]) not in tool or int(parts[1]) in tool:
            continue
        pid, wall, cpu, wchan, args = int(parts[0]), int(parts[2]), int(parts[3]), parts[4], parts[5]
        if wall >= th.idle_minutes * 60 and cpu * 100 < th.idle_cpu_percent * wall:
            print(
                f"== IDLE {label} pid {pid}{where}: {wall // 60} min wall, {cpu}s CPU ({100 * cpu // wall}%), "
                f"wchan {wchan}: {args[:90]}"
            )


def idle(spec, now, th=DEFAULT):
    """`rig HOST REGEX`: flags commands matching REGEX (whose parent does not match: workers idle by design) alive 15+
    minutes at under 5% CPU, waiting on slots or locks rather than working. HOST `local` reads this machine."""
    host, _, pattern = spec.partition(" ")
    match = re.compile(pattern.strip())
    starved(process_rows(host), lambda r: match.search(r[5]), th, host)


def under(path, roots):
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def owned_idle(name, owns, host, rows, th, proc="/proc"):
    """Flags processes of lane NAME on HOST: argv naming a path under its owned dirs, or (local only) a cwd under them.
    Never flagged: the peek and its parent, the lane's own supervisor (argv `--name NAME`) and the agent it started
    (the supervisor's children), which wait on the model by design; the commands they run are checked."""
    supervisor = {int(r[0]) for r in rows if len(r) == 6 and (a := r[5].split()) and ("--name", name) in pairwise(a)}
    skip = {os.getpid(), os.getppid(), *supervisor, *(int(r[0]) for r in rows if int(r[1]) in supervisor)}

    def cwd(pid):
        try:
            return os.readlink(f"{proc}/{pid}/cwd") if host == "local" else ""
        except OSError:
            return ""

    def selected(r):
        return int(r[0]) not in skip and (under(cwd(r[0]), owns) or any(under(w, owns) for w in r[5].split()))

    starved(rows, selected, th, name, f" on {host}")


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


def launch_owns(transcript):
    """The `owns` recorded in LAUNCH.json beside a lane's transcript, for a lane the registry has no row for."""
    try:
        record = json.loads((Path(transcript).parent / "LAUNCH.json").read_text())
        return tuple(str(p) for p in record.get("owns") or ())
    except (OSError, ValueError, AttributeError):
        return ()


def run(minutes, specs, jobs=(), rigs=(), state=None, th=DEFAULT, facts=None):
    """Print the job flags, the idle-rig flags, the idle flags of lane-owned processes, then each lane's recent actions
    (a STALE flag ahead of a lane whose last action is old while it runs). `state` remembers when a job's open stage
    was first seen; with no state path nothing is remembered between peeks. `facts` maps a lane name to its LaneFacts;
    a lane without facts owns what its LAUNCH.json records and counts as running."""
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
        job(name, where, now, seen, th)
    if state is not None:
        with open(state, "w") as f:
            json.dump({k: v for k, v in seen.items() if now - v < 86400}, f)
    for host in rigs:
        try:
            idle(host, now, th)
        except subprocess.SubprocessError as e:
            print(f"== IDLE {host}: unreadable ({e})")
    facts = facts or {}
    hosts = ["local", *dict.fromkeys(r.partition(" ")[0] for r in rigs if r.partition(" ")[0] != "local")]
    table = {}
    for host in hosts:
        try:
            table[host] = process_rows(host)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"== IDLE {host}: unreadable ({e})")
    lanes = {}
    for spec in specs:
        name, _, path = spec.partition("=")
        info = facts.get(name) or LaneFacts(launch_owns(path))
        lanes[name] = info
        if not info.running or not info.owns:
            continue
        for host, rows in table.items():
            owned_idle(name, info.owns, host, rows, th)
    activity(minutes, specs, now, th, {n: i.running for n, i in lanes.items()})


def finished(lines):
    """True when a Claude task's last assistant message ended its turn: the agent is done, nothing is in flight."""
    for line in reversed(lines):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        msg = o.get("message") or {}
        if msg.get("role") == "assistant":
            return msg.get("stop_reason") == "end_turn"
    return False


def activity(minutes, argv, now, th=DEFAULT, running=None):
    running = running or {}
    for spec in argv:
        name, _, path = spec.partition("=")
        try:
            with open(path, errors="replace") as f:
                lines = f.readlines()[-4000:]
            modified = os.path.getmtime(path)
        except OSError as e:
            print(f"== {name}: unreadable ({e.strerror})")
            continue
        events = list((codex if path.endswith("transcript.jsonl") else claude)(lines))
        last_at = max((e[0] for e in events if e[0] is not None), default=modified)
        if running.get(name, True) and not finished(lines) and now - last_at > th.stale_minutes * 60:
            flight = " ".join(events[-1][2].split())[:120] if events else "none"
            print(f"== STALE {name}: no new action for {(now - last_at) / 60:.0f} min, in flight: {flight}")
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
