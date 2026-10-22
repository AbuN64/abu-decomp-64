"""`decomp tas record|save|list|show|delete|play`: controller movies replayed against the port.

Section `tas`: {"movies", "runs", "port", "args", "rom", "env", "exit_after", "timeout_seconds", "gdb"}.
`movies` is the folder of named movies (kept in a repository), `runs` holds recordings and replay runs, `port` is
the game executable and `args` its arguments, where the token `{user_dir}` becomes a fresh folder per run so saves
never leak between runs. `rom` is the image whose SHA-1 a movie names; replay refuses another one.

The port speaks only the raw form, one `BUTTONS STICK_X STICK_Y` line per guest controller poll (hex buttons): it
replays `N64C_TAS_PLAY`, appends every poll to `N64C_TAS_RECORD`, and exits 0 `N64C_TAS_EXIT_AFTER` polls after the
movie. A movie (`NAME.tas`) is the readable form of the same polls: `#` header lines, then one line per run of equal
polls, `BUTTONS [X,Y] [*COUNT]`, where BUTTONS is `-` or names joined by `+` (`A+START`) and the stick is omitted
when centred. Replay keys input to the poll index, so a movie is the same run at any host speed.

A replay runs under the supervisor's gdb bridge with the port's stop function as the breakpoint, so a stop or a
host fault leaves the failing stack and every thread's stack in the run folder. The outcome is one of `ok`,
`stop` (the port's own boot stop, with its subject), `crash` (a host fault or signal), `hang` (no exit within
`timeout_seconds`) or `exit` (any other exit code).
"""

from __future__ import annotations

import hashlib
import os
import re
import resource
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, tas_capture, tas_health, tas_rank, tas_symbolize
from abu_decomp.config import Profile, check_keys

SCHEMA = 1
BUTTONS = (
    ("A", 0x8000),
    ("B", 0x4000),
    ("Z", 0x2000),
    ("START", 0x1000),
    ("UP", 0x0800),
    ("DOWN", 0x0400),
    ("LEFT", 0x0200),
    ("RIGHT", 0x0100),
    ("L", 0x0020),
    ("R", 0x0010),
    ("CUP", 0x0008),
    ("CDOWN", 0x0004),
    ("CLEFT", 0x0002),
    ("CRIGHT", 0x0001),
)
_BIT = dict(BUTTONS)
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_RAW = re.compile(r"([0-9a-fA-F]{1,4}) (-?\d{1,3}) (-?\d{1,3})")
_KEYS = frozenset(
    {
        "movies",
        "runs",
        "port",
        "args",
        "rom",
        "env",
        "exit_after",
        "timeout_seconds",
        "gdb",
        "serve",
        "build",
        "replay",
        "rank",
    }
)
_REQUIRED = frozenset({"movies", "runs", "port", "args", "rom"})
STOP_FUNCTION = "n64c_boot_stopped"
STOP_SUBJECT = "*(char**)&n64c_boot_stop_symbol"

Poll = tuple[int, int, int]


def _build(profile: Profile, table: Any) -> dict[str, Any]:
    if not table:
        return {}
    check_keys(profile.source_file, "tas.build", table, frozenset(), tas_health.BUILD_KEYS)
    out = {"rebuild": str(table.get("rebuild", ""))}
    for key in ("units", "sources"):
        if key in table:
            out[key] = profile.path(f"tas.build.{key}", table[key])
    return out


def settings(profile: Profile) -> dict[str, Any]:
    table = profile.section("tas")
    check_keys(profile.source_file, "tas", table, _REQUIRED, _KEYS)
    return {
        "movies": profile.path("tas.movies", table["movies"]),
        "runs": profile.path("tas.runs", table["runs"]),
        "port": profile.path("tas.port", table["port"]),
        "args": [str(arg) for arg in table["args"]],
        "rom": profile.path("tas.rom", table["rom"]),
        "env": {str(k): str(v) for k, v in table.get("env", {}).items()},
        "exit_after": int(table.get("exit_after", 120)),
        "timeout_seconds": int(table.get("timeout_seconds", 300)),
        "gdb": str(table.get("gdb", "gdb")),
        "build": _build(profile, table.get("build")),
        "replay": _replay(profile, table.get("replay")),
        "rank": tas_rank.settings(table.get("rank")),
    }


