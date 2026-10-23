"""`decomp tas save|list|show|delete|play`: controller movies replayed against the port.

Section `tas`: {"movies", "runs", "port", "args", "rom", "env", "exit_after", "timeout_seconds", "gdb"}.
`movies` is the folder of named movies (kept in a repository), `runs` holds recordings and replay runs, `port` is
the game executable and `args` its arguments, where the token `{user_dir}` becomes a fresh folder per run so saves
never leak between runs. `rom` is the image whose SHA-1 a movie names; replay refuses another one.

The port speaks only the raw form, one `BUTTONS STICK_X STICK_Y` line per guest controller poll (hex buttons): it
replays `N64C_TAS_PLAY`, appends every poll to `N64C_TAS_RECORD`, and exits 0 `N64C_TAS_EXIT_AFTER` polls after the
movie. A movie (`NAME.tas`) is the readable form of the same polls: `#` header lines, then one line per run of equal
polls, `BUTTONS [X,Y] [*COUNT]`, where BUTTONS is `-` or names joined by `+` (`A+START`) and the stick is omitted
when centred. Replay keys input to the poll index, so a movie is the same run at any host speed.

Every replay is the same one kind of run, and every one is visible: the port writes its latest picture to the run
folder (`N64C_VIEW`, a BMP at most ~15 times a second), a per-poll RAM hash (`N64C_TAS_HASH`) and, at speed 1 only,
the sound it plays (`N64C_AUDIO_OUT`). Speed 0 (turbo, the default) skips the retrace sleep; the game's own clock
is per retrace, so a movie is the same run at any speed. `run.json` in the run folder is the run's record (written at
start and at the end), which `tas serve` reads. `same_twice` compares the hash file with the movie's previous run on
the same port binary: true when every poll's RAM was identical, null when there is no earlier run to compare.

A replay runs under the supervisor's gdb bridge with the port's stop function as the breakpoint, so a stop or a
host fault leaves the failing stack and every thread's stack in the run folder. The outcome is one of `ok`,
`stop` (the port's own boot stop, with its subject), `crash` (a host fault or signal), `hang` (no exit within
`timeout_seconds`) or `exit` (any other exit code).
"""

from __future__ import annotations

import hashlib
import json
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

from abu_decomp import DecompError, tas_health, tas_rank, tas_symbolize
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
        "rank": tas_rank.settings(table.get("rank")),
    }


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


VIEW, HASH, AUDIO, RECORD = "view.bmp", "hash.txt", "audio.pcm", "run.json"
_builds: dict[tuple[str, int, int], str] = {}


def build_id(port: Path) -> str:
    """SHA-1 of the port executable (cached per path, mtime and size): runs compare only on the same build."""
    try:
        stat = port.stat()
    except OSError:
        return ""
    key = (str(port), stat.st_mtime_ns, stat.st_size)
    if key not in _builds:
        _builds[key] = _sha1(port)
    return _builds[key]


def new_run_id(cfg: dict[str, Any], name: str, now: datetime | None = None) -> str:
    """`STAMP-NAME`, with `-2`, `-3`... when that folder is taken (two plays of one movie in one second)."""
    plays, base = cfg["runs"] / "plays", f"{_stamp(now)}-{name}"
    run_id, n = base, 1
    while (plays / run_id).exists():
        n += 1
        run_id = f"{base}-{n}"
    return run_id


def read_record(run: Path) -> dict[str, Any] | None:
    try:
        row = json.loads((run / RECORD).read_text())
    except (OSError, ValueError):
        return None
    return row if isinstance(row, dict) else None


def _write_record(run: Path, row: dict[str, Any]) -> None:
    tmp = run / (RECORD + ".tmp")
    tmp.write_text(json.dumps(row, sort_keys=True) + "\n")
    tmp.replace(run / RECORD)


def alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def run_rows(cfg: dict[str, Any]) -> list[tuple[Path, dict[str, Any]]]:
    """Every replay folder with its record, oldest first. A record left `running` by a process that is gone (killed
    server, reboot) reads as done with no outcome."""
    plays = cfg["runs"] / "plays"
    rows = []
    for run in sorted(plays.iterdir()) if plays.is_dir() else []:
        row = read_record(run)
        if row is None:
            continue
        if row.get("status") == "running" and not alive(row.get("pid")):
            row = {**row, "status": "done", "outcome": None}
        rows.append((run, row))
    return rows


def last_poll(path: Path) -> int:
    """The last poll a hash file names (it gains one line per poll while the run goes)."""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - 128))
            data = handle.read()
    except OSError:
        return 0
    lines = data[: data.rfind(b"\n") + 1].splitlines()  # a line still being written is left out
    for line in reversed(lines):
        head = line.split(b" ", 1)[0]
        if head.isdigit():
            return int(head)
    return 0


