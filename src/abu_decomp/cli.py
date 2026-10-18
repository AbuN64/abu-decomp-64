"""The one public `decomp` entry point. Every command prints one JSON envelope on stdout.

The two exceptions are help (plain text) and `decomp mcp`, whose stdout is the MCP stdio channel.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from abu_decomp import DecompError
from abu_decomp.config import Profile, load_profile


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DecompError("cli.arguments", message)


@dataclass(frozen=True)
class Command:
    help: str
    arguments: Callable[[argparse.ArgumentParser], None]
    run: Callable[[argparse.Namespace], tuple[dict[str, Any], int, list[dict[str, Any]]]]


def _profile(path: Path | None) -> Profile:
    if path is None:
        raise DecompError("config.missing", "pass --profile PATH before the command")
    return load_profile(path)


def _none(parser: argparse.ArgumentParser) -> None:
    del parser


def _passthrough(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("args", nargs=argparse.REMAINDER, help="the tool's own command and arguments")


def _run_profile(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    return _profile(args.profile).describe(), 0, []


def _run_dependencies(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp.tools import verify_dependencies

    return verify_dependencies(), 0, []


def _run_tool(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp.tools import invoke

    stdin = sys.stdin.buffer.read() if "-" in args.args else b""
    called = invoke(args.command, _profile(args.profile), args.args, stdin=stdin)
    if called.stderr:
        sys.stderr.write(called.stderr)
    if called.ok:
        return called.describe(), 0, []
    findings = called.document.get("findings") or [
        {
            "key": "dependency.refused",
            "blocking": True,
            "message": f"{args.command} returned exit {called.returncode}, ok={called.document['ok']}",
        }
    ]
    return called.describe(), 1, findings


def _port_arguments(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", required=True, parser_class=_Parser)
    new = actions.add_parser("new", help="write the port boilerplate into an empty directory")
    new.add_argument("--spec", type=Path, required=True, help="port spec JSON (the port's project facts)")
    new.add_argument("destination", type=Path)


def _run_port(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp.port import generate, load_spec

    return generate(load_spec(args.spec), args.destination.resolve()), 0, []


def _event_arguments(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", required=True, parser_class=_Parser)
    add = actions.add_parser("append", help="append one event through the Cake Taste writer")
    add.add_argument("--actor", required=True, help="lane or tool name")
    add.add_argument("--status", required=True, help="info|started|passed|failed|blocked|decision|handoff")
    add.add_argument("--message", required=True, help="one line of fact")
    add.add_argument("--kind", help="RUN-FAIL|PROBE|MATCH|HANDOFF|LAND|LANE|DECISION")
    add.add_argument("--evidence", type=Path, help="file whose path and sha256 are recorded")
    add.add_argument("--subject", help="subject object as JSON (repo, ref, version, function, address)")
    add.add_argument("--lane", help="lane object as JSON (name, kind, model, track)")
    add.add_argument("--payload", help="payload object as JSON")
    tail = actions.add_parser("tail", help="show the last events with their schema 2 kind")
    tail.add_argument("--lines", type=int, default=20, help="how many events (default 20)")
    tail.add_argument("--kinds", help="comma-separated schema 2 kinds to keep")
    validate = actions.add_parser("validate", help="validate every line of a feed; the feed is only read")
    validate.add_argument("--path", type=Path, help="feed to check (default: the profile's events path)")


def _json_object(name: str, text: str | None) -> dict[str, Any] | None:
    if text is None:
        return None
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise DecompError("cli.arguments", f"--{name} is not JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise DecompError("cli.arguments", f"--{name} must be a JSON object")
    return value


def _run_event(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp import events

    path = (
        args.path if args.action == "validate" and args.path is not None else events.events_path(_profile(args.profile))
    )
    if args.action == "append":
        evidence = events.writer().evidence_of(args.evidence) if args.evidence else None
        record = {
            "actor": args.actor,
            "status": args.status,
            "message": args.message,
            "kind": args.kind,
            "evidence": evidence,
            "subject": _json_object("subject", args.subject),
            "lane": _json_object("lane", args.lane),
            "payload": _json_object("payload", args.payload),
        }
        return events.append(path, record), 0, []
    if args.action == "tail":
        kinds = {kind.strip() for kind in args.kinds.split(",") if kind.strip()} if args.kinds else None
        return {"path": str(path), "events": events.tail(path, args.lines, kinds)}, 0, []
    report = events.validate(path)
    findings = [
        {"key": "events.invalid", "line": row["line"], "message": row["message"], "blocking": True}
        for row in report["findings"]
    ]
    return {"path": str(path), "events": report["events"], "schemas": report["schemas"]}, int(bool(findings)), findings


def _lane_arguments(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", required=True, parser_class=_Parser)
    launch = actions.add_parser(
        "launch", help="start a lane as a systemd user unit after the ladder and overlap checks"
    )
    launch.add_argument("--name", required=True)
    launch.add_argument("--kind", required=True, choices=("tooling", "impl"))
    launch.add_argument("--model", required=True)
    launch.add_argument("--effort", required=True)
    launch.add_argument("--track", required=True, choices=("official", "experiment"))
    launch.add_argument("--owns", required=True, action="append", help="absolute path the lane may write (repeatable)")
    launch.add_argument("--dir", type=Path, required=True, help="lane directory holding brief.md")
    launch.add_argument("--cwd", type=Path, required=True)
    launch.add_argument("--wall", type=int, required=True, help="wall seconds")
    launch.add_argument("--continues", type=int, required=True)
    launch.add_argument("--done-when", required=True, choices=("identical", "result"))
    launch.add_argument("--fanout", required=True, choices=("enabled", "disabled"))
    launch.add_argument("--max-agents", type=int, required=True)
    launch.add_argument("--owner-override", help="the owner's reason for a model outside the ladder")
    steer = actions.add_parser("steer", help="append the text of a file to a lane's active turn")
    steer.add_argument("--name", help="a registered live lane")
    steer.add_argument("--dir", type=Path, help="lane directory (a lane launched elsewhere)")
    steer.add_argument("--socket", type=Path, help="lane socket (a lane launched elsewhere)")
    steer.add_argument("file", type=Path, help="file holding the instruction text")
    peek = actions.add_parser("peek", help="recent actions and waste counters of live lanes")
    peek.add_argument("--minutes", type=float, default=5.0)
    peek.add_argument("--name", action="append", default=[], help="a live lane (default: all)")
    peek.add_argument("extra", nargs="*", help="additional NAME=PATH transcripts")
    end = actions.add_parser("end", help="mark a lane ended in the registry")
    end.add_argument("--name", required=True)
    end.add_argument("--reason", required=True)
    end.add_argument("--stop", action="store_true", help="also stop the lane's running unit")


def _run_lane(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp.lanes import control

    profile = _profile(args.profile)
    if args.action == "launch":
        spec = {key: value for key, value in vars(args).items() if key not in {"command", "action", "profile"}}
        spec["owns"] = [str(path) for path in args.owns]
        return control.launch(profile, spec), 0, []
    if args.action == "steer":
        return control.steer_lane(profile, args.name, args.dir, args.socket, args.file), 0, []
    if args.action == "peek":
        return control.peek_lanes(profile, args.name, args.minutes, args.extra), 0, []
    return control.end(profile, args.name, args.reason, args.stop), 0, []


def _run_stream(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp.stream.cli import run

    return run(_profile(args.profile), tuple(args.args)), 0, []


def _console_arguments(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", required=True, parser_class=_Parser)
    tile = actions.add_parser("tile", help="run the console for one tile of the profile's tiles table")
    tile.add_argument("tile", help="tile id")
    actions.add_parser("refs", help="follow the configured git refs and append LAND events")
    actions.add_parser("status-line", help="print the newest event for the compositor bar")


def _run_console(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    profile = _profile(args.profile)
    if args.action == "tile":
        from abu_decomp.console.tiles import run_tile

        return run_tile(profile, args.tile), 0, []
    from abu_decomp.console.tiles import run_follower

    return run_follower(profile, args.action), 0, []


def _served_elsewhere(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    raise DecompError("internal.dispatch", f"{args.command} is served by main, not by a command handler")


def _run_describe(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    return describe(), 0, []


COMMANDS: dict[str, Command] = {
    "profile": Command("show the loaded seat profile", _none, _run_profile),
    "dependencies": Command("check every pinned tool is installed at its commit", _none, _run_dependencies),
    "unbake": Command("run unbake on the profile's project", _passthrough, _run_tool),
    "recipe": Command("run Recipe Maker on the profile's project", _passthrough, _run_tool),
    "taste": Command("run Cake Taste with the profile's Taste profile", _passthrough, _run_tool),
    "event": Command("append, tail and validate the event feed", _event_arguments, _run_event),
    "lane": Command("launch, steer, peek and end AI lanes", _lane_arguments, _run_lane),
    "console": Command(
        "run a tile console, the git ref follower or the bar status line", _console_arguments, _run_console
    ),
    "stream": Command("serve, supervise, control and capture the headless tile stream", _passthrough, _run_stream),
    "port": Command("generate a native port repository skeleton", _port_arguments, _run_port),
    "describe": Command("list every command and its arguments as JSON", _none, _run_describe),
    "mcp": Command("serve the commands as MCP tools over stdio", _none, _served_elsewhere),
}


def parser() -> _Parser:
    root = _Parser(prog="decomp", description="The decompilation dev cycle and its seat")
    root.add_argument("--profile", type=Path, help="explicit seat JSON profile")
    commands = root.add_subparsers(dest="command", required=True, parser_class=_Parser)
    for name, command in COMMANDS.items():
        command.arguments(commands.add_parser(name, help=command.help, description=command.help))
    return root


def _arguments(sub: argparse.ArgumentParser) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for action in sub._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        row: dict[str, Any] = {
            "name": action.dest,
            "flags": list(action.option_strings),
            "help": action.help,
            "required": bool(action.required),
            "nargs": action.nargs,
        }
        if isinstance(action, argparse._SubParsersAction):
            row["subcommands"] = {name: _arguments(child) for name, child in action.choices.items()}
        elif action.choices:
            row["choices"] = list(action.choices)
        rows.append(row)
    return rows


def describe() -> dict[str, Any]:
    """The command tree derived from the parser itself; the MCP shim builds its tool list from this."""
    group = next(action for action in parser()._actions if isinstance(action, argparse._SubParsersAction))
    commands = [
        {"name": name, "help": COMMANDS[name].help, "usage": sub.format_usage().strip(), "arguments": _arguments(sub)}
        for name, sub in group.choices.items()
    ]
    return {"program": "decomp", "commands": commands}


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    started = time.monotonic()
    invocation = uuid.uuid4().hex
    result: dict[str, Any] | None = None
    findings: list[dict[str, Any]] = []
    code = 0
    command = "parse"
    try:
        parsed = parser().parse_args(args)
        command = parsed.command
        if command == "mcp":
            from abu_decomp.mcp import serve

            prefix = ["--profile", str(parsed.profile)] if parsed.profile else []
            return serve(sys.stdin.buffer, sys.stdout.buffer, prefix=prefix)
        result, code, findings = COMMANDS[command].run(parsed)
    except DecompError as exc:
        code, findings = 1, [exc.finding()]
    except (ValueError, OSError) as exc:
        code, findings = 1, [{"key": "command.failed", "message": str(exc), "blocking": True}]
    except SystemExit as exc:
        # argparse help is the only plain-text stdout path.
        if exc.code == 0:
            return 0
        code, findings = 2, [{"key": "cli.arguments", "message": f"{command} rejected its arguments", "blocking": True}]
    except Exception as exc:
        sys.stderr.write(traceback.format_exc())
        code, findings = 3, [{"key": "internal.error", "message": repr(exc), "blocking": True}]
    envelope = {
        "ok": code == 0,
        "result": result,
        "findings": findings,
        "stages": [{"name": command, "wall_seconds": round(time.monotonic() - started, 6)}],
        "invocation": invocation,
    }
    sys.stdout.write(json.dumps(envelope, sort_keys=True, default=str) + "\n")
    sys.stdout.flush()
    return code