def _replay(profile: Profile, table: Any) -> dict[str, Any]:
    """Section `tas.replay`: the cheap mode `play --all` uses: {args: port arguments (default tas.args), view: false}.

    The picture a replay is rendered at costs most of its CPU and the outcome never reads it, so the cheap mode runs
    the same port at a tiny window with no capture. A watched `tas play NAME` keeps `tas.args` and its view."""
    table = table or {}
    check_keys(profile.source_file, "tas.replay", table, frozenset(), frozenset({"args", "view"}))
    args = [str(a) for a in table["args"]] if "args" in table else None
    return {"args": args, "view": bool(table.get("view", False))}


def parse_raw(text: str) -> list[Poll]:
    polls = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        match = _RAW.fullmatch(line.strip())
        if not match:
            raise DecompError("tas.raw", f"line {number}: expected 'BUTTONS X Y', got {line!r}")
        polls.append((int(match[1], 16), int(match[2]), int(match[3])))
    return polls


def read_raw(path: Path) -> list[Poll]:
    """The polls of a recording the port may still be writing: a last line without its newline is left out."""
    text = path.read_text()
    return parse_raw(text[: text.rfind("\n") + 1])


def format_raw(polls: list[Poll]) -> str:
    return "".join(f"{b:04x} {x} {y}\n" for b, x, y in polls)


def _buttons_text(bits: int) -> str:
    names = [name for name, bit in BUTTONS if bits & bit]
    if bits & ~sum(_BIT.values()):
        raise DecompError("tas.buttons", f"poll has bits outside the controller's buttons: {bits:#06x}")
    return "+".join(names) or "-"


def encode(polls: list[Poll], header: dict[str, str]) -> str:
    lines = [f"# tas {SCHEMA}"] + [f"# {key} {value}" for key, value in header.items()]
    index = 0
    while index < len(polls):
        end = index
        while end + 1 < len(polls) and polls[end + 1] == polls[index]:
            end += 1
        bits, x, y = polls[index]
        line = _buttons_text(bits) + (f" {x},{y}" if (x, y) != (0, 0) else "")
        count = end - index + 1
        lines.append(line + (f" *{count}" if count > 1 else ""))
        index = end + 1
    return "\n".join(lines) + "\n"


def decode(text: str) -> tuple[dict[str, str], list[Poll]]:
    header: dict[str, str] = {}
    polls: list[Poll] = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            key, _, value = line[1:].strip().partition(" ")
            header[key] = value.strip()
            continue
        fields = line.split()
        count, x, y = 1, 0, 0
        if fields[-1].startswith("*"):
            count = int(fields.pop()[1:])
        if len(fields) == 2:
            x_text, _, y_text = fields.pop().partition(",")
            x, y = int(x_text), int(y_text)
        if len(fields) != 1 or count < 1 or not -128 <= x <= 127 or not -128 <= y <= 127:
            raise DecompError("tas.movie", f"line {number}: expected 'BUTTONS [X,Y] [*COUNT]', got {line!r}")
        bits = 0
        for name in fields[0].split("+") if fields[0] != "-" else []:
            if name not in _BIT:
                raise DecompError("tas.movie", f"line {number}: unknown button {name!r}")
            bits |= _BIT[name]
        polls.extend([(bits, x, y)] * count)
    if header.get("tas") != str(SCHEMA):
        raise DecompError("tas.movie", f"movie must start with '# tas {SCHEMA}'")
    return header, polls


