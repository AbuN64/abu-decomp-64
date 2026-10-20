# SPDX-License-Identifier: GPL-3.0-or-later
"""Run a link-all archive unit's prepare + compile steps: a failure leaves empty objects and a .failed marker, so one
unit that does not compile never blocks the archive or the link (linkall.py). Kept apart from linkall.py: every
archive unit's command depends on this file, so a change to the archive or autogen logic recompiles nothing.
  unitstep.py --base BASE [--source RELATIVE] -- step --- step ..."""
import argparse
import pathlib
import re
import subprocess
import sys
OUTPUT_SUFFIXES = (".i", ".stubs.c", ".json", ".o", ".stubs.o")


def unit(argv):
    """argv: --base BASE [--source RELATIVE] -- step --- step ...; steps run in order until one fails."""
    parser = argparse.ArgumentParser(prog="unit")
    parser.add_argument("--base", required=True)
    parser.add_argument("--source", default="")
    parser.add_argument("steps", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    commands, current = [], []
    for word in args.steps[1:] if args.steps[:1] == ["--"] else args.steps:
        if word == "---":
            commands.append(current)
            current = []
        else:
            current.append(word)
    commands.append(current)
    base = pathlib.Path(args.base)
    marker = base.with_name(base.name + ".failed")
    for command in commands:
        r = subprocess.run(command, capture_output=True, text=True)
        if r.returncode:
            err = next((m for m in re.finditer(r"([^\s:]+):(\d+):\d+: error: (.*)", r.stderr)), None)
            reason = f"{args.source}:{err.group(2)} {err.group(3)}" if err else f"{args.source} does not compile"
            marker.write_text(reason + "\n")
            for suffix in OUTPUT_SUFFIXES:
                target = base.with_name(base.name + suffix)
                if suffix in (".o", ".stubs.o") or not target.exists():
                    target.write_bytes(b"")
            return 0
    marker.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(unit(sys.argv[1:]))
