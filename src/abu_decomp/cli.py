"""The one public `decomp` entry point. Every command prints one JSON envelope on stdout.

The two exceptions are help (plain text) and `decomp mcp`, whose stdout is the MCP stdio channel.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import traceback
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, priority, runslice
from abu_decomp.config import Profile, default_profile_path, load_profile


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DecompError("cli.arguments", message)


@dataclass(frozen=True)
class Command:
    help: str
    arguments: Callable[[argparse.ArgumentParser], None]
    run: Callable[[argparse.Namespace], tuple[dict[str, Any], int, list[dict[str, Any]]]]


def _profile(path: Path | None) -> Profile:
    path = path or default_profile_path()
    if path is None:
        raise DecompError(
            "config.missing",
            "pass --profile PATH before the command, set DECOMP_PROFILE, or link the seat profile at "
            "~/.config/abu-decomp/profile.json",
        )
    return load_profile(path)


def _none(parser: argparse.ArgumentParser) -> None:
    del parser


def _passthrough(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("args", nargs=argparse.REMAINDER, help="the tool's own command and arguments")


def _tool_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project",
        "--tree",
        type=Path,
        help="the project tree to act on (default: the work tree of the current directory, else the profile's project)",
    )
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
    from abu_decomp.tools import cwd_project, invoke

    stdin = sys.stdin.buffer.read() if "-" in args.args else b""
    profile = _profile(args.profile)
    project = cwd_project(profile, args.command, args.args, args.project)
    called = invoke(args.command, profile, args.args, stdin=stdin, project=project, host=args.host)
    if called.text is not None:
        sys.stdout.buffer.write(called.text)
        sys.stdout.buffer.flush()
        raise _Text(called.returncode)
    print(f"decomp: {args.command} used {called.cpu:.1f} CPU s in {called.seconds:.1f} s", file=sys.stderr)
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


def _run_make(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp import proof
    from abu_decomp.tools import cwd_project

    profile = _profile(args.profile)
    # the tree the caller stands in, as the unbake and recipe verbs do (a bare make built the seat tree 4 times)
    project = cwd_project(profile, "make", args.args, args.project)
    return proof.run(profile, args.args, project=project, host=args.host)


def _port_arguments(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", required=True, parser_class=_Parser)
    new = actions.add_parser("new", help="write the port boilerplate into an empty directory")
    new.add_argument("--spec", type=Path, required=True, help="port spec JSON (the port's project facts)")
    new.add_argument("destination", type=Path)
    carry = actions.add_parser("remap", help="carry the source's symbol renames since a commit into the port's facts")
    carry.add_argument("--tree", type=Path, required=True, help="the port tree (land.trees entry with pin.remap)")
    carry.add_argument("--since", help="the source commit the tree's facts are spelled at (not with --renames)")
    carry.add_argument(
        "--renames",
        type=Path,
        nargs="+",
        metavar="TSV",
        help="carry name-ledger batch maps (renames/batch-K.tsv) instead",
    )
    carry.add_argument("--to", help="the source commit or ref the renames run to (default: the pin config's ref)")
    carry.add_argument("--check", action="store_true", help="report what would change and write nothing")
    carry.add_argument(
        "--like", type=Path, help="read the pin config from this registered port tree (--tree is a clone of it)"
    )


def _run_port(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    if args.action == "remap":
        from abu_decomp import land, remap, repin

        profile = _profile(args.profile)
        tree = args.tree.expanduser().resolve()
        registered = args.like.expanduser().resolve() if args.like else tree
        pin = land.settings(profile, registered)["pin"]
        if pin is None:
            raise remap.RemapError("remap.config", f"land.trees has no pin for {registered}")
        if args.renames:
            result = remap.from_map(tree, pin, [m.expanduser() for m in args.renames], write=not args.check)
            return result, 0, []
        if not args.since:
            raise remap.RemapError("remap.config", "remap needs --since COMMIT, or --renames TSV...")
        old = repin.commit(land.run_process, pin["source"], args.since)
        new = repin.commit(land.run_process, pin["source"], args.to or pin["ref"])
        result, findings = remap.remap(land.run_process, tree, pin, old, new, write=not args.check)
        return result, int(any(f["blocking"] for f in findings)), findings
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
        try:
            return control.continue_lane(profile, args.old_dir, args.name, given, args.wait), 0, []
        except DecompError as error:
            if (
                args.wait
            ):  # the runner's detached relaunch unit: nobody reads its log, so the registry carries the failure
                control.mark_continue_failed(
                    profile, args.old_dir.expanduser().resolve(), f"{args.name}: {error.detail}"
                )
            raise
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
    deploy.add_argument("--venv", type=Path, required=True, nargs="+", help="live venv paths (symlinks) to deploy to")


def _run_tools(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp import deploy

    profile = _profile(args.profile)
    live = [Path(os.path.abspath(path)) for path in args.venv]  # never resolve: the live path is the symlink
    commits = {tool: getattr(args, tool) for tool in ("unbake", "recipe", "taste")}
    return deploy.deploy(profile, args.sha, live, commits)


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


def _tas_arguments(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", required=True, parser_class=_Parser)
    monkey = actions.add_parser(
        "monkey", help="seeded random controller input on the sanitizer port; keeps the movies that find something"
    )
    monkey.add_argument("--seeds", required=True, help="3, 1-8 or 1,4,9-11")
    monkey.add_argument("--polls", type=int, default=3000, help="random polls per movie (default 3000)")
    monkey.add_argument("--hold", type=int, default=24, help="longest run of equal polls (default 24)")
    monkey.add_argument("--prefix", help="a saved movie that reaches the start point; the random input follows it")
    monkey.add_argument("--port", type=Path, help="the sanitizer build's executable (default tas.sanitizer.port)")
    monkey.add_argument("--jobs", type=int, default=2, help="parallel runs (default 2)")
    monkey.add_argument("--keep", choices=("findings", "all"), default="findings", help="which movies stay saved")
    findings = actions.add_parser("findings", help="the sanitizer findings, grouped by file:line")
    findings.add_argument("--limit", type=int, default=50, help="groups to list (default 50)")
    save = actions.add_parser("save", help="save a recording (default: the newest) as a named movie")
    save.add_argument("name", help="lower-case words joined by '-', saying what the movie does")
    save.add_argument("--from", dest="source", type=Path, help="raw recording to save")
    save.add_argument("--note", default="", help="one line: what the movie shows")
    save.add_argument("--replace", action="store_true", help="overwrite a movie of the same name")
    save.add_argument("--pak", choices=("on", "off"), default="on", help="the recording ran with the pak")
    save.add_argument(
        "--cpak",
        choices=("none", "fresh"),
        default="none",
        help="the recording started with no Controller Pak or a new one",
    )
    actions.add_parser("list", help="the saved movies and the newest recordings")
    point = actions.add_parser("point", help="start points: places in the game a movie can begin")
    point_verbs = point.add_subparsers(dest="verb", required=True, parser_class=_Parser)
    point_verbs.add_parser("list", help="the start points, the areas they reach and the areas still without one")
    card = point_verbs.add_parser(
        "card", help="keep the card a replay run ended with, to start points from its save data"
    )
    card.add_argument("run", help="the replay run whose end-of-run Controller Pak card to keep")
    keep_point = point_verbs.add_parser("save", help="keep the first polls of a movie or run as a start point")
    keep_point.add_argument("name", help="lower-case words joined by '-', saying where the point is")
    keep_point.add_argument("--movie", help="the saved movie whose polls make the point")
    keep_point.add_argument("--run", help="the replay run (its recording) whose polls make the point")
    keep_point.add_argument("--at", type=int, help="keep the first N polls (default: all of them)")
    keep_point.add_argument("--area", default="", help="the game area it reaches (a name in points/areas.txt)")
    keep_point.add_argument("--note", default="", help="one line: how the point gets there")
    keep_point.add_argument("--replace", action="store_true", help="overwrite a point of the same name")
    coverage = actions.add_parser(
        "coverage", help="union the newest run of every movie into the game-code coverage table"
    )
    coverage.add_argument("--no-oracle", action="store_true", help="do not replay runs that lack an oracle trace")
    coverage.add_argument(
        "--oracle-seconds",
        type=float,
        default=None,
        help="start no oracle replay after this many seconds (the rest wait for the next call); default: no limit",
    )
    coverage.add_argument(
        "--table-only",
        action="store_true",
        help="rebuild only the function table from the per-run counts (seconds, no gcovr merge or page); "
        "the HTML page and line totals stay the last full build's",
    )
    coverage.add_argument(
        "--jobs", type=int, default=0, help="parallel oracle replays (default 0: the host slot pool capacity)"
    )
    coverage.add_argument(
        "--rebaseline",
        action="store_true",
        help="replace the report in place even when the new union covers fewer functions (the join key or the port "
        "build changed on purpose); the old one is kept as coverage-table.prev.json",
    )
    bake = actions.add_parser("bake", help="write the static site data the Runner page reads, into <tas.runs>/site/")
    bake.add_argument("--what", default="", help="what to read again: source,xref,coverage,movie (default: all)")
    bake.add_argument("--movie", help="with --what movie: bake only this movie")
    bake.add_argument("--union", type=Path, help="the gcovr union JSON (default: <tas.runs>/coverage/union.json)")
    actions.add_parser("serve", help="serve the TAS page: every replay's picture, sound, log and the cycle state")
    triage = actions.add_parser(
        "triage", help="the findings of batch runs, sweeps and checks grouped by root cause, strongest bucket first"
    )
    triage.add_argument("--batch", help="only this batch's findings (a play --all stamp)")
    triage.add_argument("--top", type=int, default=0, help="only the first N buckets")
    triage.add_argument(
        "--kind",
        action="append",
        default=[],
        help="only findings of this kind (crash, crash-sanitized, hang, stop, exit, fn-differs, state-diverge, sweep, "
        "ubsan; repeat or comma-separate); a sanitizer kind lists its buckets uncollapsed",
    )
    triage.add_argument(
        "--backfill", action="store_true", help="first add findings for each movie's newest recorded run"
    )
    corpus = actions.add_parser("corpus", help="the regression corpus of movies, problem spots and the speedrun gate")
    verbs = corpus.add_subparsers(dest="verb", required=True, parser_class=_Parser)
    verbs.add_parser("list", help="corpus entries, problem spots, coverage features and the last gate")
    verbs.add_parser("seed", help="offer every movie's newest run (replaying those without one), then minimise")
    verbs.add_parser("minimise", help="drop automatic entries whose coverage the others already reach")
    keep = verbs.add_parser("add", help="keep a movie by hand (never minimised away)")
    keep.add_argument("movie")
    keep.add_argument("--why", default="", help="one line: what the movie guards")
    verbs.add_parser("drop", help="remove a movie from the corpus").add_argument("movie")
    gate = verbs.add_parser(
        "gate", help="replay every entry at turbo and judge it against its baseline; exit 1 on a fail"
    )
    gate.add_argument("--port", type=Path, help="gate this build's executable (a lane's own; baselines never move)")
    gate.add_argument("--jobs", type=int, default=0, help="parallel replays (default 0: the host slot pool capacity)")
    gate.add_argument(
        "--no-oracle", action="store_true", help="do not replay the original for entries without a reference"
    )
    accept = verbs.add_parser(
        "accept", help="take the last gate's runs as baselines of failed entries (a meant change)"
    )
    accept.add_argument("movies", nargs="*", help="failed entries to accept (default: every failed one)")
    spot = verbs.add_parser("spot", help="define or drop a problem spot (a check for a non-crash bug)")
    spots = spot.add_subparsers(dest="spot_verb", required=True, parser_class=_Parser)
    define = spots.add_parser("add", help="define a spot on a movie, replay it once and keep the movie")
    define.add_argument("id", help="lower-case words joined by '-'")
    define.add_argument("--movie", required=True, help="the movie that shows the spot")
    define.add_argument("--ram", help="a RAM check at --at, like 'u32@0x80111D24 == 0xE3F635E3' (host byte order)")
    define.add_argument("--at", type=int, help="the poll the RAM check reads")
    define.add_argument("--function", help="a function check: reached, unreached or a minimum count (--expect)")
    define.add_argument("--expect", help="reached (default), unreached or a minimum entry count")
    define.add_argument("--note", default="", help="one line: the bug the spot guards")
    define.add_argument("--replace", action="store_true", help="redefine an existing spot")
    spots.add_parser("drop", help="drop a spot").add_argument("id")
    for verb, text in (
        ("show", "one movie's header and polls"),
        ("delete", "remove one movie"),
    ):
        actions.add_parser(verb, help=text).add_argument("name")
    play = actions.add_parser("play", help="replay a movie (or every movie with --all) and report the outcome")
    play.add_argument("name", nargs="?")
    play.add_argument(
        "--all",
        action="store_true",
        help="replay every saved movie in parallel; exit 1 if any is worse (the other options apply to each movie)",
    )
    play.add_argument(
        "--jobs", type=int, default=0, help="parallel replays with --all (default 0: the host's slot pool capacity)"
    )
    play.add_argument(
        "--changed",
        action="store_true",
        help="with --all: replay only the movies the sources changed since their last good run can have touched "
        "(profile tas.select); the others keep their recorded standing",
    )
    play.add_argument(
        "--resume",
        action="store_true",
        help="with --all: skip a movie whose newest run came from the current build (same pins) with a real outcome, "
        "so a throttled or interrupted sweep restarts cheaply",
    )
    play.add_argument(
        "--nice", type=int, default=0, help="with --all: scheduling niceness of the sweep (default 0: 10, never lower)"
    )
    play.add_argument(
        "--gdb-all",
        action="store_true",
        help="with --all: every movie under the gdb bridge (default: only one not ending ok is replayed under it)",
    )
    play.add_argument(
        "--fps-unlock",
        action="store_true",
        help="framerate unlock: draw and present extra pictures between game frames; the run itself is unchanged",
    )
    play.add_argument("--speed", type=int, default=0, help="0 = turbo (default), 1 = real time with sound, k = k times")
    play.add_argument("--twice", action="store_true", help="replay twice; same_twice compares the two runs' RAM")
    play.add_argument("--port", type=Path, help="run this build's executable instead of the profile's (a lane's build)")
    play.add_argument("--label", help="tag the run with this lane name on the TAS page")
    play.add_argument(
        "--sanitize",
        action="store_true",
        help="run the sanitizer port (tas.sanitizer.port, or --port): ASan + UBSan reports become findings.jsonl lines",
    )
    play.add_argument("--pak", choices=("on", "off"), help="Expansion Pak on (8 MB) or off (4 MB), else the movie's")
    play.add_argument(
        "--cpak",
        choices=("on", "off"),
        help="Controller Pak card on (the movie's, else a fresh one) or off, else the movie's",
    )
    play.add_argument(
        "--shots-every", type=int, default=0, metavar="N", help="write a picture every N polls to RUN/shots"
    )
    play.add_argument("--dump-ram-at", type=int, metavar="P", help="write guest RAM at poll P to RUN/ram-P.bin")
    play.add_argument(
        "--trace-draws",
        type=int,
        metavar="P",
        help="write the draws (texture, palette, combiner, colours, "
        "geometry) of the first graphics task from poll P to RUN/draws-P.txt",
    )
    play.add_argument(
        "--oracle", action="store_true", help="replay it in the original game beside the port and compare outcomes"
    )
    play.add_argument(
        "--oracle-pictures",
        action="store_true",
        help="with --oracle: also dump both sides' pictures every tas.oracle.shot_every polls and score them "
        "(off by default: ~650 .ppm per run filled the rig disk)",
    )
    play.add_argument(
        "--from",
        dest="start",
        metavar="POINT",
        help="begin at start point POINT: alone it replays the point; before NAME it replaces NAME's own start",
    )
    play.add_argument(
        "--take",
        action="store_true",
        help="with --from and no movie: replay the point at turbo, then drive live; saved as a new movie",
    )
    play.add_argument(
        "--trace-events",
        metavar="FUNC[/N],...",
        help="under the gdb bridge: log each call of FUNC (and its first N integer arguments) to RUN/events.txt",
    )
    play.add_argument("--gdb-init", type=Path, metavar="FILE", help="gdb commands run before the port starts")
    play.add_argument(
        "--fault-gdb", type=Path, metavar="FILE", help="gdb commands run at a host fault (RUN/signal-extra.txt)"
    )
    play.add_argument(
        "--fpe-tolerate",
        action="store_true",
        help="count guest float traps (RUN/fpe-tolerated.txt) and run on instead of stopping at the first",
    )
    play.add_argument(
        "--no-state-diff",
        action="store_true",
        help="skip the region-by-region game state comparison with the original (findings.jsonl)",
    )


def _run_tas(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp import tas

    profile = _profile(args.profile)
    if args.action == "save":
        saved = tas.save(
            profile, args.name, source=args.source, note=args.note, replace=args.replace, pak=args.pak, cpak=args.cpak
        )
        return saved, 0, []
    if args.action == "list":
        return tas.listing(profile), 0, []
    if args.action == "point":
        from abu_decomp import tas_points

        if args.verb == "card":
            return tas_points.keep_card(profile, args.run), 0, []
        if args.verb == "save":
            return (
                tas_points.save_point(
                    profile,
                    args.name,
                    movie=args.movie,
                    run=args.run,
                    at=args.at,
                    area=args.area,
                    note=args.note,
                    replace=args.replace,
                ),
                0,
                [],
            )
        listed = tas_points.listing(profile)
        print(tas_points.render(listed), file=sys.stderr)
        return listed, 0, []
    if args.action == "monkey":
        from abu_decomp import tas_sanitize

        done = tas_sanitize.monkey(
            profile,
            tas_sanitize.seeds_of(args.seeds),
            polls=args.polls,
            hold=args.hold,
            prefix=args.prefix,
            port=args.port or tas.settings(profile)["sanitizer_port"],
            jobs=args.jobs,
            keep=args.keep,
        )
        return done, 0, []
    if args.action == "findings":
        from abu_decomp import tas_sanitize

        rows = tas_sanitize.read(tas.settings(profile))
        return {"total": len(rows), "groups": tas_sanitize.grouped(rows)[: args.limit]}, 0, []
    if args.action == "serve":
        from abu_decomp import tas_serve

        return tas_serve.serve(profile), 0, []
    if args.action == "bake":
        from abu_decomp import tas_bake

        what = {w for w in args.what.split(",") if w}
        return tas_bake.bake(profile, what or None, args.movie, args.union), 0, []
    if args.action == "coverage":
        from abu_decomp import tas_coverage

        report = tas_coverage.build(
            profile,
            tas.settings(profile),
            oracle=not args.no_oracle,
            jobs=args.jobs,
            oracle_seconds=args.oracle_seconds,
            table_only=args.table_only,
            rebaseline=getattr(args, "rebaseline", False),
        )
        if report is None:
            raise DecompError("tas.coverage", "the profile has no tas.coverage section")
        print(tas_coverage.line(report), file=sys.stderr)
        refused = [
            {"key": "tas.coverage.refused", "blocking": True, "message": report["refused"], "next": report.get("next")}
        ] if report.get("refused") else []  # fmt: skip
        return tas_coverage.lists(report, 25), (5 if refused else 0), refused
    if args.action == "triage":
        return _run_triage(profile, args)
    if args.action == "corpus":
        return _run_corpus(profile, args)
    if args.action == "show":
        return tas.show(profile, args.name), 0, []
    if args.action == "delete":
        return tas.delete(profile, args.name), 0, []
    # gdb bridge inputs (the bridge reads them from its environment; TAS-COVER-4/5 set the variables by hand)
    for flag, var in (("gdb_init", "N64C_GDB_INIT"), ("fault_gdb", "N64C_FAULT_GDB")):
        given = getattr(args, flag, None)
        if given:
            if not Path(given).is_file():
                raise DecompError("tas.play", f"--{flag.replace('_', '-')} {given} is no file")
            os.environ[var] = str(Path(given).resolve())
    if getattr(args, "fpe_tolerate", False):
        os.environ["N64C_GDB_FPE_TOLERATE"] = "1"
    if getattr(args, "trace_events", None):
        os.environ["N64C_TRACE_EVENTS"] = args.trace_events
    inspect = {"shots_every": args.shots_every, "dump_ram_at": args.dump_ram_at, "trace_draws": args.trace_draws}
    if args.twice and any(inspect.values()):
        raise DecompError("tas.play", "--twice compares two plain runs; inspect with a single replay")
    if not args.all and not args.name and not args.start:
        raise DecompError("tas.play", "give a movie name, --from POINT or --all")
    options = {
        "speed": args.speed,
        "port": args.port,
        "label": args.label,
        "pak": args.pak,
        "cpak": args.cpak,
        "oracle": args.oracle,
        "oracle_pictures": args.oracle_pictures,
        "fps_unlock": args.fps_unlock,
        "start": args.start,
        "take": args.take,
        "state_diff": not args.no_state_diff,
        "sanitize": args.sanitize,
    }
    if args.all:  # every play option applies to every movie
        result, code = tas.play_all(
            profile,
            jobs=args.jobs,
            twice=args.twice,
            changed=args.changed,
            resume=args.resume,
            nice=args.nice,
            gdb_all=args.gdb_all,
            **options,
            **inspect,
        )
        print(tas.table(result), file=sys.stderr)
        return result, code, []
    if args.twice:
        twice = tas.play_twice(profile, args.name, **options)
        return twice, 0, []
    played = tas.play(profile, args.name, **options, **inspect)
    return played, 0, []


def _run_triage(profile: Any, args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp import tas, tas_buckets, tas_findings, tas_triage

    cfg = tas.settings(profile)
    project = tas_triage._project(profile)
    added = tas_findings.backfill(cfg["runs"], project) if args.backfill else 0
    rows = tas_findings.read(cfg["runs"])
    buckets = tas_buckets.build(
        rows, str(profile.source_file), args.batch, args.top, [k for v in args.kind for k in v.split(",")]
    )
    tas_buckets.enrich(project, buckets)
    print(tas_buckets.render(buckets), file=sys.stderr)
    return (
        {"findings": len(rows), "added": added, "buckets": buckets, "next": buckets[0]["next"] if buckets else ""},
        0,
        [],
    )


def _run_corpus(profile: Any, args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp import tas, tas_corpus

    verb = args.verb
    if verb == "list":
        return tas_corpus.view(tas.settings(profile)), 0, []
    if verb == "seed":
        return tas_corpus.seed(profile), 0, []
    if verb == "minimise":
        cfg = tas.settings(profile)
        with tas_corpus.locked(cfg):
            data = tas_corpus.load(cfg)
            dropped = tas_corpus.minimise(cfg, data["entries"])
            tas_corpus.store(cfg, data)
        return {"dropped": dropped, "kept": sorted(data["entries"])}, 0, []
    if verb == "add":
        return tas_corpus.add(profile, args.movie, args.why), 0, []
    if verb == "drop":
        return tas_corpus.drop(profile, args.movie), 0, []
    if verb == "gate":
        result, code = tas_corpus.gate(profile, port=args.port, jobs=args.jobs, oracle=not args.no_oracle)
        print(tas_corpus.render(result), file=sys.stderr)
        return result, code, []
    if verb == "accept":
        return tas_corpus.accept(profile, args.movies), 0, []
    if args.spot_verb == "drop":
        return tas_corpus.drop_spot(profile, args.id), 0, []
    spot = tas_corpus.add_spot(
        profile,
        args.id,
        args.movie,
        ram=args.ram,
        at=args.at,
        function=args.function,
        expect=args.expect,
        note=args.note,
        replace=args.replace,
    )
    return spot, 0, []


def _run_cycle(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp.cycle import serve

    return serve(_profile(args.profile)), 0, []


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


def _ship_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--tree", type=Path, required=True, help="the warm tree: a checkout the ship keeps at the branch"
    )
    parser.add_argument(
        "--patch",
        action="append",
        default=[],
        metavar="LANE=PATH[:MESSAGE]",
        help="an input to land, commit and push; repeat it for every ready input, landed in order in one round with "
        "one commit each and one push (MESSAGE as for land)",
    )
    parser.add_argument(
        "--worktree",
        nargs="?",
        const="worktree",
        metavar="NAME[:MESSAGE]",
        help="the tree's own tracked changes are the input (the default when no --patch is given): captured as a "
        "patch, gated like one, committed alone and pushed; the tree ends clean at the new commit, and a refusal puts "
        "it back exactly as it was",
    )
    parser.add_argument(
        "--no-proof",
        action="store_true",
        help="land every input with the full gates under the lock: no proof is built in the lane's work tree and none "
        "is carried over",
    )
    parser.add_argument(
        "--no-train",
        action="store_true",
        help="do not split a patch that has header and body hunks: its header hunks land with the body in the full "
        "round, not in the types train",
    )
    parser.add_argument(
        "--regen",
        action="store_true",
        help="regenerate README, maps, overview and progress in one commit once enough carried ships landed (a carried "
        "ship starts this itself); the branch tip is built first, red is a finding",
    )
    parser.add_argument("--regen-now", action="store_true", help="with --regen: regenerate even when not yet due")
    parser.add_argument("--regen-min", type=int, default=6, help="with --regen: carried ships that make it due")
    parser.add_argument(
        "--regen-age", type=float, default=900.0, help="with --regen: seconds since the last that make it due"
    )


def _run_ship(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp.land import parse_messages, parse_patches
    from abu_decomp.ship import ship

    if args.regen:
        if args.patch or args.worktree:
            raise DecompError("ship.arguments", "--regen is a ship of its own; give it no input")
        return ship(
            _profile(args.profile), args.tree, [], regen=True, regen_now=args.regen_now,
            regen_min=args.regen_min, regen_age=args.regen_age,
        )  # fmt: skip
    if args.patch and args.worktree:
        raise DecompError("ship.arguments", "--worktree is the input itself; give it without --patch")
    if args.patch:
        return ship(
            _profile(args.profile),
            args.tree,
            parse_patches(args.patch),
            messages=parse_messages(args.patch),
            proof=not args.no_proof,
            split=not args.no_train,
        )
    name, _, message = (args.worktree or "worktree").partition(":")
    return ship(
        _profile(args.profile),
        args.tree,
        [],
        messages={name: message.strip()} if message.strip() else {},
        worktree=name,
    )


def _worktree_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "verb",
        choices=["new", "prune"],
        help="new: a lane's own work tree, warm from the shared tree; prune: remove ended lanes'",
    )
    parser.add_argument(
        "lane", nargs="*", help="new: the lane name (tree <shared>/../wt/LANE); prune: ended lane names"
    )
    parser.add_argument("--results", type=Path, help="prune: also every lane with a RESULT.md under this lanes dir")
    parser.add_argument("--tree", type=Path, required=True, help="the shared tree the lane's worktree is seeded from")
    parser.add_argument("--at", help="the commit (default: the branch tip, fetched)")


def _run_worktree(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp import worktree

    if args.verb == "prune":
        return worktree.prune(args.tree, args.lane, args.results)
    if len(args.lane) != 1:
        raise DecompError("worktree.arguments", "new takes exactly one lane name")
    return worktree.new(_profile(args.profile), args.tree, args.lane[0], args.at)


def _repin_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tree", type=Path, required=True, help="the tree whose pin places are set (land.trees pin)")
    parser.add_argument("--to", help="the source commit or ref to pin (default: the pin config's ref, HEAD)")
    parser.add_argument("--no-rebuild", action="store_true", help="leave the page port (tas.build.rebuild) unbuilt")
    parser.add_argument("--check", action="store_true", help="name the stale symbols a repin would meet; write nothing")


def _run_repin(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp.repin import repin

    return repin(_profile(args.profile), args.tree, args.to, rebuild=not args.no_rebuild, dry_run=args.check)


def _standalone_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tree", type=Path, required=True, help="a game repo checkout")
    parser.add_argument("--against", help="judge only the lines the work tree adds over this git revision")


def _run_standalone(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp import standalone

    found = standalone.mentions(args.tree, args.against)
    result = {"tree": str(args.tree), "against": args.against, "lines": len(found), "mentions": found}
    return result, int(bool(found)), [standalone.finding(found, "standalone.tool_named")] if found else []


def _behave_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project",
        type=Path,
        help="the project tree to act on (default: the work tree of the cwd, else the profile's project)",
    )
    parser.add_argument("--host", type=Path, help="the unbake host file for that tree (default: the profile's host)")
    parser.add_argument(
        "--function",
        action="append",
        help="the function symbol, as the project names it (--replay: repeat it to limit the batch to some functions)",
    )
    parser.add_argument("--candidate", type=Path, help="candidate C file; without it the original is described")
    parser.add_argument("--version", help="ROM version (default: the project's names_from)")
    parser.add_argument("--cases", type=int, help="input combinations to run, capped (default: profile behave.cases)")
    parser.add_argument("--seed", type=int, default=1, help="seed of the deterministic input set (default 1)")
    parser.add_argument("--depth", type=int, help="calls deep the original callees run; deeper ones are stubs")
    parser.add_argument("--no-capture", action="store_true", help="compare without the oracle's captured entry state")
    parser.add_argument("--lenient-reason", help="lenient memory (unmapped reads zero), with this reason")
    parser.add_argument("--capture-hit", type=int, default=1, help="which entry of the function the capture stops at")
    parser.add_argument(
        "--precapture",
        metavar="LIST",
        help="fill the oracle capture store for many functions in ONE oracle boot, so each later behave on them is "
        "cached: open-nonmatching (every NON_MATCHING body that is a symbol of the version), a file of names, or "
        "names separated by commas",
    )
    parser.add_argument(
        "--replay",
        action="store_true",
        help="batch mode: real calls recorded in the oracle from corpus movies, run again through the port's build of "
        "each function; every difference is a fn-differs line of <tas.runs>/findings.jsonl",
    )
    parser.add_argument("--movie", action="append", help="--replay: a movie to record (repeat; default the corpus)")
    parser.add_argument("--calls", type=int, help="--replay: samples per function (default 3)")
    parser.add_argument("--oracle", type=Path, help="--replay: an oracle host with the recorder (default tas.oracle)")
    parser.add_argument("--port", type=Path, help="--replay: a port executable with replay.c (default tas.port)")
    parser.add_argument("--source", type=Path, help="--replay: the game sources the port was built from")
    parser.add_argument("--refresh", action="store_true", help="--replay: record the movies again")
    parser.add_argument(
        "--cpu-budget",
        type=float,
        help="--replay: CPU seconds the batch may spend (default 300); --precapture: the planning (default 120)",
    )
    parser.add_argument("--no-findings", action="store_true", help="--replay: do not write findings.jsonl")


def _own_tree(args: argparse.Namespace, tool: str) -> Path | None:
    """The tree a command acts on: `--project`, else the work tree of the caller's cwd (tools.cwd_project, the one
    resolution unbake, recipe, make and diff share), else None for the profile's. A lane outside any tree is refused."""
    from abu_decomp.tools import cwd_project

    return cwd_project(_profile(args.profile), tool, [tool], args.project)