def _sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _movie(cfg: dict[str, Any], name: str) -> Path:
    if not _NAME.fullmatch(name):
        raise DecompError("tas.name", f"movie names are lower-case words joined by '-', not {name!r}")
    return cfg["movies"] / f"{name}.tas"


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")


def recordings(cfg: dict[str, Any]) -> list[Path]:
    folder = cfg["runs"] / "live"
    return sorted(folder.glob("*.raw")) if folder.is_dir() else []


def save(
    profile: Profile, name: str, *, source: Path | None, note: str, replace: bool, now: datetime | None = None
) -> dict[str, Any]:
    cfg = settings(profile)
    path = _movie(cfg, name)
    if path.exists() and not replace:
        raise DecompError("tas.save", f"{path.name} exists; pass --replace to overwrite it")
    if source is None:
        live = recordings(cfg)
        if not live:
            raise DecompError("tas.save", f"no recording under {cfg['runs'] / 'live'}")
        source = live[-1]
    text = source.read_text()
    polls = parse_raw(text[: text.rfind("\n") + 1])  # a live recording may end in a half-written line
    if not polls:
        raise DecompError("tas.save", f"{source} holds no polls")
    header = {"rom-sha1": _sha1(cfg["rom"]), "recorded": _stamp(now), "polls": str(len(polls))}
    if note:
        header["note"] = " ".join(note.split())
    cfg["movies"].mkdir(parents=True, exist_ok=True)
    path.write_text(encode(polls, header))
    return {"movie": str(path), "name": name, "polls": len(polls), "source": str(source)}


def listing(profile: Profile) -> dict[str, Any]:
    cfg = settings(profile)
    rows = []
    for path in sorted(cfg["movies"].glob("*.tas")) if cfg["movies"].is_dir() else []:
        try:
            header, polls = decode(path.read_text())
        except DecompError as error:
            rows.append({"name": path.stem, "error": str(error)})
            continue
        rows.append(
            {
                "name": path.stem,
                "polls": len(polls),
                "seconds": round(len(polls) / 60, 1),
                "note": header.get("note", ""),
                "recorded": header.get("recorded", ""),
            }
        )
    live = [{"path": str(p), "polls": len(read_raw(p))} for p in recordings(cfg)[-5:]]
    return {"movies": rows, "recordings": live}


def show(profile: Profile, name: str) -> dict[str, Any]:
    path = _movie(settings(profile), name)
    if not path.is_file():
        raise DecompError("tas.show", f"no movie {path}")
    text = path.read_text()
    header, polls = decode(text)
    return {"name": name, "header": header, "polls": len(polls), "text": text}


def delete(profile: Profile, name: str) -> dict[str, Any]:
    path = _movie(settings(profile), name)
    if not path.is_file():
        raise DecompError("tas.delete", f"no movie {path}")
    path.unlink()
    return {"deleted": str(path)}


def _argv(cfg: dict[str, Any], user_dir: Path) -> list[str]:
    return [str(cfg["port"])] + [arg.replace("{user_dir}", str(user_dir)) for arg in cfg["args"]]


def supervised(cfg: dict[str, Any], run_dir: Path, argv: list[str]) -> list[str]:
    """The port under the supervisor's gdb bridge: a stop or host fault leaves its stacks in run_dir."""
    return [
        sys.executable,
        "-m",
        "abu_decomp.supervisor.gdb",
        "--gdb",
        cfg["gdb"],
        "--breakpoint",
        STOP_FUNCTION,
        "--reason-expression",
        STOP_SUBJECT,
        "--run-dir",
        str(run_dir),
        "--",
        *argv,
    ]


