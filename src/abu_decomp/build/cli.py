"""Public `decomp build` commands. The core CLI owns JSON stdout."""

import argparse
import fcntl
import json

from abu_decomp import DecompError
from abu_decomp.config import Profile

from . import BuildProfile
from .runner import Runner
from .util import BuildFailure


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def _event(profile, status, message, evidence=None):
    from abu_decomp import events

    try:
        path = events.events_path(profile)
    except DecompError:
        return None
    return events.append(path, {"actor": "build", "status": status, "message": message, "evidence": evidence})


def run(profile: Profile, argv):
    parser = Parser(prog="decomp build", add_help=False)
    commands = parser.add_subparsers(dest="command", required=True)
    once = commands.add_parser("once", add_help=False)
    once.add_argument("--prove-only", action="store_true")
    commands.add_parser("status", add_help=False)
    args = parser.parse_args(argv)
    try:
        runner = Runner(BuildProfile(profile))
    except BuildFailure as error:
        raise DecompError("build.config", str(error)) from error
    if args.command == "status":
        latest = runner.workspace / "latest.json"
        current = runner.workspace / "status.json"
        return {
            "action": "status",
            "latest": json.loads(latest.read_text()) if latest.exists() else None,
            "current": json.loads(current.read_text()) if current.exists() else None,
        }
    lock_path = runner.workspace / "run.lock"
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"action": "once", "status": "busy", "workspace": str(runner.workspace)}
        _event(profile, "started", "Committed build gates started", str(runner.workspace / "status.json"))
        result = runner.drain(prove_only=args.prove_only)
        event_status = (
            "passed"
            if result["status"] in ("proved", "promoted")
            else ("blocked" if result["status"] == "blocked" else "failed")
        )
        _event(profile, event_status, f"Committed build {result['status']}", result["receipt"])
        if result["status"] in ("failed", "blocked"):
            raise DecompError("build.gates", f"{result['status']}: {result.get('error')}; receipt={result['receipt']}")
    return {"action": "once", **result}
