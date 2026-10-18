#!/usr/bin/env python3
"""Compact recent-activity view of lanes for the 5-minute supervisor peek.

lanepeek.py [--minutes N] NAME=PATH ...
PATH is a Claude subagent .output JSONL or a Codex lane transcript.jsonl.
Per lane: the last commands/edits (truncated) and counters that flag waste:
full test suites, full multi-version builds, cold syncs, and edit->test alternations
(code changes interleaved with test runs instead of all code first)."""

import json
import re
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


def main(argv):
    minutes = 5.0
    specs = []
    while argv:
        flag, argv = argv[0], argv[1:]
        if flag == "--minutes":
            minutes, argv = float(argv[0]), argv[1:]
        elif flag == "--registry":
            with open(argv[0]) as f:
                specs += [l.strip() for l in f if l.strip() and not l.startswith("#")]
            argv = argv[1:]
        else:
            specs.append(flag)
    argv = specs
    now = time.time()
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


if __name__ == "__main__":
    main(sys.argv[1:])
