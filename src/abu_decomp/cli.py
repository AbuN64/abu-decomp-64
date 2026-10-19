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


def _tool_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", type=Path, help="the project tree to act on (default: the profile's project)")
    parser.add_argument("--host", type=Path, help="the unbake host file for that tree (default: the profile's host)")
    _passthrough(parser)


def _run_profile(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    return _profile(args.profile).describe(), 0, []


def _run_dependencies(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp.tools import verify_dependencies

    return verify_dependencies(), 0, []


class _Text(Exception):
    """The tool answered with its own text (--version, --help); main passes the exit code through."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _run_tool(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp.tools import invoke

    stdin = sys.stdin.buffer.read() if "-" in args.args else b""
    called = invoke(args.command, _profile(args.profile), args.args, stdin=stdin, project=args.project, host=args.host)
    if called.text is not None:
        sys.stdout.buffer.write(called.text)
        sys.stdout.buffer.flush()
        raise _Text(called.returncode)
    from abu_decomp import narrate

    narrate.emit(_profile(args.profile), args.command, args.args, called.document, args.project)
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
    launch.add_argument(
        "--export", action="append", help="absolute file under an owned path whose changed content counts as progress"
    )
    launch.add_argument("--dir", type=Path, required=True, help="lane directory holding brief.md")
    launch.add_argument("--cwd", type=Path, required=True)
    launch.add_argument("--wall", type=int, required=True, help="wall seconds")
    launch.add_argument("--continues", type=int, required=True)
    launch.add_argument("--done-when", required=True, choices=("identical", "result"))
    launch.add_argument("--fanout", required=True, choices=("enabled", "disabled"))
    launch.add_argument("--max-agents", type=int, required=True)
    launch.add_argument("--owner-override", help="the owner's reason for a model outside the ladder")
    register = actions.add_parser("register", help="record a lane that has no unit (a Claude agent) in the registry")
    register.add_argument("--name", required=True)
    register.add_argument("--transcript", type=Path, required=True, help="absolute path of the agent's task output")
    register.add_argument("--kind", required=True, choices=("tooling", "impl"))
    register.add_argument("--model", required=True)
    register.add_argument("--track", choices=("official", "experiment"), default="official")
    register.add_argument("--owner-override", help="the owner's reason for a model outside the ladder")
    steer = actions.add_parser("steer", help="append the text of a file to a lane's active turn")
    steer.add_argument("--name", help="a registered live lane")
    steer.add_argument("--dir", type=Path, help="lane directory (a lane launched elsewhere)")
    steer.add_argument("--socket", type=Path, help="lane socket (a lane launched elsewhere)")
    steer.add_argument("file", type=Path, help="file holding the instruction text")
    cont = actions.add_parser("continue", help="launch a fresh lane that continues a closed one from its handoff")
    cont.add_argument("--from", dest="old_dir", type=Path, required=True, help="the closed lane's directory")
    cont.add_argument("--name", required=True, help="the new lane; its directory is a sibling of --from")
    cont.add_argument("--owns", action="append", help="owned path, overrides the old lane's value (repeatable)")
    cont.add_argument("--export", action="append", help="export file, overrides the old lane's value (repeatable)")
    cont.add_argument("--track", choices=("official", "experiment"), help="overrides the old lane's value")
    cont.add_argument("--kind", choices=("tooling", "impl"), help="overrides the old lane's value")
    # A lane with no registry row, LAUNCH.json or unit (launched before LAUNCH.json): every setting comes from these.
    cont.add_argument("--model", help="overrides the old lane's value")
    cont.add_argument("--effort", help="overrides the old lane's value")
    cont.add_argument("--wall", type=int, help="wall seconds; overrides the old lane's value")
    cont.add_argument("--continues", type=int, help="overrides the old lane's value")
    cont.add_argument("--done-when", choices=("identical", "result"), help="overrides the old lane's value")
    cont.add_argument("--fanout", choices=("enabled", "disabled"), help="overrides the old lane's value")
    cont.add_argument("--max-agents", type=int, help="overrides the old lane's value")
    cont.add_argument("--wait", type=int, default=0, help="seconds to wait for the old lane's unit to close")
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
        spec["export"] = list(args.export or [])
        return control.launch(profile, spec), 0, []
    if args.action == "register":
        return (
            control.register(
                profile, {k: v for k, v in vars(args).items() if k not in {"command", "action", "profile"}}
            ),
            0,
            [],
        )
    if args.action == "continue":
        flags = ("track", "kind", "model", "effort", "wall", "continues", "done_when", "fanout", "max_agents")
        given = {
            "owns": [str(path) for path in args.owns or []],
            "export": list(args.export or []),
            **{key: getattr(args, key) for key in flags},
        }
        return control.continue_lane(profile, args.old_dir, args.name, given, args.wait), 0, []
    if args.action == "steer":
        return control.steer_lane(profile, args.name, args.dir, args.socket, args.file), 0, []
    return control.end(profile, args.name, args.reason, args.stop), 0, []


def _tools_arguments(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", required=True, parser_class=_Parser)
    deploy = actions.add_parser(
        "deploy", help="install a decomp commit and its tools into venvs from git commits, after the pins agree"
    )
    deploy.add_argument("--sha", required=True, help="the decomp commit (40 hex)")
    for tool in ("unbake", "recipe", "taste"):
        deploy.add_argument(f"--{tool}", help=f"the {tool} commit to deploy (default: the one decomp pins)")
    deploy.add_argument("--venv", type=Path, required=True, nargs="+", help="venvs to install into")


def _run_tools(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp import deploy

    commits = {tool: getattr(args, tool) for tool in ("unbake", "recipe", "taste")}
    return deploy.deploy(_profile(args.profile), args.sha, [path.resolve() for path in args.venv], commits)


def _probe_arguments(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", required=True, parser_class=_Parser)
    request = actions.add_parser("request", help="queue one oracle probe request and announce it as HANDOFF")
    request.add_argument("--requester", required=True, help="requesting lane")
    request.add_argument("--address", required=True, help="owner address, 0x...")
    request.add_argument("--function", required=True, help="function name")
    request.add_argument("--question", required=True, help="one line: what the probe must observe")
    request.add_argument("--decomp-path", help="decomp source path of the owner")
    request.add_argument("--match-state", help="match state of that source")
    request.add_argument("--evidence", type=Path, action="append", default=[], help="file the request rests on")
    request.add_argument("--supersedes", help="id of the request this one replaces")
    actions.add_parser("serve", help="the open requests, oldest first, each with its Taste repro story")
    status = actions.add_parser("status", help="every request joined to its PROBE events")
    status.add_argument("--id", help="one request")


def _run_probe(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp import probe

    profile = _profile(args.profile)
    if args.action == "request":
        result = probe.make_request(
            profile,
            requester=args.requester,
            address=args.address,
            function=args.function,
            question=args.question,
            decomp_path=args.decomp_path,
            match_state=args.match_state,
            evidence=args.evidence,
            supersedes=args.supersedes,
        )
        return result, 0, []
    if args.action == "serve":
        return probe.serve(profile), 0, []
    return probe.status(profile, args.id), 0, []


def _run_cycle(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp.cycle import serve

    return serve(_profile(args.profile)), 0, []


def _run_stream(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp.stream.cli import run

    return run(_profile(args.profile), tuple(args.args)), 0, []


def _console_arguments(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", required=True, parser_class=_Parser)
    tile = actions.add_parser("tile", help="run the console for one tile of the profile's tiles table")
    tile.add_argument("tile", help="tile id")
    hud = actions.add_parser("hud", help="print the one-line overlay text of a video tile (its `hud` object)")
    hud.add_argument("tile", help="tile id")
    actions.add_parser("refs", help="follow the configured git refs and append LAND events")
    actions.add_parser("status-line", help="print the newest event for the compositor bar")


def _run_console(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    profile = _profile(args.profile)
    if args.action == "tile":
        from abu_decomp.console.tiles import run_tile

        return run_tile(profile, args.tile), 0, []
    if args.action == "hud":
        from abu_decomp.console.hud import run_hud

        return run_hud(profile, args.tile), 0, []
    from abu_decomp.console.tiles import run_follower

    return run_follower(profile, args.action), 0, []


def _supervisor_arguments(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", required=True, parser_class=_Parser)
    for name, text in (
        ("run", "supervise a role headless until SIGTERM (SIGHUP restarts, SIGUSR1 pauses the retry)"),
        ("status", "print a supervised role's status file"),
        ("viewer", "exec the viewer command on an artifact role's current image"),
    ):
        actions.add_parser(name, help=text).add_argument("--role", required=True)
    show = actions.add_parser("show-artifact", help="stage one real PNG/JPEG result for an artifact role")
    show.add_argument("--role", required=True)
    show.add_argument("--source", type=Path, required=True)
    show.add_argument("--name", required=True)


def _run_supervisor(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp.supervisor.cli import run

    result = run(
        _profile(args.profile), args.action, args.role, getattr(args, "source", None), getattr(args, "name", None)
    )
    return result, 0, []


def _build_arguments(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", required=True, parser_class=_Parser)
    once = actions.add_parser("once", help="pin committed sources, run the gates and promote the releases")
    once.add_argument("--prove-only", action="store_true", help="run the gates without promoting")
    actions.add_parser("status", help="print the latest and current build receipts")


def _run_build(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp.build.cli import run

    argv = [args.action, *(["--prove-only"] if getattr(args, "prove_only", False) else [])]
    return run(_profile(args.profile), argv), 0, []


def _land_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tree", type=Path, required=True, help="proof tree: a clean checkout at --base")
    parser.add_argument("--base", required=True, help="git ref the tree must be at")
    parser.add_argument(
        "--patch",
        action="append",
        required=True,
        metavar="LANE=PATH[:MESSAGE]",
        help="a lane's patch, in order (repeatable); MESSAGE is that lane's commit message when it matches no function",
    )


def _run_land(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp.land import land, parse_messages, parse_patches

    return land(
        _profile(args.profile), args.tree, args.base, parse_patches(args.patch), messages=parse_messages(args.patch)
    )


def _behave_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", type=Path, help="the project tree to act on (default: the profile's project)")
    parser.add_argument("--host", type=Path, help="the unbake host file for that tree (default: the profile's host)")
    parser.add_argument("--function", required=True, help="the function symbol, as the project names it")
    parser.add_argument("--candidate", type=Path, help="candidate C file; without it the original is described")
    parser.add_argument("--version", help="ROM version (default: the project's names_from)")
    parser.add_argument("--cases", type=int, help="input combinations to run, capped (default: profile behave.cases)")
    parser.add_argument("--seed", type=int, default=1, help="seed of the deterministic input set (default 1)")
    parser.add_argument("--depth", type=int, help="calls deep the original callees run; deeper ones are stubs")


def _run_behave(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp.behave.run import Options, run

    options = Options(
        args.function, args.candidate, args.version, args.cases, args.seed, args.project, args.host, args.depth
    )
    return run(_profile(args.profile), options)


def _served_elsewhere(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    raise DecompError("internal.dispatch", f"{args.command} is served by main, not by a command handler")


def _run_describe(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    return describe(), 0, []


COMMANDS: dict[str, Command] = {
    "profile": Command("show the loaded seat profile", _none, _run_profile),
    "dependencies": Command("check every pinned tool is installed at its commit", _none, _run_dependencies),
    "unbake": Command("run unbake on the profile's project (or --project)", _tool_arguments, _run_tool),
    "recipe": Command("run Recipe Maker on the profile's project (or --project)", _tool_arguments, _run_tool),
    "taste": Command("run Cake Taste with the profile's Taste profile", _tool_arguments, _run_tool),
    "event": Command("append, tail and validate the event feed", _event_arguments, _run_event),
    "tools": Command("deploy the pinned tools into venvs", _tools_arguments, _run_tools),
    "lane": Command("launch, register, steer and end AI lanes", _lane_arguments, _run_lane),
    "supervisor": Command("supervise a role headless, or stage an artifact", _supervisor_arguments, _run_supervisor),
    "build": Command("build committed sources and promote verified releases", _build_arguments, _run_build),
    "console": Command(
        "run a tile console, the git ref follower or the bar status line", _console_arguments, _run_console
    ),
    "probe": Command("queue oracle probe requests and join them to PROBE events", _probe_arguments, _run_probe),
    "cycle": Command("rerun Recipe and unbake report whenever the decomp sources settle", _none, _run_cycle),
    "stream": Command("serve, supervise, control and capture the headless tile stream", _passthrough, _run_stream),
    "port": Command("generate a native port repository skeleton", _port_arguments, _run_port),
    "land": Command("apply lane patches to a proof tree and run the landing gates", _land_arguments, _run_land),
    "behave": Command(
        "run the original and a candidate on the same inputs and compare their behaviour",
        _behave_arguments,
        _run_behave,
    ),
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


def _tool_call(args: list[str]) -> argparse.Namespace | None:
    """`[--profile P] unbake|recipe|taste [--project D] [--host F] ARGS...`, with ARGS untouched (argparse would eat
    --help and --version); None when the line is anything else."""
    from abu_decomp.tools import TOOLS

    rest, profile = list(args), None
    if rest[:1] == ["--profile"] and len(rest) > 1:
        profile, rest = Path(rest[1]), rest[2:]
    elif rest[:1] and rest[0].startswith("--profile="):
        profile, rest = Path(rest[0].partition("=")[2]), rest[1:]
    if not rest or rest[0] not in TOOLS:
        return None
    found: dict[str, Path | None] = {"project": None, "host": None}
    command, rest = rest[0], rest[1:]
    while rest:
        flag, eq, value = rest[0].partition("=")
        if flag not in ("--project", "--host"):
            break
        if eq:
            rest = rest[1:]
        elif len(rest) > 1:
            value, rest = rest[1], rest[2:]
        else:
            raise DecompError("cli.arguments", f"{flag} expects a value")
        found[flag[2:]] = Path(value)
    return argparse.Namespace(profile=profile, command=command, project=found["project"], host=found["host"], args=rest)


def _own_version(args: list[str]) -> bool:
    return args[-1:] == ["--version"] and (len(args) == 1 or (len(args) == 3 and args[0] == "--profile"))


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if _own_version(args):
        from abu_decomp.tools import version_text

        sys.stdout.write(version_text())
        return 0
    started = time.monotonic()
    invocation = uuid.uuid4().hex
    result: dict[str, Any] | None = None
    findings: list[dict[str, Any]] = []
    code = 0
    command = "parse"
    try:
        parsed = _tool_call(args) or parser().parse_args(args)
        command = parsed.command
        if command == "mcp":
            from abu_decomp.mcp import serve

            prefix = ["--profile", str(parsed.profile)] if parsed.profile else []
            return serve(sys.stdin.buffer, sys.stdout.buffer, prefix=prefix)
        result, code, findings = COMMANDS[command].run(parsed)
    except _Text as exc:
        return exc.code
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
