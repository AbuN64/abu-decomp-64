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
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from abu_decomp import DecompError
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
_KEYS = frozenset({"movies", "runs", "port", "args", "rom", "env", "exit_after", "timeout_seconds", "gdb"})
_REQUIRED = frozenset({"movies", "runs", "port", "args", "rom"})
STOP_FUNCTION = "n64c_boot_stopped"
STOP_SUBJECT = "n64c_boot_stop_symbol"

Poll = tuple[int, int, int]


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
    polls = parse_raw(source.read_text())
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
    live = [{"path": str(p), "polls": len(parse_raw(p.read_text()))} for p in recordings(cfg)[-5:]]
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
    reason = re.search(r"^debug-reason: (.+)$", output, re.M)
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


def play(profile: Profile, name: str, *, now: datetime | None = None) -> dict[str, Any]:
    cfg = settings(profile)
    path = _movie(cfg, name)
    if not path.is_file():
        raise DecompError("tas.play", f"no movie {path}")
    header, polls = decode(path.read_text())
    rom = _sha1(cfg["rom"])
    if header.get("rom-sha1") != rom:
        raise DecompError("tas.play", f"{path.name} was recorded on ROM {header.get('rom-sha1')}, not {rom}")
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
    argv = [
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
        str(run),
        "--",
        *_argv(cfg, user_dir),
    ]
    with (run / "output.txt").open("w") as output:
        process = subprocess.Popen(
            argv, env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            code: int | None = process.wait(cfg["timeout_seconds"])
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            code = None
    text = (run / "output.txt").read_text(errors="replace")
    reached = len(parse_raw((run / "polls.raw").read_text())) if (run / "polls.raw").is_file() else 0
    result = {"movie": name, "run": str(run), "polls": len(polls), "polls_reached": reached, **classify(code, text)}
    stacks = [str(p) for p in sorted(run.glob("*-st*.txt")) + sorted(run.glob("*-threads.txt"))]
    if stacks:
        result["stacks"] = stacks
    return result
