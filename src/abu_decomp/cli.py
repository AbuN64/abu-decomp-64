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