def record(profile: Profile, *, now: datetime | None = None, extra_env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run the port with live input and log every poll; returns when the port exits."""
    cfg = settings(profile)
    stamp = _stamp(now)
    run = cfg["runs"] / "live"
    user_dir = cfg["runs"] / "users" / stamp
    user_dir.mkdir(parents=True, exist_ok=True)
    log = run / f"{stamp}.raw"
    run.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **cfg["env"], **(extra_env or {}), "N64C_TAS_RECORD": str(log)}
    for key in ("N64C_TAS_PLAY", "N64C_TAS_EXIT_AFTER"):
        env.pop(key, None)
    code = subprocess.run(_argv(cfg, user_dir), env=env, check=False).returncode
    polls = len(parse_raw(log.read_text())) if log.is_file() else 0
    return {"recording": str(log), "polls": polls, "exit": code}


def classify(code: int | None, output: str) -> dict[str, Any]:
    # The bridge names the subject when it held the stop; stops that end the process directly only print it.
    reason = re.search(r"^debug-reason: (.+)$", output, re.M) or re.search(r"^boot-missing: (\S+)$", output, re.M)
    signal_name = re.search(r"^debug-signal: (\S+)$", output, re.M)
    if code is None:
        return {"outcome": "hang"}
    if reason:
        subject = reason[1].strip()
        return {"outcome": "crash" if subject.startswith("host-fault-") else "stop", "subject": subject}
    if signal_name:
        return {"outcome": "crash", "subject": signal_name[1]}
    if code == 0 and "n64c-tas: done" in output:
        return {"outcome": "ok"}
    return {"outcome": "exit", "subject": f"exit {code}"}


def movie_polls(cfg: dict[str, Any], name: str) -> list[Poll]:
    """A saved movie's polls, refused when it was recorded on another ROM."""
    path = _movie(cfg, name)
    if not path.is_file():
        raise DecompError("tas.play", f"no movie {path}")
    header, polls = decode(path.read_text())
    rom = _sha1(cfg["rom"])
    if header.get("rom-sha1") != rom:
        raise DecompError("tas.play", f"{path.name} was recorded on ROM {header.get('rom-sha1')}, not {rom}")
    return polls


def _wait(process: subprocess.Popen, timeout: float) -> tuple[int | None, float]:
    """(exit code or None after a kill at the timeout, CPU seconds of the run's whole process tree).

    wait4 returns the rusage of the child and every descendant it reaped, so one replay's CPU is its own even while
    other replays run in this process."""
    end = time.monotonic() + timeout
    while True:
        pid, status, usage = os.wait4(process.pid, os.WNOHANG)
        if pid:
            process.returncode = os.waitstatus_to_exitcode(status)
            return process.returncode, round(usage.ru_utime + usage.ru_stime, 1)
        if time.monotonic() > end:
            os.killpg(process.pid, signal.SIGKILL)
            _, status, usage = os.wait4(process.pid, 0)
            process.returncode = os.waitstatus_to_exitcode(status)
            return None, round(usage.ru_utime + usage.ru_stime, 1)
        time.sleep(0.05)


def play(
    profile: Profile,
    name: str,
    *,
    now: datetime | None = None,
    health: dict[str, Any] | None = None,
    cheap: bool = False,
) -> dict[str, Any]:
    """Replay one movie. `cheap` (what `play --all` sets) uses `tas.replay`: its args and no display capture."""
    cfg = settings(profile)
    began = time.monotonic()
    if cheap and cfg["replay"]["args"] is not None:
        cfg = {**cfg, "args": cfg["replay"]["args"]}
    health = tas_health.read(cfg) if health is None else health
    polls = movie_polls(cfg, name)
    run = cfg["runs"] / "plays" / f"{_stamp(now)}-{name}"
    user_dir = run / "user"
    user_dir.mkdir(parents=True)
    (run / "movie.raw").write_text(format_raw(polls))
    env = {
        **os.environ,
        **cfg["env"],
        "N64C_TAS_PLAY": str(run / "movie.raw"),
        "N64C_TAS_RECORD": str(run / "polls.raw"),
        "N64C_TAS_EXIT_AFTER": str(cfg["exit_after"]),
    }
    argv = supervised(cfg, run, _argv(cfg, user_dir))
    # Its own display and sound card, so the page can show it and parallel plays never share a screen.
    view = None if cheap and not cfg["replay"]["view"] else tas_capture.PlayView.open(cfg, run, name, _stamp(now))
    if view:
        env.update(view.env())
    try:
        with (run / "output.txt").open("w") as output:
            process = subprocess.Popen(
                argv, env=env, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT, start_new_session=True
            )
            # The bridge holds a stopped port until its stdin says "continue"; a closed pipe ends it after the stacks.
            process.stdin.close()
            code, cpu = _wait(process, cfg["timeout_seconds"])
    except BaseException:
        if view:
            view.finish({"outcome": "died"})
        raise
    text = (run / "output.txt").read_text(errors="replace")
    reached = len(parse_raw((run / "polls.raw").read_text())) if (run / "polls.raw").is_file() else 0
    result = {"movie": name, "run": str(run), "polls": len(polls), "polls_reached": reached, **classify(code, text)}
    result["wall_seconds"], result["cpu_seconds"] = round(time.monotonic() - began, 1), cpu
    if view:
        view.finish(result)
    stacks = [str(p) for p in sorted(run.glob("*-st*.txt")) + sorted(run.glob("*-threads.txt"))]
    if stacks:
        result["stacks"] = stacks
    # Kind, blocking, what cannot help, owner, arity note and one next command (the arity note comes first when set).
    result.update(tas_symbolize.claim(profile, result, stacks, health))
    if health:
        result["build"] = tas_health.header(health)
    return result


def compare(recorded: dict[str, Any] | None, now: dict[str, Any]) -> str:
    """better / same / worse / new: a clean end beats any stop, then more polls reached beats fewer."""
    if not recorded:
        return "new"

    def standing(row: dict[str, Any]) -> tuple[int, int]:
        return (int(row.get("outcome") == "ok"), int(row.get("polls_reached") or 0))

    before, after = standing(recorded), standing(now)
    return "worse" if after < before else "better" if after > before else "same"


def table(result: dict[str, Any]) -> str:
    """The `play --all` result as aligned text: the build header, then one row per movie."""
    build = result.get("build") or {}
    lines = [build.get("summary", "build: not configured")]
    lines += [f"  failed {row['unit']}: {row['error']}" for row in build.get("failed", [])]
    if build.get("stale"):
        lines.append(f"  stale: {build['stale']}; next: {build.get('rebuild', '')}")
    head = ("movie", "outcome", "polls", "vs recorded", "wall/cpu s", "next")
    rows = [
        (
            r["movie"],
            r["outcome"] + (f" {r['subject']}" if r["subject"] else ""),
            f"{r['polls_reached']}/{r['polls']}",
            r["vs_recorded"],
            f"{r.get('wall_seconds', 0)}/{r.get('cpu_seconds', 0)}",
            r["next"],
        )
        for r in result["movies"]
    ]
    widths = [max(len(str(x[i])) for x in [head, *rows]) for i in range(5)]
    for row in [head, *rows]:
        lines.append("  ".join(str(c).ljust(w) for c, w in zip(row[:5], widths, strict=True)) + "  " + row[5])
    if "wall_seconds" in result:
        lines.append(f"wall {result['wall_seconds']} s, cpu {result['cpu_seconds']} s (children)")
    if "ranked" in result:
        lines.append(tas_rank.render(result["ranked"]))
    return "\n".join(lines)


def play_all(profile: Profile, *, jobs: int = 4, now: datetime | None = None) -> tuple[dict[str, Any], int]:
    """Replay every saved movie in parallel, block until done, one row per movie against its recorded standing.

    The recorded standing is the best row so far in `runs/standing.json`, moved only when a movie is not worse, so a
    regression stays `worse` until fixed. Exit status 1 when any movie is worse."""
    import json
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cfg = settings(profile)
    names = sorted(p.stem for p in cfg["movies"].glob("*.tas")) if cfg["movies"].is_dir() else []
    health = tas_health.read(cfg)
    began, cpu0 = time.monotonic(), resource.getrusage(resource.RUSAGE_CHILDREN)

    def one(name: str) -> dict[str, Any]:
        try:
            return play(profile, name, now=now, health=health, cheap=True)
        except DecompError as error:
            return {"movie": name, "outcome": "refused", "subject": str(error), "polls_reached": 0}

    def progress(row: dict[str, Any]) -> None:
        reached = f"{row.get('polls_reached', 0)}/{row.get('polls', 0)}"
        outcome = row["outcome"] + (f" {row['subject']}" if row.get("subject") else "")
        sys.stderr.write(
            f"tas: {row['movie']} {outcome} {reached} "
            f"{row.get('wall_seconds', 0)}/{row.get('cpu_seconds', 0)} s (wall/cpu)\n"
        )
        sys.stderr.flush()

    done: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(jobs, len(names) or 1))) as pool:
        for future in as_completed([pool.submit(one, name) for name in names]):
            row = future.result()
            progress(row)  # one row per movie as each finishes; the table and JSON below are unchanged
            done[row["movie"]] = row
    results = [done[name] for name in names]
    path = cfg["runs"] / "standing.json"
    try:
        standing = json.loads(path.read_text())
    except (OSError, ValueError):
        standing = {}
    rows = []
    for result in results:
        recorded = standing.get(result["movie"])
        verdict = compare(recorded, result)
        if verdict != "worse":
            standing[result["movie"]] = {
                k: result.get(k) for k in ("outcome", "subject", "polls_reached", "kind", "next", "owner")
            }
        rows.append(
            {
                "movie": result["movie"],
                "outcome": result["outcome"],
                "subject": result.get("subject", ""),
                "polls_reached": result.get("polls_reached", 0),
                "polls": result.get("polls", 0),
                "recorded": recorded,
                "vs_recorded": verdict,
                "kind": result.get("kind", ""),
                "next": result.get("next", ""),
                "owner": result.get("owner", ""),
                "run": result.get("run", ""),
                "wall_seconds": result.get("wall_seconds", 0),
                "cpu_seconds": result.get("cpu_seconds", 0),
            }
        )
    cpu1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = round(cpu1.ru_utime + cpu1.ru_stime - cpu0.ru_utime - cpu0.ru_stime, 1)
    cfg["runs"].mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(standing, indent=1, sort_keys=True) + "\n")
    worse = [r["movie"] for r in rows if r["vs_recorded"] == "worse"]
    timing = {"wall_seconds": round(time.monotonic() - began, 1), "cpu_seconds": cpu}
    ranked = ranked_work(profile, cfg, health, rows)
    result = {"build": tas_health.header(health), "movies": rows, "worse": worse, **timing, "ranked": ranked}
    return {**result, "next": ranked[0]["next"] if ranked else "nothing open"}, int(bool(worse))


def ranked_work(
    profile: Profile,
    cfg: dict[str, Any],
    health: dict[str, Any],
    movies: list[dict[str, Any]],
    owed: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The ranked open-work list (module tas_rank) for these movie rows and the current build health."""
    from abu_decomp import owes

    if owed is None:
        try:
            owed = owes.run(profile, None, cfg["rank"]["owes_rows"] + len(movies))
        except Exception:
            owed = None
    return tas_rank.rank(cfg["rank"], health, movies, owed)


def standing_rows(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Movie rows from the recorded standing (no replay), for the ranked list outside `play --all`."""
    import json

    try:
        standing = json.loads((cfg["runs"] / "standing.json").read_text())
    except (OSError, ValueError):
        return []
    return [{"movie": name, **row} for name, row in sorted(standing.items())]