def previous_run(cfg: dict[str, Any], name: str, build: str, before: str) -> Path | None:
    """The newest finished run of `name` on this build (by folder name, older than `before`) with a hash file."""
    for run, row in reversed(run_rows(cfg)):
        if run.name >= before or row.get("movie") != name or row.get("build") != build:
            continue
        if row.get("status") == "done" and not row.get("stopped") and row.get("outcome") and (run / HASH).is_file():
            return run
    return None


def same_hashes(a: Path, b: Path) -> bool:
    try:
        return (a / HASH).read_bytes() == (b / HASH).read_bytes()
    except OSError:
        return False


def fn_of(result: dict[str, Any]) -> str | None:
    """The function a non-clean outcome names: its subject (`func_X` or `func_X+0x10`), else the owner of the fault."""
    for text in (str(result.get("subject") or ""), str(result.get("owner") or "")):
        match = re.match(r"(func_[0-9A-Fa-f]{8}\w*?)(?:\+0x[0-9a-fA-F]+)?(?:\s|$)", text)
        if match:
            return match[1]
    return None


def play(
    profile: Profile,
    name: str,
    *,
    speed: int = 0,
    now: datetime | None = None,
    health: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Replay one movie at `speed` (0 = turbo), visible on the page; the result says whether its RAM was the same
    every poll as the movie's previous run on this build (`same_twice`)."""
    cfg = settings(profile)
    if not 0 <= speed <= 1000:
        raise DecompError("tas.play", f"speed is 0 (turbo) .. 1000, not {speed}")
    began = time.monotonic()
    health = tas_health.read(cfg, profile) if health is None else health
    polls = movie_polls(cfg, name)
    run = cfg["runs"] / "plays" / (run_id or new_run_id(cfg, name, now))
    user_dir = run / "user"
    user_dir.mkdir(parents=True)
    (run / "movie.raw").write_text(format_raw(polls))
    env = {
        **os.environ,
        **cfg["env"],
        "N64C_TAS_PLAY": str(run / "movie.raw"),
        "N64C_TAS_RECORD": str(run / "polls.raw"),
        "N64C_TAS_EXIT_AFTER": str(cfg["exit_after"]),
        "N64C_TAS_HASH": str(run / HASH),
        "N64C_VIEW": str(run / VIEW),
        "N64C_SPEED": str(speed),
    }
    if speed == 1:
        env["N64C_AUDIO_OUT"] = str(run / AUDIO)
    build = build_id(cfg["port"])
    # At real speed a movie takes its own length in wall time; the timeout is the allowance beyond that.
    timeout = cfg["timeout_seconds"] + ((len(polls) + cfg["exit_after"]) / (60 * speed) if speed else 0)
    record = {
        "id": run.name,
        "movie": name,
        "speed": speed,
        "polls": len(polls),
        "build": build,
        "status": "running",
        "started": time.time(),
        "pid": os.getpid(),
    }
    _write_record(run, record)
    argv = supervised(cfg, run, _argv(cfg, user_dir))
    try:
        with (run / "output.txt").open("w") as output:
            process = subprocess.Popen(
                argv, env=env, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT, start_new_session=True
            )
            _write_record(run, {**record, "pgid": process.pid})
            # The bridge holds a stopped port until its stdin says "continue"; a closed pipe ends it after the stacks.
            process.stdin.close()
            code, cpu = _wait(process, timeout)
    except BaseException:
        _write_record(run, {**record, "status": "done", "outcome": None, "ended": time.time()})
        raise
    text = (run / "output.txt").read_text(errors="replace")
    reached = len(parse_raw((run / "polls.raw").read_text())) if (run / "polls.raw").is_file() else 0
    result = {"movie": name, "run": str(run), "id": run.name, "speed": speed, "polls": len(polls)}
    result.update(polls_reached=reached, **classify(code, text))
    stopped = (run / "stopped").exists()
    if stopped:
        result["subject"] = "stopped from the page"
    result["wall_seconds"], result["cpu_seconds"] = round(time.monotonic() - began, 1), cpu
    stacks = [str(p) for p in sorted(run.glob("*-st*.txt")) + sorted(run.glob("*-threads.txt"))]
    if stacks:
        result["stacks"] = stacks
    # Kind, blocking, what cannot help, owner, arity note and one next command (the arity note comes first when set).
    result.update(tas_symbolize.claim(profile, result, stacks, health))
    if health:
        result["build"] = tas_health.header(health)
    before = previous_run(cfg, name, build, run.name) if build and not stopped else None
    result["same_twice"] = same_hashes(run, before) if before else None
    if before:
        result["compared_with"] = before.name
    result["fn"] = fn_of(result)
    keep = ("outcome", "subject", "polls_reached", "kind", "same_twice", "compared_with", "fn", "wall_seconds")
    _write_record(
        run,
        {**record, **{k: result.get(k) for k in keep}, "status": "done", "stopped": stopped, "ended": time.time()},
    )
    return result


def play_twice(profile: Profile, name: str, *, speed: int = 0) -> dict[str, Any]:
    """Two replays back to back; the second's `same_twice` compares it with the first."""
    play(profile, name, speed=speed)
    return play(profile, name, speed=speed)


def compare(recorded: dict[str, Any] | None, now: dict[str, Any]) -> str:
    """better / same / worse / new: a clean end beats any stop, then more polls reached beats fewer."""
    if not recorded:
        return "new"

    def standing(row: dict[str, Any]) -> tuple[int, int]:
        return (int(row.get("outcome") == "ok"), int(row.get("polls_reached") or 0))

    before, after = standing(recorded), standing(now)
    return "worse" if after < before else "better" if after > before else "same"


def _twice_text(value: Any) -> str:
    return "-" if value is None else "yes" if value else "NO"


def table(result: dict[str, Any]) -> str:
    """The `play --all` result as aligned text: the build header, then one row per movie."""
    build = result.get("build") or {}
    lines = [build.get("summary", "build: not configured")]
    lines += [f"  failed {row['unit']}: {row['error']}" for row in build.get("failed", [])]
    if build.get("stale"):
        lines.append(
            f"  stale: {build['stale']}; next: {build.get('rebuild', '')}"
            + (f"; then: {build['then']}" if build.get("then") else "")
        )
    head = ("movie", "outcome", "polls", "vs recorded", "same_twice", "wall/cpu s", "next")
    rows = [
        (
            r["movie"],
            r["outcome"] + (f" {r['subject']}" if r["subject"] else ""),
            f"{r['polls_reached']}/{r['polls']}",
            r["vs_recorded"],
            _twice_text(r.get("same_twice")),
            f"{r.get('wall_seconds', 0)}/{r.get('cpu_seconds', 0)}",
            r["next"],
        )
        for r in result["movies"]
    ]
    widths = [max(len(str(x[i])) for x in [head, *rows]) for i in range(6)]
    for row in [head, *rows]:
        lines.append("  ".join(str(c).ljust(w) for c, w in zip(row[:6], widths, strict=True)) + "  " + row[6])
    if "wall_seconds" in result:
        lines.append(f"wall {result['wall_seconds']} s, cpu {result['cpu_seconds']} s (children)")
    if "ranked" in result:
        lines.append(tas_rank.render(result["ranked"]))
    return "\n".join(lines)


def play_all(
    profile: Profile,
    *,
    jobs: int = 4,
    now: datetime | None = None,
    twice: bool = False,
    run_ids: dict[str, str] | None = None,
) -> tuple[dict[str, Any], int]:
    """Replay every saved movie in parallel at turbo, block until done, one row per movie against its recorded
    standing. `twice` replays each movie twice (same_twice then compares the two); `run_ids` names the run folders
    (the page knows them before the runs start).

    The recorded standing is the best row so far in `runs/standing.json`, moved only when a movie is not worse, so a
    regression stays `worse` until fixed. Exit status 1 when any movie is worse."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cfg = settings(profile)
    names = sorted(p.stem for p in cfg["movies"].glob("*.tas")) if cfg["movies"].is_dir() else []
    health = tas_health.read(cfg, profile)
    began, cpu0 = time.monotonic(), resource.getrusage(resource.RUSAGE_CHILDREN)

    def one(name: str) -> dict[str, Any]:
        try:
            if twice:
                play(profile, name, now=now, health=health)
            return play(profile, name, now=now, health=health, run_id=(run_ids or {}).get(name) if not twice else None)
        except DecompError as error:
            return {"movie": name, "outcome": "refused", "subject": str(error), "polls_reached": 0}

    def progress(row: dict[str, Any]) -> None:
        reached = f"{row.get('polls_reached', 0)}/{row.get('polls', 0)}"
        outcome = row["outcome"] + (f" {row['subject']}" if row.get("subject") else "")
        sys.stderr.write(
            f"tas: {row['movie']} {outcome} {reached} same_twice={_twice_text(row.get('same_twice'))} "
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
                "same_twice": result.get("same_twice"),
                "fn": result.get("fn"),
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
    from abu_decomp import owes, recipe_needs, tools

    try:  # a stop row recorded before its function was skipped for a type still names the type item
        project: Path | None = tools.project_dir(profile)
    except (DecompError, OSError, ValueError, KeyError, AttributeError):
        project = None
    movies = [{**m, "next": recipe_needs.reroute(project, m["next"])} if m.get("next") else m for m in movies]
    if owed is None:
        try:
            owed = owes.run(profile, None, cfg["rank"]["owes_rows"] + len(movies))
        except Exception:
            owed = None
    return tas_rank.rank(cfg["rank"], health, movies, owed)


def standing_rows(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Movie rows from the recorded standing (no replay), for the ranked list outside `play --all`."""

    try:
        standing = json.loads((cfg["runs"] / "standing.json").read_text())
    except (OSError, ValueError):
        return []
    return [{"movie": name, **row} for name, row in sorted(standing.items())]