def _run_behave(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp.behave.run import Options, run

    if not args.replay:
        args.project = _own_tree(args, "behave")

    if args.replay:
        from abu_decomp.behave import replay

        return replay.run(
            _profile(args.profile),
            replay.Options(
                movies=args.movie or [],
                functions=args.function or [],
                calls=args.calls or replay.CALLS,
                oracle=args.oracle,
                port=args.port,
                source=args.source,
                version=args.version,
                refresh=args.refresh,
                cpu_budget=args.cpu_budget or replay.CPU_BUDGET,
                findings=not args.no_findings,
            ),
        )
    if args.precapture:
        from abu_decomp.behave import precapture

        if args.function or args.candidate:
            raise DecompError(
                "behave.arguments", "--precapture takes its functions from LIST, no --function/--candidate"
            )
        profile = _profile(args.profile)
        with _rekeyed(profile, args):
            return precapture.run(
                profile,
                Options(
                    "",
                    None,
                    args.version,
                    args.cases,
                    args.seed,
                    args.project,
                    args.host,
                    args.depth,
                    hit=args.capture_hit,
                ),
                args.precapture,
                args.cpu_budget or precapture.CPU_BUDGET,
            )
    if not args.function or len(args.function) != 1:
        raise DecompError("behave.arguments", "behave takes exactly one --function (--replay takes any number)")
    options = Options(
        args.function[0],
        args.candidate,
        args.version,
        args.cases,
        args.seed,
        args.project,
        args.host,
        args.depth,
        capture=not args.no_capture,
        hit=args.capture_hit,
        lenient=bool(args.lenient_reason),
        lenient_reason=args.lenient_reason,
    )
    profile = _profile(args.profile)
    with _rekeyed(profile, args):
        return run(profile, options)


def _rekeyed(profile: Profile, args: argparse.Namespace) -> Any:
    """Taste's generated boundaries made from the tree behave acts on (behave/rekey.py)."""
    from abu_decomp.behave import rekey
    from abu_decomp.behave.run import _project_version
    from abu_decomp.tools import settings

    project = settings(profile, args.project, args.host).project
    return rekey.rekeyed(profile, project, args.version or _project_version(project))


def _arity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "verb",
        choices=["census", "show", "gate"],
        help="census: counts and the worst symbols; show SYMBOL: its sites and the asm's argument count; "
        "gate: the land arity gate on the working tree against --base",
    )
    parser.add_argument("symbol", nargs="?", help="show: the function name")
    parser.add_argument(
        "--project", type=Path, help="project tree (default: the work tree of the cwd, else the profile's)"
    )
    parser.add_argument("--base", help="gate: git revision the tree is compared with (default HEAD)")
    parser.add_argument(
        "--top", type=int, default=20, help="census: how many symbols to list (default 20; 0 = every offender)"
    )


