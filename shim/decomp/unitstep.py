# SPDX-License-Identifier: GPL-3.0-or-later
"""Run a link-all archive unit's prepare + compile steps: a failure leaves empty objects and a .failed marker, so one
unit that does not compile never blocks the archive or the link (linkall.py). Kept apart from linkall.py: every
archive unit's command depends on this file, so a change to the archive or autogen logic recompiles nothing.
  unitstep.py --base BASE [--source RELATIVE] [--strict] -- step --- step ..."""
import argparse
import pathlib
import re
import subprocess
import sys
OUTPUT_SUFFIXES = (".i", ".stubs.c", ".json", ".o", ".stubs.o")
HERE = pathlib.Path(__file__).resolve().parent
def run(command):
    """One step. The prepare.py step runs in this process (a Python start and its imports are paid once per unit
    instead of twice, 5000 units a build); every other step is its own process."""
    if len(command) > 2 and command[1:3] == ["-I", str(HERE / "prepare.py")] or \
            len(command) > 1 and command[1:2] == [str(HERE / "prepare.py")]:
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(HERE))
        import io
        import prepare
        err = io.StringIO()
        saved = sys.argv, sys.stderr
        sys.argv, sys.stderr = [command[command.index(str(HERE / "prepare.py"))], *command[command.index(str(HERE / "prepare.py")) + 1:]], err
        try:
            code = prepare.main()
        except SystemExit as stop:
            code = stop.code if isinstance(stop.code, int) else 2
        finally:
            sys.argv, sys.stderr = saved
        return subprocess.CompletedProcess(command, code or 0, "", err.getvalue())
    return subprocess.run(command, capture_output=True, text=True)


def unit(argv):
    """argv: --base BASE [--source RELATIVE] -- step --- step ...; steps run in order until one fails."""
    parser = argparse.ArgumentParser(prog="unit")
    parser.add_argument("--base", required=True)
    parser.add_argument("--source", default="")
    parser.add_argument("--strict", action="store_true", help="a failing step fails the build (closure units)")
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
        r = run(command)
        if r.returncode and args.strict:
            sys.stderr.write(r.stdout + r.stderr)
            return r.returncode
        if r.stderr and not r.returncode:
            sys.stderr.write(r.stderr)
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