def _run_arity(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp import arity

    if args.verb == "show" and not args.symbol:
        raise DecompError("arity.args", "show needs a function name")
    profile = _profile(args.profile)
    result = arity.run(profile, args.verb, args.symbol, _own_tree(args, "arity"), args.base, args.top)
    findings = []
    for r in result.get("refused", []):
        command = arity.next_command(profile.source_file, r["function"], r["kinds"])
        findings.append(
            {
                "key": "land.arity",
                "blocking": True,
                "message": f"{r['function']}: {r['why']}; {r['detail']}; next: {command}",
                "file": r["file"],
                "next": command,
            }
        )
    return result, (1 if findings else 0), findings


def _data_arguments(parser: argparse.ArgumentParser) -> None:
    from abu_decomp.data import command

    command.arguments(parser)


def _run_data(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp.data import command

    return command.run(args)


def _diff_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("paths", nargs="*", help="files or directories whose change is carried")
    parser.add_argument("--onto", required=True, help="revision to rebuild the change on (e.g. github/main)")
    parser.add_argument(
        "--from", dest="base", default="HEAD", help="revision the tree's change is measured from (default HEAD)"
    )
    parser.add_argument("--hunks", help="keep only hunks with a changed line matching this regex")
    parser.add_argument(
        "--function",
        help="keep only this function's hunks: inside its definition or naming it (symbol, size and layout rows too)",
    )
    parser.add_argument("--rename-symbol", help="OLD=NEW: also rename the symbol in every version's symbol_addrs.txt")
    parser.add_argument("--msg", help="written as the first line `# MSG: ...` of the diff")
    parser.add_argument("--out", type=Path, help="write the diff here (default: report only)")
    parser.add_argument(
        "--project", type=Path, help="project tree (default: the work tree of the cwd, else the profile's)"
    )


def _run_diff(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp import diffonto, tools

    profile = _profile(args.profile)
    tree = tools.project_dir(profile, tools.cwd_project(profile, "diff", ["diff"], args.project))
    return (
        diffonto.run(
            tree,
            args.onto,
            args.paths,
            base=args.base,
            hunks=args.hunks,
            function=args.function,
            rename=args.rename_symbol,
            msg=args.msg,
            out=args.out,
        ),
        0,
        [],
    )


def _owes_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project", type=Path, help="project tree (default: the work tree of the cwd, else the profile's)"
    )
    parser.add_argument(
        "--top", type=int, default=10, help="how many functions to list, most reached first (default 10)"
    )


def _run_owes(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp import owes, tas, tas_health, tas_rank

    profile = _profile(args.profile)
    result = owes.run(profile, _own_tree(args, "owes"), args.top)
    try:  # the ranked open-work list over the recorded standing (no replay); owes does not need a tas section
        cfg = tas.settings(profile)
        ranked = tas.ranked_work(profile, cfg, tas_health.read(cfg, profile), tas.standing_rows(cfg), result)
    except DecompError:
        return result, 0, []
    print(tas_rank.render(ranked), file=sys.stderr)
    return {**result, "ranked": ranked, "next": ranked[0]["next"] if ranked else result["next"]}, 0, []


def _cleanup_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "verb",
        choices=["count", "dead-decls", "slices", "ledger", "owners"],
        help="count: placeholder names (func_/D_/unk/pad/spNN/var_/argN) per folder; dead-decls: the declarations of"
        " --dir (a header folder) nothing reads, as layered removal packs; slices: one owner for every placeholder"
        " target (80-100 functions per slice), the lane ownership files the name ledger gates on; ledger: the name"
        " ledger's queue, batches, rows accepted/refused per lane and slice progress (WATCH); owners: the owning"
        " module of every declaration of --dir (definer for externs, majority user for types and macros), as a"
        " move list (--packs DIR writes owners.tsv and owners.json)",
    )
    parser.add_argument(
        "--project", type=Path, help="project tree (default: the work tree of the cwd, else the profile's)"
    )
    parser.add_argument("--dir", help="only this folder and below (src/game/MENU_2, game/MENU_2 or the leaf MENU_2)")
    parser.add_argument("--rev", help="count: the sources of this commit, read from git (no checkout)")
    parser.add_argument("--distinct", action="store_true", help="count: also the number of different names per class")
    parser.add_argument("--hours", type=float, default=1.0, help="ledger: the window the per-lane rates cover")
    parser.add_argument("--book", type=Path, help="ledger: the name book directory (default ~/rig-state/names)")
    parser.add_argument("--top", type=int, default=15, help="folders listed, most placeholders first (0 = every one)")
    parser.add_argument(
        "--packs",
        type=Path,
        help="dead-decls: write one diff per layer here (dead-decls-L<n>.diff); owners: owners.tsv and owners.json",
    )
    parser.add_argument("--apply", action="store_true", help="dead-decls: write layer 0 into the tree")
    parser.add_argument("--lanes", type=int, help="slices: the lane count (GLOBALS and STRUCT slices scale with it)")
    parser.add_argument(
        "--map", help="slices, owners: a module map file (unit<TAB>module lines; default docs/MODULES.tsv)"
    )
    parser.add_argument("--assign", action="append", default=[], metavar="LANE=ID[,ID]", help="slices: LANE owns these")
    parser.add_argument("--names", help="slices: the names directory (default: the name book's store)")
    parser.add_argument(
        "--evidence",
        action="store_true",
        help="per listed folder, how many locals (L), data (N4) and functions (N5) Recipe Maker can name from source"
        " evidence and how many have none (one read-only `recipe name count` for every listed folder)",
    )


def _run_cleanup(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp import cleanup, tools

    if args.verb == "ledger":
        book = args.book or Path("~/rig-state/names").expanduser()
        found = cleanup.ledger(book, args.hours)
        rows = found["rows"]
        print(
            f"ledger rows queued {rows['queued']} inflight {rows['inflight']} landed {rows['landed']} returned"
            f" {rows['returned']}; batches landed {found['batches']['landed']}",
            file=sys.stderr,
        )
        for lane, got in sorted(found["lanes"].items()):
            print(
                f"  {lane}: accepted {got['accepted']} refused {got['refused']} ({got['rows_per_hour']}/h)"
                f" {got['by_rule']}",
                file=sys.stderr,
            )
        for lane, got in sorted(found["slices"].items()):
            print(
                f"  slice {got['slice']} ({lane}): {got['named_fns']}/{got['owned_fns']} fns have a row",
                file=sys.stderr,
            )
        return found, 0, []
    root = tools.project_dir(_profile(args.profile), _own_tree(args, "cleanup"))
    if args.verb == "slices":
        argv = cleanup.slices_argv(args.lanes, args.map, args.assign, args.names)
        called = tools.invoke("recipe", _profile(args.profile), argv, project=root)
        if not called.ok:
            why = "; ".join(str(f.get("message", f)) for f in called.document.get("findings", [])) or "recipe refused"
            raise DecompError("cleanup.slices", f"recipe name slices failed: {why}")
        for line in cleanup.slices_lines(called.document):
            print(line, file=sys.stderr)
        return dict(called.document), 0, []
    if args.verb == "owners":
        from abu_decomp import owners

        if not args.dir:
            raise DecompError("cleanup.dir", "owners needs --dir (the header folder, e.g. include/COMMON)")
        found = owners.run(root, args.dir, Path(args.map) if args.map else None, args.packs)
        print(
            f"owners {found['declarations']} declarations in {found['headers']} headers: {found['owned']} owned"
            f" ({found['no_header']} of them by a module with no header yet), {found['shared']} shared,"
            f" {found['unused']} unused",
            file=sys.stderr,
        )
        for module, n in list(found["by_owner"].items())[: args.top or None]:  # type: ignore[attr-defined]
            print(f"  {n:>6}  {module}", file=sys.stderr)
        summary = {k: v for k, v in found.items() if k not in ("rows", "by_owner")}
        summary["top_owners"] = dict(list(found["by_owner"].items())[:20])  # type: ignore[attr-defined]
        return summary | ({"packs": str(args.packs)} if args.packs else {}), 0, []
    if args.verb == "dead-decls":
        from abu_decomp import deaddecls

        if not args.dir:
            raise DecompError("cleanup.dir", "dead-decls needs --dir (the header folder, e.g. include/COMMON)")
        found = deaddecls.run(root, args.dir, args.packs, args.apply)
        sizes = [row["declarations"] for row in found["layers"]]  # type: ignore[attr-defined]
        print(f"dead-decls {found['dead']} in {found['headers']} headers, layers {sizes}", file=sys.stderr)
        return found, 0, []
    rev = getattr(args, "rev", None)  # a hand-made Namespace (tests, other verbs) has none
    if rev and args.evidence:
        raise DecompError("cleanup.rev", "--evidence reads a built work tree: count --rev without it")
    try:
        got = cleanup.count(root, args.dir, rev, getattr(args, "distinct", False))
    except ValueError as e:
        raise DecompError("cleanup.rev", str(e)) from None
    rows = sorted(got["folders"].items(), key=lambda kv: (-kv[1]["count"], kv[0]))
    shown = rows if args.top == 0 else rows[: args.top]
    print(f"placeholders {got['total']} " + " ".join(f"{k}={v}" for k, v in got["classes"].items()), file=sys.stderr)
    if "distinct" in got:
        print(
            f"distinct {got['distinct']['total']} "
            + " ".join(f"{k}={v}" for k, v in got["distinct"]["classes"].items()),
            file=sys.stderr,
        )
    found: dict[str, Any] = {}
    if args.evidence:
        profile = _profile(args.profile)

        def ask(steps: list[str], targets: list[str]) -> dict[str, Any]:
            called = tools.invoke(
                "recipe", profile, ["name", "count", "--steps", ",".join(steps), "--targets", *targets], project=root
            )
            return dict(called.document) if called.ok else {}

        found = cleanup.evidence(dict(shown), ask)
    for folder, row in shown:
        names = " ".join(f"{what}={v['namable']}/{v['no_evidence']}" for what, v in found.get(folder, {}).items())
        print(f"  {row['count']:>6}  {folder}" + (f"  namable/no-evidence: {names}" if names else ""), file=sys.stderr)
    return (
        {
            **got,
            "folders": {k: {**v, **({"evidence": found[k]} if k in found else {})} for k, v in shown},
            "folder_count": len(rows),
        },
        0,
        [],
    )


def _sweep_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--rules", action="append", type=Path, required=True, metavar="FILE", help="a rules TOML file (repeat)"
    )
    parser.add_argument(
        "--tree", action="append", default=[], metavar="LABEL=DIR", help="a tree the rules read (repeat)"
    )
    parser.add_argument("--rule", action="append", default=[], metavar="ID", help="run only this rule (repeat)")
    parser.add_argument("--baseline", type=Path, help="hits listed in this file are known; the rest are new")
    parser.add_argument("--update-baseline", action="store_true", help="rewrite --baseline with every hit now")
    parser.add_argument("--findings", action="store_true", help="append the hits to the profile's findings feed")
    parser.add_argument("--show", type=int, default=5, help="new hits listed per rule (default 5)")
    parser.add_argument("--strict", action="store_true", help="exit 1 on any new hit, not only a block rule's")


def _run_sweep(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp import sweep, tas_findings

    trees = {}
    for item in args.tree:
        label, sep, directory = item.partition("=")
        if not sep or not label or not directory:
            raise DecompError("sweep.tree", f"--tree takes LABEL=DIR, not {item!r}")
        trees[label] = Path(directory).expanduser().resolve()
    rules = sweep.load_rules(args.rules)
    result = sweep.run(rules, trees, only=args.rule, baseline=args.baseline)
    if args.update_baseline:
        if args.baseline is None:
            raise DecompError("sweep.baseline", "--update-baseline needs --baseline FILE")
        result["baseline_written"] = sweep.write_baseline(args.baseline, result["hits"])
    if args.findings:
        profile = _profile(args.profile)
        runs = profile.path("tas.runs", profile.section("tas")["runs"])
        result["findings_added"] = tas_findings.append(runs, sweep.to_findings(result, trees))
    print(sweep.render(result, args.show), file=sys.stderr)
    failing = [h for h in result["hits"] if h["new"] and (args.strict or h["severity"] == "block")]
    findings = [
        {
            "key": f"sweep.{h['rule']}",
            "blocking": True,
            "message": f"{h['tree']}/{h['file']}:{h['line']}: {h['text'][:120]}",
            "file": h["file"],
            "next": f"sed -n '{max(1, h['line'] - 3)},{h['line'] + 6}p' {trees[h['tree']] / h['file']}",
        }
        for h in failing[:50]
    ]
    result["hits_total"] = len(result["hits"])
    result["hits"] = [h for h in result["hits"] if h["new"]][:200]  # the envelope lists new hits; the feed has all
    return result, int(bool(failing)), findings


def _claims_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "verb", choices=["check"], help="check: validate that a text's result claims cite recorded runs"
    )
    parser.add_argument("file", type=Path, help="a text file (commit message, inbox or RESPONSE entry)")


def _run_claims(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp import claims

    report = claims.check(_profile(args.profile), args.file.read_text(errors="replace"))
    findings = [
        {
            "key": "claims.uncited",
            "message": claims.refusal_text(p),
            "blocking": True,
            "line": p["line"],
            "next": p["next"],
        }
        for p in report["problems"]
    ]
    return {"file": str(args.file), **report}, (1 if findings else 0), findings


def _phase_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("action", nargs="?", choices=["timelapse"], help="timelapse: make the version mp4")
    parser.add_argument("--fps", type=int, default=12, help="timelapse frame rate (default 12)")
    parser.add_argument("--game", help="only this game of phase.games")
    parser.add_argument("--version", help="only this version (default: the focus version, else the game's first)")
    parser.add_argument("--refresh", action="store_true", help="write phase.out and append a phase.history line")


def _run_phase(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp import phase

    if args.action == "timelapse":
        from abu_decomp import tas, tas_phasemap, tas_timelapse

        profile = _profile(args.profile)
        version = args.version or tas_phasemap.Phase(profile, tas.settings(profile), None, None).focus()
        if not version:
            raise DecompError("phase.version", "pass --version (the profile has no phase focus)")
        return tas_timelapse.make(tas.settings(profile)["runs"], version, args.fps), 0, []
    result, findings = phase.run(_profile(args.profile), args.game, args.version, args.refresh)
    return result, 0, findings


def _served_elsewhere(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    raise DecompError("internal.dispatch", f"{args.command} is served by main, not by a command handler")


def _run_describe(args: argparse.Namespace) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    return describe(), 0, []


COMMANDS: dict[str, Command] = {
    "profile": Command("show the loaded seat profile", _none, _run_profile),
    "dependencies": Command("check every pinned tool is installed at its commit", _none, _run_dependencies),
    "unbake": Command("run unbake on the profile's project (or --project)", _tool_arguments, _run_tool),
    "recipe": Command("run Recipe Maker on the profile's project (or --project)", _tool_arguments, _run_tool),
    "make": Command(
        "proof-build the profile's project (or --project) through the build pool", _tool_arguments, _run_make
    ),
    "taste": Command("run Cake Taste with the profile's Taste profile", _tool_arguments, _run_tool),
    "event": Command("append, tail and validate the event feed", _event_arguments, _run_event),
    "tools": Command("deploy the pinned tools into venvs", _tools_arguments, _run_tools),
    "lane": Command("launch, register, steer and end AI lanes", _lane_arguments, _run_lane),
    "supervisor": Command("supervise a role headless, or stage an artifact", _supervisor_arguments, _run_supervisor),
    "build": Command("build committed sources and promote verified releases", _build_arguments, _run_build),
    "probe": Command("queue oracle probe requests and join them to PROBE events", _probe_arguments, _run_probe),
    "cycle": Command("rerun Recipe and unbake report whenever the decomp sources settle", _none, _run_cycle),
    "port": Command("generate a port repository skeleton on the n64 shim", _port_arguments, _run_port),
    "standalone": Command(
        "list the lines of a game repo (or those it adds over --against) that name one of our tools",
        _standalone_arguments,
        _run_standalone,
    ),
    "ship": Command(
        "land every ready input on the warm tree in one round, commit each and push fast-forward to the branch",
        _ship_arguments,
        _run_ship,
    ),
    "worktree": Command(
        "make a lane's own work tree, seeded warm from the shared tree (extraction, objects, times)",
        _worktree_arguments,
        _run_worktree,
    ),
    "repin": Command(
        "set a tree's pin of another repository's commit in every place it is spelled, ready for ship --worktree",
        _repin_arguments,
        _run_repin,
    ),
    "behave": Command(
        "run the original and a candidate on the same inputs and compare their behaviour",
        _behave_arguments,
        _run_behave,
    ),
    "arity": Command(
        "census of every function's parameter list: definition vs declarations, calls and the original's assembly",
        _arity_arguments,
        _run_arity,
    ),
    "data": Command(
        "data typing of raw globals: census (classes + work queue), probe, rewrite, check, gate",
        _data_arguments,
        _run_data,
    ),
    "diff": Command(
        "rebuild a lane tree's change to PATHs on another revision (3-way, scratch index) as a train diff",
        _diff_arguments,
        _run_diff,
    ),
    "owes": Command(
        "functions that owe a match (NON_MATCHING bodies, reached INCLUDE_ASM) by evidence level, most reached first",
        _owes_arguments,
        _run_owes,
    ),
    "cleanup": Command(
        "placeholder-name counts (func_/D_/unk/pad/spNN/var_/argN) per folder: the one number lanes and WATCH share",
        _cleanup_arguments,
        _run_cleanup,
    ),
    "sweep": Command(
        "find the siblings of fixed root causes: project rules (TOML) over source trees, new hits vs a baseline",
        _sweep_arguments,
        _run_sweep,
    ),
    "claims": Command(
        "check that a text's result claims (fixed, behaves, ...) cite runs whose recorded outcome supports them",
        _claims_arguments,
        _run_claims,
    ),
    "tas": Command("record, save, list and replay controller movies against the port", _tas_arguments, _run_tas),
    "phase": Command(
        "print every phase gate (map, foundations, port/oracle, coverage, release ladder) with the next work items",
        _phase_arguments,
        _run_phase,
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
    """`[--profile P] unbake|recipe|taste [--] [--project D] [--host F] ARGS...`, with ARGS untouched (argparse would
    eat --help and --version); None when the line is anything else."""
    from abu_decomp.tools import TOOLS

    rest, profile = list(args), None
    if rest[:1] == ["--profile"] and len(rest) > 1:
        profile, rest = Path(rest[1]), rest[2:]
    elif rest[:1] and rest[0].startswith("--profile="):
        profile, rest = Path(rest[0].partition("=")[2]), rest[1:]
    if not rest or (rest[0] not in TOOLS and rest[0] != "make"):
        return None
    found: dict[str, Path | None] = {"project": None, "host": None}
    command, rest = rest[0], rest[1:]
    if rest[:1] == [
        "--"
    ]:  # `unbake -- --project T cmd` is `unbake --project T cmd`: the separator only ends decomp's flags
        rest = rest[1:]
    while rest:
        flag, eq, value = rest[0].partition("=")
        if flag not in ("--project", "--tree", "--host"):
            break
        flag = "--project" if flag == "--tree" else flag
        if eq:
            rest = rest[1:]
        elif len(rest) > 1:
            value, rest = rest[1], rest[2:]
        else:
            raise DecompError("cli.arguments", f"{flag} expects a value")
        found[flag[2:]] = Path(value)
    return argparse.Namespace(profile=profile, command=command, project=found["project"], host=found["host"], args=rest)


def _record_claim(profile: Path | None, invocation: str, command: str, ok: bool, result: Any, argv: list[str]) -> None:
    """Persist the run so a later claim can cite its invocation id; never fails the command."""
    try:
        from abu_decomp import claims

        claims.record(load_profile(profile) if profile else None, invocation, command, ok, result, argv)
    except Exception:  # recording is best effort
        return


def _own_version(args: list[str]) -> bool:
    return args[-1:] == ["--version"] and (len(args) == 1 or (len(args) == 3 and args[0] == "--profile"))


def _refusal_lines(findings: list[dict[str, Any]]) -> None:
    """Each blocking finding on stderr: what refused and the one command to run next (never only in JSON)."""
    for finding in findings:
        if finding.get("blocking"):
            why = finding.get("message") or finding.get("reason") or "no reason given"  # a recipe finding says `reason`
            sys.stderr.write(f"decomp: refused {finding.get('key')}: {why}\n")
            if nxt := finding.get("next") or finding.get("action"):
                sys.stderr.write(f"decomp: next: {nxt}\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if _own_version(args):
        from abu_decomp.tools import version_text

        sys.stdout.write(version_text())
        return 0
    if argv is None:  # a real start, not a call from a test or from `decomp mcp`'s own dispatch
        priority.apply(args)  # before the slice exec, which keeps it
        runslice.start(args)
    started = time.monotonic()
    invocation = uuid.uuid4().hex
    result: dict[str, Any] | None = None
    findings: list[dict[str, Any]] = []
    code = 0
    command = "parse"
    parsed_profile: Path | None = None
    try:
        parsed = _tool_call(args) or parser().parse_args(args)
        command = parsed.command
        parsed_profile = parsed.profile
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
    except KeyboardInterrupt as exc:
        code = 128 + getattr(exc, "signum", 2)
        findings = [{"key": "command.interrupted", "message": str(exc) or "SIGINT", "blocking": True}]
    except SystemExit as exc:
        # argparse help is the only plain-text stdout path.
        if exc.code == 0:
            return 0
        # tools._on_terminate turns SIGTERM into SystemExit(128+signal): an external cap or kill, not a bad argument
        if isinstance(exc.code, int) and 128 < exc.code < 160:
            spent = time.monotonic() - started
            name = signal.Signals(exc.code - 128).name
            message = f"TIMEOUT or kill: {command} stopped by {name} after {spent:.0f} s (external cap)"
            code, findings = exc.code, [{"key": "command.terminated", "message": message, "blocking": True}]
        else:
            message = f"{command} rejected its arguments"
            code, findings = 2, [{"key": "cli.arguments", "message": message, "blocking": True}]
    except Exception as exc:
        sys.stderr.write(traceback.format_exc())
        code, findings = 3, [{"key": "internal.error", "message": repr(exc), "blocking": True}]
    _record_claim(parsed_profile, invocation, command, code == 0, result, args)
    envelope = {
        "ok": code == 0,
        "result": result,
        "findings": findings,
        "stages": [{"name": command, "wall_seconds": round(time.monotonic() - started, 6)}],
        "invocation": invocation,
    }
    sys.stdout.write(json.dumps(envelope, sort_keys=True, default=str) + "\n")
    sys.stdout.flush()
    _refusal_lines(findings)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
