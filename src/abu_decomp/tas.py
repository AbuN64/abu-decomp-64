"""`decomp tas save|list|show|delete|play`: controller movies replayed against the port.

Section `tas`: {"movies", "runs", "port" (the shared seat, optional: see module tas_seat), "seat", "args", "rom", "env",
"exit_after", "timeout_seconds", "gdb"}.
`movies` is the folder of named movies (kept in a repository), `runs` holds recordings and replay runs, `port` is
the game executable and `args` its arguments, where the token `{user_dir}` becomes a fresh folder per run so saves
never leak between runs. `rom` is the image whose SHA-1 a movie names; replay refuses another one.

The port speaks only the raw form, one `BUTTONS STICK_X STICK_Y` line per guest controller poll (hex buttons): it
replays `N64C_TAS_PLAY`, appends every poll to `N64C_TAS_RECORD`, and exits 0 `N64C_TAS_EXIT_AFTER` polls after the
movie. A movie (`NAME.tas`) is the readable form of the same polls: `#` header lines, then one line per run of equal
polls, `BUTTONS [X,Y] [*COUNT]`, where BUTTONS is `-` or names joined by `+` (`A+START`) and the stick is omitted
when centred. Replay keys input to the poll index, so a movie is the same run at any host speed.
The header's `pak` (`on` or `off`) says whether the run had the Expansion Pak (8 MB RDRAM) or not (4 MB); a movie
without one ran with it. A replay keeps what the movie ran with (`play --pak` overrides, the page's "new run" picks),
the port gets `--pak off` for a run without it, and the run's record and the page's tile show it.

The header's `cpak` says what Controller Pak (32 KiB save card) the run started with: `none` (the slot is empty; a movie
without the line ran so), `fresh` (a newly formatted card) or the SHA-256 of the starting card, whose bytes sit beside
the movie as `<sha256>.mpk`. A replay never touches the user's card: it gets a copy in its run folder (`cpak.mpk`, the
card as it ended; `cpak-start.mpk`, as it began), so the same movie ends on the same card every time. The owner's own
runs on the page use the profile's card (`controller-pak.mpk` beside the runs) and keep what the game saves to it; the
movie saved from such a run names the card it started with. `play --cpak on|off` and the page's checkbox override.
Every replay is the same one kind of run, and every one is visible: the port writes its latest picture to the run
folder (`N64C_VIEW`, a BMP sampled at up to 30 Hz), a per-poll RAM hash
(`N64C_TAS_HASH`) and, at speed 1 only,
the sound it plays (`N64C_AUDIO_OUT`). Speed 0 (turbo, the default) skips the retrace sleep; the game's own clock
is per retrace, so a movie is the same run at any speed. `run.json` in the run folder is the run's record (written at
start and at the end), which `tas serve` reads. `same_twice` compares the hash file with the movie's previous run on
the same port binary: true when every poll's RAM was identical, null when there is no earlier run to compare.

`play --fps-unlock` (the page's "Framerate unlock" box) runs the port with `--fps-unlock`: between two game frames it
draws the scene again with its matrices moved part of the way and presents as fast as the host allows. It changes what
the window shows and nothing the game can see: the movie, the VI and audio timing, input polls and the per-poll hashes
are the same with the box on or off (`same_twice` holds across the two), so it is not a movie header line, the oracle
ignores it, and the run's record keeps `fps_unlock` and `shown` (the port's own closing "boot-fps:" count: pictures
presented and game frames per wall second).

Start points (module tas_points): `play --from POINT` begins at a named place in the game (a menu, an area) by replaying
the game's own inputs and save card from power-on; before a movie it makes the movie start there (header `start`),
`--from POINT --take` replays the point at turbo and then hands live input over, saved as a movie that starts at
the point. `point list` shows which game areas have a point.

A replay runs under the supervisor's gdb bridge with the port's stop function as the breakpoint, so a stop or a
host fault leaves the failing stack and every thread's stack in the run folder. The outcome is one of `ok`,
`stop` (the port's own boot stop, with its subject), `crash` (a host fault or signal), `hang` (no exit within
`timeout_seconds`) or `exit` (any other exit code).
"""

from __future__ import annotations

import contextlib
import fcntl
import functools
import hashlib
import json
import os
import re
import resource
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from abu_decomp import (
    DecompError,
    procguard,
    slots,
    tas_coverage,
    tas_fault,
    tas_findings,
    tas_health,
    tas_oracle,
    tas_points,
    tas_rank,
    tas_sanitize,
    tas_seat,
    tas_select,
    tas_statediff,
    tas_symbolize,
)
from abu_decomp.config import Profile, check_keys

SCHEMA = 1
MAX_PADS = 4  # controllers a movie may drive (the console has four ports)
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
_PAD = r"[0-9a-fA-F]{1,4} -?\d{1,3} -?\d{1,3}"
_RAW = re.compile(rf"{_PAD}(?: {_PAD}){{0,{MAX_PADS - 1}}}")
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
        "oracle",
        "coverage",
        "sanitizer",
        "select",
        "seat",
    }
)
_REQUIRED = frozenset({"movies", "runs", "args", "rom"})
STOP_FUNCTION = "n64c_boot_stopped"
STOP_SUBJECT = "*(char**)&n64c_boot_stop_symbol"

Poll = tuple[int, ...]  # BUTTONS, STICK_X, STICK_Y of pad 1, then of each further pad (3 * pads ints)
PAKS = ("on", "off")
CPAKS = ("on", "off")
CARD, CARD_START, PROFILE_CARD = "cpak.mpk", "cpak-start.mpk", "controller-pak.mpk"
CARD_BYTES = 0x8000
_CARD_MODE = re.compile(r"none|fresh|[0-9a-f]{64}")


def fresh_card() -> bytes:
    """A newly formatted Controller Pak, byte for byte what the port makes for a missing card file (the shim's
    n64c_cpak_format): four id blocks with a fixed serial, an index table with every data page free, no notes."""
    image = bytearray(CARD_BYTES)
    for at in (0x20, 0x60, 0x80, 0xC0):
        block = image[at : at + 32]
        for i in range(24):
            block[i] = 0xA5 ^ (i * 29 & 0xFF)
        block[0x18:0x1C] = bytes([0, 1, 1, 1])
        total = sum(int.from_bytes(block[i : i + 2], "big") for i in range(0, 28, 2)) & 0xFFFF
        block[0x1C:0x20] = total.to_bytes(2, "big") + ((0xFFF2 - total) & 0xFFFF).to_bytes(2, "big")
        image[at : at + 32] = block
    for page in range(1, 128):
        image[0x100 + page * 2 : 0x102 + page * 2] = (1 if page < 5 else 3).to_bytes(2, "big")
    image[0x101] = (3 * 123) & 0xFF
    image[0x200:0x300] = image[0x100:0x200]
    return bytes(image)


def check_cpak(cpak: str | None) -> None:
    if cpak is not None and cpak not in CPAKS:
        raise DecompError("tas.cpak", f"the Controller Pak is 'on' or 'off', not {cpak!r}")


def _build(profile: Profile, table: Any) -> dict[str, Any]:
    if not table:
        return {}
    check_keys(profile.source_file, "tas.build", table, frozenset(), tas_health.BUILD_KEYS)
    out = {"rebuild": str(table.get("rebuild", ""))}
    for key in ("units", "sources", "pins", "pins_repo", "lock"):
        if key in table:
            out[key] = profile.path(f"tas.build.{key}", table[key])
    return out


def _seat(profile: Profile, table: Any) -> dict[str, Any]:
    if not table:
        return {}
    check_keys(profile.source_file, "tas.seat", table, frozenset({"stamp"}), tas_seat.SEAT_KEYS)
    out = {key: str(table[key]) for key in tas_seat.SEAT_KEYS if key in table and key != "tree"}
    if "tree" in table:
        out["tree"] = profile.path("tas.seat.tree", table["tree"])
    return out


def port_of(cfg: dict[str, Any]) -> Path:
    """The port executable cfg runs: an explicit `--port` (with_port) or the profile's shared seat; none is an error."""
    if not cfg.get("port"):
        raise DecompError("tas.port", "no port executable: the profile names no shared seat; pass --port <executable>")
    return Path(cfg["port"])


def settings(profile: Profile) -> dict[str, Any]:
    table = profile.section("tas")
    check_keys(profile.source_file, "tas", table, _REQUIRED, _KEYS)
    return {
        "movies": profile.path("tas.movies", table["movies"]),
        "runs": profile.path("tas.runs", table["runs"]),
        "port": profile.path("tas.port", table["port"]) if "port" in table else None,
        "seat": _seat(profile, table.get("seat")),
        "args": [str(arg) for arg in table["args"]],
        "rom": profile.path("tas.rom", table["rom"]),
        "env": {str(k): str(v) for k, v in table.get("env", {}).items()},
        "exit_after": int(table.get("exit_after", 120)),
        "timeout_seconds": int(table.get("timeout_seconds", 300)),
        "gdb": str(table.get("gdb", "gdb")),
        "build": _build(profile, table.get("build")),
        "rank": tas_rank.settings(table.get("rank")),
        "sanitizer_port": profile.path("tas.sanitizer.port", table["sanitizer"]["port"])
        if isinstance(table.get("sanitizer"), dict) and "port" in table["sanitizer"]
        else None,
    }


def parse_raw(text: str) -> list[Poll]:
    polls: list[Poll] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        if not _RAW.fullmatch(line.strip()):
            raise DecompError(
                "tas.raw", f"line {number}: expected 'BUTTONS X Y' per pad (up to {MAX_PADS}), got {line!r}"
            )
        fields = line.split()
        poll = tuple(int(f, 16) if i % 3 == 0 else int(f) for i, f in enumerate(fields))
        if polls and len(poll) != len(polls[0]):
            raise DecompError("tas.raw", f"line {number}: {len(poll) // 3} pads after {len(polls[0]) // 3}")
        polls.append(poll)
    return polls


def read_raw(path: Path) -> list[Poll]:
    """The polls of a recording the port may still be writing: a last line without its newline is left out."""
    text = path.read_text()
    return parse_raw(text[: text.rfind("\n") + 1])


def format_raw(polls: list[Poll]) -> str:
    return "".join(" ".join(f"{p[i]:04x} {p[i + 1]} {p[i + 2]}" for i in range(0, len(p), 3)) + "\n" for p in polls)


def pad_count(polls: list[Poll]) -> int:
    """Controllers the polls drive (1 for no polls)."""
    return len(polls[0]) // 3 if polls else 1


def header_pads(header: dict[str, str]) -> int:
    try:
        count = int(header.get("pads", "1"))
    except ValueError:
        count = 0
    if not 1 <= count <= MAX_PADS:
        raise DecompError("tas.movie", f"header pads is 1..{MAX_PADS}, not {header.get('pads')!r}")
    return count


def _buttons_text(bits: int) -> str:
    names = [name for name, bit in BUTTONS if bits & bit]
    if bits & ~sum(_BIT.values()):
        raise DecompError("tas.buttons", f"poll has bits outside the controller's buttons: {bits:#06x}")
    return "+".join(names) or "-"


def encode(polls: list[Poll], header: dict[str, str]) -> str:
    """The movie text. A movie of more than one pad names the count in its header (`pads N`) and writes each poll as
    the pads' `BUTTONS [X,Y]` joined by ` | `."""
    count = pad_count(polls)
    if count > 1:
        header = {**header, "pads": str(count)}
    lines = [f"# tas {SCHEMA}"] + [f"# {key} {value}" for key, value in header.items()]
    index = 0
    while index < len(polls):
        end = index
        while end + 1 < len(polls) and polls[end + 1] == polls[index]:
            end += 1
        parts = []
        for at in range(0, len(polls[index]), 3):
            bits, x, y = polls[index][at : at + 3]
            parts.append(_buttons_text(bits) + (f" {x},{y}" if (x, y) != (0, 0) else ""))
        count_ = end - index + 1
        lines.append(" | ".join(parts) + (f" *{count_}" if count_ > 1 else ""))
        index = end + 1
    return "\n".join(lines) + "\n"


def decode(text: str) -> tuple[dict[str, str], list[Poll]]:
    header: dict[str, str] = {}
    lines: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            key, _, value = line[1:].strip().partition(" ")
            header[key] = value.strip()
            continue
        lines.append((number, line))
    if header.get("tas") != str(SCHEMA):
        raise DecompError("tas.movie", f"movie must start with '# tas {SCHEMA}'")
    pads = header_pads(header)
    polls: list[Poll] = []
    for number, line in lines:
        count = 1
        fields = line.split()
        if fields[-1].startswith("*"):
            count = int(fields.pop()[1:])
        segments = " ".join(fields).split("|")
        if len(segments) != pads or count < 1:
            raise DecompError(
                "tas.movie", f"line {number}: expected {pads} pad(s) of 'BUTTONS [X,Y]' and [*COUNT], got {line!r}"
            )
        poll: list[int] = []
        for segment in segments:
            parts = segment.split()
            x, y = 0, 0
            if len(parts) == 2:
                x_text, _, y_text = parts.pop().partition(",")
                x, y = int(x_text), int(y_text)
            if len(parts) != 1 or not -128 <= x <= 127 or not -128 <= y <= 127:
                raise DecompError("tas.movie", f"line {number}: expected 'BUTTONS [X,Y] [*COUNT]', got {line!r}")
            bits = 0
            for name in parts[0].split("+") if parts[0] != "-" else []:
                if name not in _BIT:
                    raise DecompError("tas.movie", f"line {number}: unknown button {name!r}")
                bits |= _BIT[name]
            poll += [bits, x, y]
        polls.extend([tuple(poll)] * count)
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
    path = cfg["movies"] / f"{name}.tas"
    if not path.is_file() and (point := cfg["movies"] / "points" / f"{name}.tas").is_file():
        return point  # a start point replays like a movie (module tas_points)
    return path


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")


def recordings(cfg: dict[str, Any]) -> list[Path]:
    folder = cfg["runs"] / "live"
    return sorted(folder.glob("*.raw")) if folder.is_dir() else []


def check_pak(pak: str | None) -> None:
    if pak is not None and pak not in PAKS:
        raise DecompError("tas.pak", f"the Expansion Pak is 'on' or 'off', not {pak!r}")


def save(
    profile: Profile,
    name: str,
    *,
    source: Path | None,
    note: str,
    replace: bool,
    now: datetime | None = None,
    pak: str = "on",
    cpak: str = "none",
    cpak_image: Path | None = None,
    parent: str | None = None,
    at: int | None = None,
    start: str | None = None,
    prefix: int = 0,
) -> dict[str, Any]:
    """Save polls as movie `name`; `parent`/`at` name the movie a take branched from and the poll it left it at
    (header lines `from` / `at`; module tas_serve folds takes under their root movie). `start` names the start point
    the recording began from (module tas_points): its first `prefix` polls are the point's, kept in the point, not
    here."""
    cfg = settings(profile)
    check_pak(pak)
    if not _CARD_MODE.fullmatch(cpak):
        raise DecompError("tas.save", f"the cpak header is none, fresh or a SHA-256, not {cpak!r}")
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
    if start:
        if len(polls) <= prefix:
            raise DecompError("tas.save", f"{source} holds no polls after start point {start!r}")
        polls = polls[prefix:]
    if not polls:
        raise DecompError("tas.save", f"{source} holds no polls")
    header = {
        "rom-sha1": _sha1(cfg["rom"]),
        "recorded": _stamp(now),
        "polls": str(len(polls)),
        "pak": pak,
        "cpak": cpak,
    }
    if note:
        header["note"] = " ".join(note.split())
    if start:
        header["start"] = start
    if parent:
        header["from"] = parent
        header["at"] = str(at or 0)
    cfg["movies"].mkdir(parents=True, exist_ok=True)
    if len(cpak) == 64:
        card = cfg["movies"] / f"{cpak}.mpk"
        if not card.is_file():
            if cpak_image is None or hashlib.sha256(cpak_image.read_bytes()).hexdigest() != cpak:
                raise DecompError("tas.save", f"the starting card {cpak} is not at hand to keep beside the movie")
            card.write_bytes(cpak_image.read_bytes())
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
                "pak": header.get("pak", "on"),
                "cpak": header.get("cpak", "none"),
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


def _slot_free(args: list[str]) -> list[str]:
    """The profile's port arguments without any `--cpak X`: the Controller Pak slot is the run's to name, and the port
    keeps the last `--cpak` it reads, so a profile's own would replace the card the run was given."""
    kept, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg == "--cpak":
            skip = True
        else:
            kept.append(arg)
    return kept


def _argv(
    cfg: dict[str, Any], user_dir: Path, pak: str = "on", card: Path | None = None, fps_unlock: bool = False
) -> list[str]:
    """The port's command line. The Controller Pak is always named: `--cpak off` for an empty slot (the port's own
    default is a card), else the card file the run owns. `--fps-unlock` is the framerate unlock (presentation only)."""
    argv = [str(port_of(cfg)), "--cpak", str(card) if card else "off"]
    argv += [arg.replace("{user_dir}", str(user_dir)) for arg in _slot_free(cfg["args"])]
    argv = [*argv, "--pak", "off"] if pak == "off" else argv
    return [*argv, "--fps-unlock"] if fps_unlock else argv


def port_env(cfg: dict[str, Any], folder: Path) -> dict[str, str]:
    """The environment of every port process: the host's plus the profile's, and `GCOV_PREFIX=<folder>/gcov` last so a
    coverage build leaves its run counters in the folder it ran for, never beside the notes in the build tree."""
    return {
        **os.environ,
        **cfg["env"],
        "GCOV_PREFIX": str(folder / "gcov"),
        "N64C_CALLS": str(folder),  # the port's per-call record (RUN/calls.jsonl) is on in every replay, oracle or not
        "N64C_CALLS_FIRST": str(tas_oracle.CALLS_FIRST),
    }


_FPS_LINE = re.compile(
    r"boot-fps: \w+ unlock=(\d) speed=(\d+) retrace=(\d+) wall=([\d.]+) presented=(\d+) \(([\d.]+)/s\) "
    r"game=(\d+) \(([\d.]+)/s\) interpolated=(\d+)"
)


def fps_shown(output: str) -> dict[str, Any] | None:
    """What the port presented, from its last "boot-fps:" line: pictures per wall second the window got, game frames
    per wall second, and how many pictures were drawn between game frames. None for a port without the line."""
    lines = _FPS_LINE.findall(output)
    if not lines:
        return None
    unlock, speed, retrace, wall, presented, per_s, game, game_per_s, interpolated = lines[-1]
    return {
        "unlock": unlock == "1",
        "speed": int(speed),
        "retraces": int(retrace),
        "wall": float(wall),
        "presented": int(presented),
        "presented_per_s": float(per_s),
        "game": int(game),
        "game_per_s": float(game_per_s),
        "interpolated": int(interpolated),
    }


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


_GUEST_FAULT = re.compile(r"^n64c-fault: TLB-miss-([0-9a-f]{8}) (load|store) at host pc (0x[0-9a-f]+)$")


def guest_fault(output: str) -> dict[str, str] | None:
    """The wild guest access behind a `boot-missing: rw_boot_entry`: the game answers a TLB miss its fault service
    cannot map with its own fatal path, which reboots through rw_boot_entry. That is a crash, not a stop at an unlinked
    function. It is the last `n64c-fault` line when only `boot-shim:` lines separate it from the `boot-missing` line
    (a miss the guest mapped is followed by game progress, `boot-step:` lines)."""
    at = re.search(r"^boot-missing: rw_boot_entry$", output, re.M)
    if not at:
        return None
    for line in reversed(output[: at.start()].splitlines()):
        if line.startswith("boot-shim:"):
            continue
        hit = _GUEST_FAULT.match(line)
        return {"address": hit[1], "access": hit[2], "host_pc": hit[3]} if hit else None
    return None


def classify(code: int | None, output: str) -> dict[str, Any]:
    # The bridge names the subject when it held the stop; stops that end the process directly only print it.
    reason = re.search(r"^debug-reason: (.+)$", output, re.M) or re.search(r"^boot-missing: (\S+)$", output, re.M)
    signal_name = re.search(r"^debug-signal: (\S+)$", output, re.M)
    if code is None:
        return {"outcome": "hang"}
    fault = guest_fault(output) if reason else None
    if fault:
        return {
            "outcome": "crash",
            "subject": f"host-fault-{fault['address']}",
            "fault": f"guest {fault['access']} of {fault['address']} at host pc {fault['host_pc']}, then rw_boot_entry",
        }
    if reason:
        subject = reason[1].strip()
        return {"outcome": "crash" if subject.startswith("host-fault-") else "stop", "subject": subject}
    if signal_name:
        return {"outcome": "crash", "subject": signal_name[1]}
    if code == 0 and "n64c-tas: done" in output:
        return {"outcome": "ok"}
    return {"outcome": "exit", "subject": f"exit {code}"}


def movie_pak(cfg: dict[str, Any], name: str, start: str | None = None) -> str:
    """What the saved movie ran with: its header's `pak`, `on` for a movie from before the switch."""
    header = decode(_movie(cfg, name).read_text())[0]
    pak = tas_points.inherit(cfg, {**header, "start": start} if start else header, "pak", "on")
    check_pak(pak)
    return pak


def movie_cpak(cfg: dict[str, Any], name: str, start: str | None = None) -> str:
    """The card a saved movie started with: its header's `cpak`; `none` for a movie from before the card."""
    header = decode(_movie(cfg, name).read_text())[0]
    mode = tas_points.inherit(cfg, {**header, "start": start} if start else header, "cpak", "none")
    if not _CARD_MODE.fullmatch(mode):
        raise DecompError("tas.cpak", f"{name}: the cpak header is none, fresh or a SHA-256, not {mode!r}")
    return mode


def start_card(cfg: dict[str, Any], mode: str) -> bytes | None:
    """A replay card start: None for an empty slot, a fresh card, or the image kept beside the movie."""
    if mode == "none":
        return None
    if mode == "fresh":
        return fresh_card()
    kept = cfg["movies"] / f"{mode}.mpk"
    if not kept.is_file() or hashlib.sha256(kept.read_bytes()).hexdigest() != mode:
        raise DecompError("tas.cpak", f"the starting card {kept} is missing or does not match its name")
    return kept.read_bytes()


def card_mode(image: bytes) -> str:
    return "fresh" if image == fresh_card() else hashlib.sha256(image).hexdigest()


def movie_polls(cfg: dict[str, Any], name: str, start: str | None = None) -> list[Poll]:
    """A saved movie's polls (its start point's first, `start` overriding the one it names), refused when it was
    recorded on another ROM."""
    path = _movie(cfg, name)
    if not path.is_file():
        raise DecompError("tas.play", f"no movie {path}")
    header, polls = decode(path.read_text())
    rom = _sha1(cfg["rom"])
    if header.get("rom-sha1") != rom:
        raise DecompError("tas.play", f"{path.name} was recorded on ROM {header.get('rom-sha1')}, not {rom}")
    return tas_points.joined(cfg, header, polls, start)


def retry_source(cfg: dict[str, Any], source: str, take_at: int | None) -> dict[str, Any]:
    """What a retry of run `source` from poll `take_at` replays: that run's movie name, its recorded polls up to the
    poll, its Expansion Pak and the card it started with (a run keeps `cpak-start.mpk`), so the prefix is the run."""
    run = run_folder(cfg, source)
    row = read_record(run) if run else None
    if not run or not row:
        raise DecompError("tas.retry", f"no run {source}")
    try:
        polls = read_raw(run / "polls.raw")
    except OSError:
        polls = []
    if take_at is None or not 0 <= take_at <= len(polls):
        raise DecompError("tas.retry", f"retry from a poll 0..{len(polls)} of {source}, not {take_at}")
    kept = run / CARD_START
    return {
        "movie": str(row.get("movie") or source),
        "polls": polls[:take_at],
        "pak": str(row.get("pak") or "on"),
        "cpak": str(row.get("cpak") or "none"),
        "card": kept.read_bytes() if kept.is_file() else None,
        "start": row.get("start") or None,
        "prefix": int(row.get("prefix") or 0),
        "pads": int(row.get("pads") or pad_count(polls)),
    }


def run_folder(cfg: dict[str, Any], run_id: str) -> Path | None:
    """The folder of run `run_id` under runs/plays, None for a name that is not one."""
    if not re.fullmatch(r"[\w.-]+", run_id or "") or run_id.startswith("."):
        return None
    run = cfg["runs"] / "plays" / run_id
    return run if run.is_dir() else None


PINNED = "pinned"  # present in a run folder the owner keeps: never archived


def pin(cfg: dict[str, Any], run_id: str, keep: bool) -> dict[str, Any]:
    run = run_folder(cfg, run_id)
    if not run or read_record(run) is None:
        raise DecompError("tas.pin", f"no run {run_id}")
    if keep:
        (run / PINNED).write_text("kept from the TAS page\n")
    else:
        (run / PINNED).unlink(missing_ok=True)
    return {"id": run_id, "pinned": keep}


def archive(cfg: dict[str, Any], keep_days: float, now: float | None = None) -> list[str]:
    """Delete finished runs older than `keep_days` (by their end) unless pinned, the newest finished run of their
    movie (the movie's standing, coverage and same_twice source) or named by the regression corpus (its baselines,
    oracle references and last gate). Runs are replays of kept movies, so what goes is reproducible; returns the
    deleted run ids."""
    from abu_decomp import tas_corpus

    cutoff = (now or time.time()) - keep_days * 86400
    corpus = tas_corpus.kept_runs(cfg)
    rows = run_rows(cfg)
    newest = {}
    for run, row in rows:  # oldest first: the last one seen per movie is its newest
        if row.get("status") == "done" and row.get("outcome") and not row.get("taken"):
            newest[row.get("movie")] = run.name
    gone = []
    for run, row in rows:
        if row.get("status") != "done" or running(run) or (run / PINNED).exists():
            continue
        if newest.get(row.get("movie")) == run.name or run.name in corpus:
            continue
        if float(row.get("ended") or row.get("started") or 0) >= cutoff:
            continue
        shutil.rmtree(run, ignore_errors=True)
        gone.append(run.name)
    return gone


def _wait(process: subprocess.Popen, timeout: float, run: Path | None = None) -> tuple[int | None, float]:
    """(exit code or None after a kill at the timeout, CPU seconds of the run's whole process tree).

    Once live input drives the run (`run/taken`), the timeout no longer applies: it is a hang after STALL_SECONDS
    without a poll. wait4 returns the rusage of the child and every descendant it reaped, so one replay's CPU is its
    own even while other replays run in this process."""
    end = time.monotonic() + timeout
    last, moved = -1, time.monotonic()
    while True:
        if run is not None:
            _check_play_signal()
        pid, status, usage = os.wait4(process.pid, os.WNOHANG)
        if pid:
            process.returncode = os.waitstatus_to_exitcode(status)
            return process.returncode, round(usage.ru_utime + usage.ru_stime, 1)
        if run is not None and (run / TAKEN).exists():
            poll = last_poll(run / HASH)
            if poll != last:
                last, moved = poll, time.monotonic()
            end = moved + STALL_SECONDS
        if time.monotonic() > end:
            os.killpg(process.pid, signal.SIGKILL)
            _, status, usage = os.wait4(process.pid, 0)
            process.returncode = os.waitstatus_to_exitcode(status)
            return None, round(usage.ru_utime + usage.ru_stime, 1)
        time.sleep(0.05)


VIEW, HASH, AUDIO, RECORD = "view.bmp", "hash.txt", "audio.pcm", "run.json"
TAKEN = "taken"  # present once live input drives the run
HUMAN_LABEL = "owner"
STALL_SECONDS = 20.0  # a run driven by live input is a hang after this long without a poll
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


_codes: dict[tuple[str, int, int], str] = {}


def _text_section(path: Path) -> bytes:
    """The bytes of an ELF executable's `.text` (32 or 64 bit, little endian); empty when there is none."""
    with path.open("rb") as handle:
        head = handle.read(64)
        if head[:4] != b"\x7fELF" or head[5] != 1:
            return b""
        wide = head[4] == 2
        shoff = struct.unpack_from("<Q" if wide else "<I", head, 0x28 if wide else 0x20)[0]
        shentsize, shnum, shstrndx = struct.unpack_from("<HHH", head, 0x3A if wide else 0x2E)
        handle.seek(shoff)
        table = handle.read(shentsize * shnum)
        fmt = "<IIQQQQ" if wide else "<IIIIII"  # name, type, flags, address, offset, size
        sections = [struct.unpack_from(fmt, table, i * shentsize) for i in range(shnum)]
        handle.seek(sections[shstrndx][4])
        strings = handle.read(sections[shstrndx][5])
        for sect in sections:
            if strings[sect[0] : strings.index(b"\0", sect[0])] == b".text":
                handle.seek(sect[4])
                return handle.read(sect[5])
    return b""


def code_hash(port: Path) -> str:
    """SHA-1 of the port executable's `.text` (cached per path, mtime and size): a rename or relink that leaves the
    native code alone keeps it, so a run on an equivalent build counts as a run on this one. Empty for a missing or
    non-ELF file."""
    try:
        stat = port.stat()
        key = (str(port), stat.st_mtime_ns, stat.st_size)
        if key not in _codes:
            text = _text_section(port)
            _codes[key] = hashlib.sha1(text).hexdigest() if text else ""
        return _codes[key]
    except (OSError, ValueError, struct.error):
        return ""


def run_code(row: dict[str, Any]) -> str:
    """The code hash a run's record carries; an older record without one reads it from the executable it ran, when that
    file still is the build the record names."""
    if row.get("code"):
        return str(row["code"])
    port = Path(row["port"]) if row.get("port") else None
    return code_hash(port) if port and build_id(port) == row.get("build") else ""


def in_shard(names: list[str], shard: str | None) -> list[str]:
    """The movies of shard `K/N` (by a stable hash of the name), so several hosts split one corpus; all without one."""
    if not shard:
        return names
    match = re.fullmatch(r"(\d+)/(\d+)", shard)
    if not match or not 0 <= int(match[1]) < int(match[2]):
        raise DecompError("tas.shard", f"--shard is K/N with 0 <= K < N, not {shard!r}")
    index, count = int(match[1]), int(match[2])
    return [n for n in names if zlib.crc32(n.encode()) % count == index]


def equivalent_build(row: dict[str, Any], code: str) -> bool:
    """A run made by an executable with this `.text` hash (`code`, the current port's): a rename or relink that leaves
    the native code alone does not change a replay's verdict."""
    return bool(code) and run_code(row) == code


_LABEL = re.compile(r"[A-Za-z0-9_.-]{1,40}")


def check_label(label: str | None) -> None:
    if label and not _LABEL.fullmatch(label):
        raise DecompError("tas.label", f"a label is a lane name ([A-Za-z0-9_.-], at most 40), not {label!r}")


def with_port(cfg: dict[str, Any], port: Path | str | None) -> dict[str, Any]:
    """cfg running `port` (another build's executable) instead of the configured one."""
    if not port:
        return cfg
    path = Path(port).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise DecompError("tas.port", f"no executable port at {path}")
    return {**cfg, "port": path.resolve(), "port_explicit": True}


def new_run_id(cfg: dict[str, Any], name: str, now: datetime | None = None) -> str:
    """`STAMP-NAME`, with `-2`, `-3`... when that folder is taken (two plays of one movie in one second)."""
    plays, base = cfg["runs"] / "plays", f"{_stamp(now)}-{name}"
    run_id, n = base, 1
    while (plays / run_id).exists():
        n += 1
        run_id = f"{base}-{n}"
    return run_id


# record path -> (mtime_ns, size, parsed): a listing re-reads only the records that changed
_RECORDS: dict[str, tuple[int, int, dict[str, Any]]] = {}


def read_record(run: Path) -> dict[str, Any] | None:
    path = run / RECORD
    key = str(path)
    try:
        info = path.stat()
        hit = _RECORDS.get(key)
        if hit and hit[0] == info.st_mtime_ns and hit[1] == info.st_size:
            return dict(hit[2])
        row = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(row, dict):
        return None
    _RECORDS[key] = (info.st_mtime_ns, info.st_size, row)
    return dict(row)


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


class NeedsSupervisor(Exception):
    """A first pass without the gdb bridge ended in anything but ok: replay under the bridge for the full outcome."""

    def __init__(self, run: Path) -> None:
        super().__init__(f"{run.name} did not end ok")
        self.run = run


class PlayInterrupted(KeyboardInterrupt):
    """Cancellation shared by the CLI's parallel plays; never swallowed as a tool error."""

    def __init__(self, signum: int):
        self.signum = signum
        super().__init__(signal.Signals(signum).name)


_play_signal = 0
_play_local = threading.local()


def _check_play_signal() -> None:
    if _play_signal:
        raise PlayInterrupted(_play_signal)


def _play_signals(function):
    @functools.wraps(function)
    def call(*args, **kwargs):
        global _play_signal
        # play_all owns the main-thread handlers; its workers share the cancellation.
        if (
            threading.current_thread() is not threading.main_thread()
            or signal.getsignal(signal.SIGTERM) is _cancel_play
        ):
            _check_play_signal()
            return function(*args, **kwargs)
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        _play_signal = 0
        try:
            for sig in previous:
                signal.signal(sig, _cancel_play)
            result = function(*args, **kwargs)
            _check_play_signal()
            return result
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            _play_signal = 0

    return call


def _cancel_play(signum, _frame) -> None:
    global _play_signal
    if not _play_signal:
        _play_signal = signum
        if not getattr(_play_local, "starting", False):
            raise PlayInterrupted(signum)


@contextlib.contextmanager
def _starting_play_process():
    # A signal between Popen returning and registering its child must not orphan
    # that child. Defer the exception until ownership is recorded, without blocking
    # signals in the child (it would inherit a pthread signal mask).
    _play_local.starting = True
    try:
        yield
    finally:
        _play_local.starting = False
    _check_play_signal()


def _abandoned(row: dict[str, Any], subject: str, *, outcome: str = "killed") -> dict[str, Any]:
    ended = time.time()
    result = {**row, "status": "done", "outcome": outcome, "subject": subject, "ended": ended}
    if isinstance(row.get("started"), (int, float)):
        result["wall_seconds"] = round(max(0, ended - row["started"]), 1)
    if isinstance(row.get("oracle"), dict) and row["oracle"].get("status") == "running":
        result["oracle"] = {**row["oracle"], "status": "done", "outcome": outcome, "subject": subject}
    return result


@contextlib.contextmanager
def _run_record(run: Path, record: dict[str, Any]):
    """A kernel lease covers setup, replay and reporting, including exceptional exits."""
    with (run / "run.lock").open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record["lease"] = True
        children, reapers = [], []
        try:
            _write_record(run, record)
            _check_play_signal()
            yield children, reapers
            _check_play_signal()
        except BaseException as error:
            row = read_record(run) or record
            if row.get("status") == "running":
                signum = error.signum if isinstance(error, PlayInterrupted) else None
                if isinstance(error, KeyboardInterrupt):
                    signum = signum or signal.SIGINT
                subject = (
                    f"interrupted by {signal.Signals(signum).name}"
                    if signum
                    else f"play aborted: {type(error).__name__}"
                )
                outcome = "timeout" if isinstance(error, subprocess.TimeoutExpired) else "killed"
                row = _abandoned(row, subject, outcome=outcome)
                if signum:
                    row["signal"] = signal.Signals(signum).name
                _write_record(run, row)
            # Only process groups started by this play. Reapers may already own wait4
            # for the oracle; kill first, then let their wait complete.
            for process in children:
                if process is not None and process.returncode is None:
                    with contextlib.suppress(OSError):
                        os.killpg(process.pid, signal.SIGKILL)
            for waiter in reapers:
                waiter.join(timeout=5)
            for process in children:
                if process is not None:
                    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                        process.wait(timeout=5)
            raise


def _record_live(run: Path, row: dict[str, Any]) -> bool:
    if row.get("status") != "running":
        return False
    if not row.get("lease"):
        return alive(row.get("pid"))
    with (run / "run.lock").open("a") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def running(run: Path) -> bool:
    """A running record with a live writer (kernel lease; PID for old records)."""
    return _record_live(run, read_record(run) or {})


def reconcile_runs(cfg: dict[str, Any]) -> None:
    """Persist abandoned records before the next play; never touch a live lease.

    SIGKILL and reboot cannot run cleanup. An unlocked lease is authoritative even
    if the owner's PID has been reused; old records use their recorded PID.
    """
    plays = cfg["runs"] / "plays"
    for run in sorted(plays.iterdir()) if plays.is_dir() else []:
        row = read_record(run)
        if not row or row.get("status") != "running":
            continue
        with (run / "run.lock").open("a") as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            row = read_record(run)
            if not row or row.get("status") != "running" or (not row.get("lease") and alive(row.get("pid"))):
                continue
            row = _abandoned(row, "play owner disappeared; reconciled on next play")
            row["reconciled"] = True
            _write_record(run, row)


# newest replay folders kept whatever their age (10-27: 2676 runs of ~46 MB = 77+ GB in two days, none ever removed)
KEEP_PLAYS = 100
# a replay folder younger than this stays whatever the count: lanes read their own run after it ends
KEEP_PLAY_HOURS = 3


def prune_plays(
    cfg: dict[str, Any], keep: int = KEEP_PLAYS, hours: float = KEEP_PLAY_HOURS, now: float | None = None
) -> int:
    """Remove the replay folders nothing reads any more: all but the newest `keep` that are also older than `hours`,
    never one a live play holds, plus the stray `*.gcov.json.gz` files an earlier gcovr left in the folder.
    Returns the folders removed. A play is a run: what it writes lives and dies in its own folder (owner boundary)."""
    plays = cfg["runs"] / "plays"
    if not plays.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - hours * 3600
    removed = 0
    with os.scandir(plays) as it:
        entries = sorted(it, key=lambda e: e.name)
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False) and entry.name.endswith(".gcov.json.gz"):
            with contextlib.suppress(OSError):
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    os.unlink(entry.path)
    folders = [Path(e.path) for e in entries if e.is_dir(follow_symlinks=False)]
    for run in folders[:-keep] if keep else folders:
        try:
            if run.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        row = read_record(run)
        if row and row.get("status") == "running" and _record_live(run, row):
            continue
        shutil.rmtree(run, ignore_errors=True)
        removed += 1
    return removed


# an oracle trace is keyed by the port build: each build strands the last one (10-27: 1006 keys, 7.6 GB, none removed)
KEEP_ORACLE_HOURS = 24


def prune_oracle(cfg: dict[str, Any], hours: float = KEEP_ORACLE_HOURS, now: float | None = None) -> int:
    """Remove the cached oracle traces (and half-built `.tmp` ones of killed plays) nobody has used for `hours`: a hit
    refreshes the entry's time, a rebuilt port never hits the old key again. Returns the entries removed."""
    cache = cfg["runs"] / "oracle-state"
    if not cache.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - hours * 3600
    removed = 0
    for entry in cache.iterdir():
        with contextlib.suppress(OSError):
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
    return removed


def run_rows(cfg: dict[str, Any]) -> list[tuple[Path, dict[str, Any]]]:
    """Every replay folder with its record, oldest first; abandoned plays read as killed."""
    plays = cfg["runs"] / "plays"
    rows = []
    for run in sorted(plays.iterdir()) if plays.is_dir() else []:
        row = read_record(run)
        if row is None:
            continue
        if row.get("status") == "running" and not _record_live(run, row):
            row = _abandoned(row, "play owner disappeared")
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


def previous_run(
    cfg: dict[str, Any], name: str, build: str, before: str, pak: str = "on", cpak: str = "none"
) -> Path | None:
    """The newest finished run of `name` on this build with the same Expansion Pak setting (by folder name, older
    than `before`) with a hash file."""
    for run, row in reversed(run_rows(cfg)):
        if run.name >= before or row.get("movie") != name or row.get("build") != build:
            continue
        if row.get("pak", "on") != pak or row.get("cpak", "none") != cpak:
            continue
        if row.get("taken"):
            continue
        if (
            row.get("status") == "done"
            and not row.get("stopped")
            and tas_select.completed_outcome(row)
            and (run / HASH).is_file()
        ):
            return run
    return None


def same_hashes(a: Path, b: Path) -> bool:
    try:
        return (a / HASH).read_bytes() == (b / HASH).read_bytes()
    except OSError:
        return False


def input_address(run: Path) -> str:
    """The run's live input socket, `@name` in the Linux abstract namespace (a run folder path can be longer than a
    socket path may be): the port binds it, `tas serve` sends to it."""
    return "@abu-tas-" + hashlib.sha1(str(run.resolve()).encode()).hexdigest()[:24]


def reach_address(run: Path) -> str:
    """The run's reach stream, the datagram socket the port sends its reach records on: the input socket's name +
    `.reach`."""
    return input_address(run) + ".reach"


def reach_rows(cfg: dict[str, Any]) -> Path | None:
    """The baked fnrows file (one function name per fns.json row) the port maps its entered functions to, or None
    before the first bake."""
    site = cfg["runs"] / "site"
    try:
        return site / json.loads((site / "manifest.json").read_text())["fnrows"]
    except (OSError, ValueError, KeyError):
        return None


def human_movie_name(now: datetime | None = None) -> str:
    return "play-" + (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")


_TAKE_SUFFIX = re.compile(r"(?:-take-\d{8}-\d+)+$")
_TOOK_OVER = re.compile(r"live input took over at poll (\d+)")


TAKE_DATAGRAM, RELEASE_DATAGRAM = b"take", b"release"  # the control datagrams a run accepts on its input socket


def send_input(run: Path, data: bytes) -> None:
    """Send one datagram (a pad line, TAKE_DATAGRAM or RELEASE_DATAGRAM) to the run's input socket; OSError when
    nothing listens there yet."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.sendto(data, "\0" + input_address(run)[1:])


def take_root(name: str) -> str:
    """The movie a chain of takes started from: `boot-take-20261023-010101` and its takes all root at `boot`."""
    return _TAKE_SUFFIX.sub("", name) or name


def took_over_at(run: Path) -> int:
    """Polls the run's movie played before live input took over (the port says so in the run's output), 0 if never."""
    try:
        match = _TOOK_OVER.search((run / "output.txt").read_text(errors="replace"))
    except OSError:
        return 0
    return int(match[1]) - 1 if match else 0


def save_taken(
    profile: Profile,
    cfg: dict[str, Any],
    run: Path,
    movie: str,
    human: bool,
    pak: str = "on",
    cpak: str = "none",
    start: str | None = None,
    prefix: int = 0,
) -> str | None:
    """Save a run that live input drove as a movie: the human's run under its own name, a takeover (or a retry) as a
    branch `<root>-take-STAMP` of its movie's root, with `from` = the movie it left and `at` = the poll it left it at.
    None when it reached no poll."""
    source = run / "polls.raw"
    if not source.is_file() or not read_raw(source):
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    name = movie if human else f"{take_root(movie)[:40]}-take-{stamp}"
    note = f"live input from the TAS page, run {run.name}" + ("" if human else f", taken over from {movie}")
    try:
        kept = run / CARD_START
        return save(
            profile,
            name,
            source=source,
            note=note,
            replace=False,
            pak=pak,
            cpak=cpak,
            cpak_image=kept if kept.is_file() else None,
            parent=None if human else movie,
            at=None if human else took_over_at(run),
            start=start,
            prefix=prefix,
        )["name"]
    except DecompError:
        return None


def fn_of(result: dict[str, Any]) -> str | None:
    """The function a non-clean outcome names: its subject (`func_X` or `func_X+0x10`), else the owner of the fault."""
    if result.get("function"):
        return str(result["function"])
    for text in (str(result.get("subject") or ""), str(result.get("owner") or "")):
        match = re.match(r"(func_[0-9A-Fa-f]{8}\w*?)(?:\+0x[0-9a-fA-F]+)?(?:\s|$)", text)
        if match:
            return match[1]
    return None


SHOTS, RAM_DUMP, DRAW_TRACE = "shots", "ram-{}.bin", "draws-{}.txt"


def check_inspect(shots_every: int, dump_ram_at: int | None, trace_draws: int | None) -> None:
    if shots_every < 0 or any(v is not None and v < 0 for v in (dump_ram_at, trace_draws)):
        raise DecompError("tas.play", "--shots-every, --dump-ram-at and --trace-draws take poll numbers >= 0")


def inspect_env(run: Path, shots_every: int, dump_ram_at: int | None, trace_draws: int | None) -> dict[str, str]:
    """The port's inspection variables for one run: pictures every N polls, guest RAM at poll P and the draw trace of
    the first graphics task from poll P (n64compat n64c_tas.h). Everything lands in the run folder the page serves."""
    env: dict[str, str] = {}
    if shots_every:
        (run / SHOTS).mkdir(exist_ok=True)
        env.update(N64C_TAS_SHOT_EVERY=str(shots_every), N64C_TAS_SHOT_DIR=str(run / SHOTS))
    if dump_ram_at is not None:
        env.update(N64C_TAS_DUMP_AT=str(dump_ram_at), N64C_TAS_DUMP_PATH=str(run / RAM_DUMP.format(dump_ram_at)))
    if trace_draws is not None:
        env["N64C_TAS_TRACE_DRAWS"] = f"{trace_draws}:{run / DRAW_TRACE.format(trace_draws)}"
    return env


def inspect_files(run: Path, shots_every: int, dump_ram_at: int | None, trace_draws: int | None) -> list[str]:
    """The inspection files a finished run holds, by path (a run that ended before its poll holds none of that kind)."""
    found = sorted(str(p) for p in (run / SHOTS).glob("poll-*.ppm")) if shots_every else []
    if dump_ram_at is not None:
        found += [str(run / RAM_DUMP.format(dump_ram_at))] * (run / RAM_DUMP.format(dump_ram_at)).is_file()
    if trace_draws is not None:
        found += [str(run / DRAW_TRACE.format(trace_draws))] * (run / DRAW_TRACE.format(trace_draws)).is_file()
    return found


slots.CANCEL = lambda: _check_play_signal()


def _admitted(fn):
    """Hold the host slot pool while a play runs (module slots): the owner's watched play (no movie, a retry or a
    take) is class owner and always gets a slot; a play a person watches (real time or k times, `speed` != 0) is
    interactive; every other play is automated (a corpus or lane batch, a turbo replay) and takes class background;
    `slot_class` (play_all passes background) or ABU_SLOT_CLASS overrides; the oracle beside the port takes a second
    slot. The weight is also the cap: the port and the oracle run pinned to the cores of the slots held, however many
    threads they start (module slots)."""

    @functools.wraps(fn)
    def inner(profile, name, *args, **kwargs):
        klass = kwargs.pop("slot_class", None)
        if name is None or kwargs.get("take") or kwargs.get("source") is not None or kwargs.get("label") == HUMAN_LABEL:
            klass = "owner"
        weight = 2 if kwargs.get("oracle") else 1  # the port and the oracle: one core each, enforced by the hold
        what = name or kwargs.get("start") or "human"
        watched = bool(kwargs.get("speed", 0))
        with slots.hold(klass or ("interactive" if watched else "background"), weight, f"play {what}"):
            result = fn(profile, name, *args, **kwargs)
        if name is not None:  # outside the hold: the bake takes its own batch slot
            from abu_decomp import tas_bake

            tas_bake.movie_hook(profile, name)
        return result

    return inner


@_play_signals
@_admitted
def play(
    profile: Profile,
    name: str | None,
    *,
    speed: int = 0,
    now: datetime | None = None,
    health: dict[str, Any] | None = None,
    run_id: str | None = None,
    port: Path | str | None = None,
    label: str | None = None,
    shots_every: int = 0,
    dump_ram_at: int | None = None,
    trace_draws: int | None = None,
    pak: str | None = None,
    oracle: bool = False,
    oracle_pictures: bool = False,
    cpak: str | None = None,
    source: str | None = None,
    take_at: int | None = None,
    fps_unlock: bool = False,
    batch: str | None = None,
    start: str | None = None,
    take: bool = False,
    state_diff: bool = True,
    sanitize: bool = False,
    supervise: bool = True,
    first_pass: bool = False,
) -> dict[str, Any]:
    """Replay one movie at `speed` (0 = turbo), visible on the page; the result says whether its RAM was the same
    every poll as the movie's previous run on this build (`same_twice`). `port` runs another build's executable (a
    lane's own build), `label` tags the run with the lane that started it; both show on the page. `oracle` replays
    the same movie in the original game beside it (module tas_oracle); with `oracle_pictures` it pictures both at the
    same polls and scores them poll by poll (no frame is dumped otherwise); the result's `oracle` says how it ended
    and the worst score (`max_diff`, at `max_poll`).

    `pak` ("on"/"off") sets the Expansion Pak: the movie's own by default, `on` for a new run. `cpak` ("on"/"off") sets
    the Controller Pak the same way: a replay starts from a copy of the card its movie started with (`on` over a movie
    without one: a fresh card), a new run uses the profile's own card file.
    Every run listens for live input (`input_address`, module tas_serve's /api/take and /api/input): once it is taken
    over the recording goes on, and at the end its polls are saved as a new movie (`saved`). `name` None is a new
    human run: a fresh boot at speed 1 driven by live input from the first poll, saved as `play-STAMP`.
    `source` (a run id) with `take_at` P retries that run from poll P: its own recorded polls up to P replay at turbo
    with its Expansion Pak and starting card, then live input drives the run from poll P on (port N64C_TAS_TAKE_AT),
    so the retry is exact; it is saved as a branch of the run's movie like any takeover.
    `fps_unlock` runs the port with the framerate unlock (module docstring): presentation only, same run.
    `start (a start point, module tas_points) plays the point's polls first: alone when `name` is None (the point
    itself), else before the movie's own polls (instead of the start the movie names). With `take`, `name` None and
    `start` the run replays the point at turbo and hands live input over at its end: a new take from that point,
    saved as `play-STAMP` with the header `start`, without the point's polls.
    A movie run also compares the game state with the original's, region by region (module tas_statediff): the result's
    `state_diff` counts the divergent regions, RUN/state-diff.json names each one's first differing field, and the
    findings go to `<tas.runs>/findings.jsonl`; `state_diff=False` skips it.
    `supervise` False runs the port without the gdb bridge (no stacks, no stop subject: about the cost of the port
    alone); `first_pass` then raises NeedsSupervisor, after deleting the run, for any outcome but ok, so the caller
    replays that movie under the bridge and gets the full classification (`play --all` does this).
    `sanitize` runs the sanitizer port (profile tas.sanitizer.port, or `port`): reports become findings.jsonl lines
    (module tas_sanitize) and the result's `sanitizer` counts them; the run is tagged `sanitizer` on the page."""
    cfg = settings(profile)
    reconcile_runs(cfg)
    prune_plays(cfg)
    prune_oracle(cfg)
    if sanitize and port is None:
        if not cfg.get("sanitizer_port"):
            raise DecompError("tas.sanitize", "no sanitizer port: set tas.sanitizer.port in the profile or pass --port")
        port = cfg["sanitizer_port"]
    cfg = with_port(cfg, port)
    if port is None:
        tas_seat.check(cfg)
    label = label or ("sanitizer" if sanitize else None)
    check_label(label)
    if not 0 <= speed <= 1000:
        raise DecompError("tas.play", f"speed is 0 (turbo) .. 1000, not {speed}")
    began = time.monotonic()
    health = tas_health.read(cfg, profile) if health is None else health
    check_inspect(shots_every, dump_ram_at, trace_draws)
    check_pak(pak)
    check_cpak(cpak)
    if take and (not start or name is not None or source is not None):
        raise DecompError("tas.play", "--take starts a new run at a start point: give --from POINT and no movie")
    if start and not take and name is None:
        name = start  # the point alone, replayed like a movie
        start = None
    retry = retry_source(cfg, source, take_at) if source is not None else None
    if take:
        point = tas_points.load(cfg, str(start))
        mode_ = point["header"].get("cpak", "none")
        retry = {
            "movie": human_movie_name(now),
            "polls": point["polls"],
            "pak": point["header"].get("pak", "on"),
            "cpak": mode_,
            "card": start_card(cfg, mode_),
            "start": start,
            "prefix": len(point["polls"]),
            "pads": pad_count(point["polls"]),
            "fresh": True,
        }
        take_at = len(point["polls"])
    human = name is None and retry is None
    if retry and (oracle or shots_every):
        raise DecompError("tas.retry", "a retry is driven by live input from its poll on: no oracle, no shots")
    if oracle and human:
        raise DecompError("tas.oracle", "the oracle replays a movie; a human run has none")
    if oracle and oracle_pictures and shots_every:
        raise DecompError(
            "tas.oracle", "--oracle-pictures pictures the port every tas.oracle.shot_every polls (shots/port)"
        )
    original = tas_oracle.required(profile) if oracle else None
    if original and oracle_pictures:
        original["pictures"] = True
    if retry:
        name, polls = retry["movie"], retry["polls"]
        speed, label, pak = 0, label or HUMAN_LABEL, retry["pak"]
    elif human:
        speed, label = 1, label or HUMAN_LABEL
        name = human_movie_name(now)
        polls = []
    else:
        polls = movie_polls(cfg, name, start)
    if not retry and not human:
        own_start = start or decode(_movie(cfg, name).read_text())[0].get("start")
        started = (own_start, len(tas_points.load(cfg, own_start)["polls"])) if own_start else (None, 0)
    else:
        started = (retry["start"], retry["prefix"]) if retry else (None, 0)
    pak = pak or ("on" if human else movie_pak(cfg, name, start))
    pads = retry["pads"] if retry else pad_count(polls)
    run = cfg["runs"] / "plays" / (run_id or new_run_id(cfg, name, now))
    user_dir = run / "user"
    user_dir.mkdir(parents=True)
    card, mode, oracle_card = None, "none", None
    if retry:
        mode = retry["cpak"]
        if retry["card"] is not None:
            card = run / CARD
            card.write_bytes(retry["card"])
            (run / CARD_START).write_bytes(retry["card"])
    elif human:
        if cpak != "off":
            card = cfg["runs"] / PROFILE_CARD  # the owner's own card: the game's saves stay in it
            if card.exists():
                start = card.read_bytes()
                if len(start) != CARD_BYTES:
                    raise DecompError("tas.cpak", f"{card} is not a 32 KiB Controller Pak image")
            else:
                start = fresh_card()
            (run / CARD_START).write_bytes(start)
            mode = card_mode(start)
    else:
        mode = movie_cpak(cfg, name, start)
        if cpak == "off":
            mode = "none"
        elif cpak == "on" and mode == "none":
            mode = "fresh"
        start = start_card(cfg, mode)
        if start is not None:
            card, oracle_card = run / CARD, run / "oracle-cpak.mpk"
            card.write_bytes(start)
            (run / CARD_START).write_bytes(start)
            oracle_card.write_bytes(start)
    env = {
        **port_env(cfg, run),
        "N64C_TAS_RECORD": str(run / "polls.raw"),
        "N64C_TAS_HASH": str(run / HASH),
        "N64C_VIEW": str(run / VIEW),
        # Keep readbacks within the realtime frame budget, including human input and takeovers.
        "N64C_VIEW_FPS": str(cfg["env"].get("N64C_VIEW_FPS", "30")),
        "N64C_FAULT_FRAME": str(run / tas_fault.FILE),  # a host fault leaves its signal frame here (shim)
        "N64C_SPEED": str(speed),
        "N64C_INPUT": input_address(run),
        "N64C_AUDIO_OUT": str(run / AUDIO),
    }
    rows = reach_rows(cfg)
    if rows and rows.is_file():
        env["N64C_REACH_ROWS"] = str(rows)  # the port streams the functions the run enters (reach_address)
    if pads > 1:
        env["N64C_PADS"] = str(pads)  # the guest sees that many controllers; every movie line holds that many pads
    if human:
        (run / TAKEN).write_text("new run\n")
    else:
        (run / "movie.raw").write_text(format_raw(polls))
        env.update(N64C_TAS_PLAY=str(run / "movie.raw"), N64C_TAS_EXIT_AFTER=str(cfg["exit_after"]))
    if retry:
        where = f"start point {start}" if retry.get("fresh") else f"retry of {source}"
        (run / TAKEN).write_text(f"{where} from poll {take_at}\n")
        env.update(N64C_TAS_TAKE_AT=str(take_at))
    inspect = inspect_env(run, shots_every, dump_ram_at, trace_draws)
    env.update(inspect)
    if sanitize:  # recoverable reports: one run lists many (tas_sanitize)
        env.update({k: v for k, v in tas_sanitize.options_env().items() if k not in os.environ})
    build = build_id(cfg["port"])
    # At real speed a movie takes its own length in wall time; the timeout is the allowance beyond that.
    timeout = cfg["timeout_seconds"] + ((len(polls) + cfg["exit_after"]) / (60 * speed) if speed else 0)
    timeout *= tas_sanitize.TIMEOUT_FACTOR if sanitize else 1
    record = {
        "id": run.name,
        "movie": name,
        "speed": speed,
        "polls": len(polls),
        "build": build,
        "code": code_hash(cfg["port"]),
        "status": "running",
        "started": time.time(),
        "pid": os.getpid(),
        "port": str(cfg["port"]),
        "port_provenance": tas_seat.record(cfg, Path(cfg["port"]), build, port is not None),
        "label": label or None,
        "input": "human" if human or retry else "movie",
        "pak": pak,
        "cpak": mode,
        "fps_unlock": bool(fps_unlock),
        "sanitizer": sanitize or None,
    }
    if (pins := tas_select.read_pins(cfg)) is not None:
        record[tas_select.PINS] = pins  # what the port was built from: the next selective replay diffs against it
    if pads > 1:
        record["pads"] = pads
    if started[0]:
        record.update({"start": started[0], "prefix": started[1]})
    if retry and source is not None:
        record.update({"from": source, "at": take_at})
    with _run_record(run, record) as (children, reapers):
        state = None
        if state_diff and not sanitize and not human and not retry:
            try:
                with _starting_play_process():
                    state = tas_statediff.begin(profile, cfg, run, pak, card, polls, pads, attach=bool(original))
                    if state and state.process:
                        children.append(state.process)
                        if state._waiter:
                            reapers.append(state._waiter)
            except (DecompError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                record["state_diff"] = {"error": str(error)}
            if state:
                env.update(state.port_env())
            elif "state_diff" not in record and (why := tas_statediff.available(profile)):
                record["state_diff"] = {"skipped": why}  # said once per run, not a silent "no trace" after a replay
        oracle_process, oracle_end, oracle_waiter = None, {}, None
        if original:
            env.update(tas_oracle.port_env(original, run))
            record["oracle"] = {"status": "running", "every": original["shot_every"], "host": str(original["host"])}
            oracle_began = time.monotonic()
            with _starting_play_process():
                oracle_process = tas_oracle.start(
                    original,
                    run,
                    cfg["exit_after"],
                    pak,
                    extra=state.oracle_env() if state and state.attached else None,  # one host: pictures and trace
                    card=oracle_card,
                    pads=pads,
                )
                children.append(oracle_process)
                record["oracle"]["pid"] = oracle_process.pid

            def reap() -> None:  # reaped as soon as it ends, so the page sees it done while the port goes on
                code, cpu = _wait(oracle_process, original["timeout_seconds"])
                oracle_end.update(code=code, cpu=cpu, wall=round(time.monotonic() - oracle_began, 1))

            oracle_waiter = threading.Thread(target=reap, daemon=True)
            oracle_waiter.start()
            reapers.append(oracle_waiter)
            if state and state.attached:
                state.join_oracle(oracle_end, oracle_waiter)
        _write_record(run, record)
        _check_play_signal()
        argv = _argv(cfg, user_dir, pak, card, fps_unlock)
        argv = supervised(cfg, run, argv) if supervise else argv
        with (run / "output.txt").open("w") as output:
            with _starting_play_process():
                process = subprocess.Popen(
                    argv,
                    env=env,
                    stdin=subprocess.PIPE if supervise else subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    **procguard.owned(),
                )
                children.append(process)
                record["pgid"] = process.pid
                _write_record(run, record)
            # The bridge holds a stopped port until its stdin says "continue"; a closed pipe ends it after the stacks.
            if supervise:
                process.stdin.close()
            code, cpu = _wait(process, timeout, run)
        text = (run / "output.txt").read_text(errors="replace")
        reached = len(parse_raw((run / "polls.raw").read_text())) if (run / "polls.raw").is_file() else 0
        result = {"movie": name, "run": str(run), "id": run.name, "speed": speed, "polls": len(polls)}
        result.update(polls_reached=reached, **classify(code, text))
        found = inspect_files(run, shots_every, dump_ram_at, trace_draws)
        if found:
            result["inspect"] = found
        if sanitize or tas_sanitize.present(text):
            result["sanitizer"] = tas_sanitize.collect(cfg, run, text, movie=name, port=cfg["port"], build=build)
        try:  # RUN/coverage.json: this run's gcovr report (nothing without a coverage build)
            if tas_coverage.run_report(profile, cfg, run):
                result["coverage"] = str(run / tas_coverage.COVERAGE)
        except DecompError as error:
            result["coverage_error"] = str(error)
        stopped = (run / "stopped").exists()
        if stopped:
            result["subject"] = "stopped from the page"
        result["wall_seconds"], result["cpu_seconds"] = round(time.monotonic() - began, 1), cpu
        stacks = [str(p) for p in sorted(run.glob("*-st*.txt")) + sorted(run.glob("*-threads.txt"))]
        if result["outcome"] == "crash" and (fault := tas_fault.capture(run, cfg["port"])):
            result["fault"] = fault
            stacks.insert(0, str(run / tas_fault.STACK))  # the signal frame's own stack ranks before the supervisor's
        if stacks:
            result["stacks"] = stacks
        # Kind, blocking, owner and one next command (the arity note comes first when set).
        result.update(tas_symbolize.claim(profile, result, stacks, health))
        if health:
            result["build"] = tas_health.header(health)
        before = previous_run(cfg, name, build, run.name, pak, mode) if build and not stopped else None
        result["same_twice"] = same_hashes(run, before) if before else None
        if before:
            result["compared_with"] = before.name
        result["fn"] = fn_of(result)
        result["cpak"] = mode
        result["shown"] = fps_shown(text)
        taken = (run / TAKEN).is_file()
        if taken:
            result.update(
                taken=True,
                same_twice=None,
                compared_with=None,
                saved=save_taken(
                    profile, cfg, run, name, human or bool(retry and retry.get("fresh")), pak, mode, *started
                ),
            )
        keep = ("outcome", "subject", "polls_reached", "kind", "same_twice", "compared_with", "fn", "wall_seconds")
        keep += (
            "taken",
            "saved",
            "coverage",
            "frame",
            "fault",
            "shown",
            "state_diff",
            "sanitizer",
            "function",
            "source",
            "source_precision",
            "instruction",
            "fcsr",
            "claim",
            "owner",
        )
        if state:
            try:
                result["state_diff"] = tas_statediff.finish(state, profile, name, {**record, "batch": batch})
            except (DecompError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                result["state_diff"] = {"error": str(error)}
        elif "state_diff" in record:
            result["state_diff"] = record["state_diff"]
        if oracle_waiter:
            oracle_waiter.join()
            wall, polls = oracle_end["wall"], tas_oracle.reached(run)
            record["oracle"] = {
                **record["oracle"],
                **tas_oracle.outcome(oracle_end["code"]),
                "status": "done",
                "polls_reached": polls,
                "wall_seconds": wall,
                "cpu_seconds": oracle_end["cpu"],
                "fps": round(polls / wall, 1) if wall else None,
            }
            if note := tas_oracle.startup_note(oracle_end["code"], polls):
                record["oracle"]["startup_fault"] = note
            if os.environ.get("N64C_TRACE_EVENTS"):
                record["oracle"]["events"] = tas_oracle.split_events(run)  # the outcome above is gdb's then
            result["oracle"] = {**record["oracle"], **(tas_oracle.summary(run, record) or {})}
        if oracle_waiter:
            record["oracle"]["verdict"] = result["oracle"]["verdict"] = tas_oracle.verdict(result)
        if first_pass and result["outcome"] != "ok":
            raise NeedsSupervisor(run)
        final = {
            **record,
            **{k: result.get(k) for k in keep},
            "status": "done",
            "stopped": stopped,
            "ended": time.time(),
        }
        _check_play_signal()
        _write_record(run, final)
        if not human:  # this run's report replaces the movie's older ones in the union: they are dead disk
            with contextlib.suppress(DecompError, OSError):
                tas_coverage.drop_superseded(cfg, run)
        if (
            not human
        ):  # a crash, stop, hang or exit is a finding (tas_findings; sanitized: crash-sanitized); clean clears
            marked = {**final, "id": run.name, "sanitizer": final.get("sanitizer") or sanitize}
            tas_findings.record_run(profile, cfg, marked, run, batch)
        if not human and not retry and not sanitize:  # a run reaching new code keeps its movie (tas_corpus)
            from abu_decomp import tas_corpus

            try:
                kept = tas_corpus.offer(profile, settings(profile), run, final)
            except (DecompError, OSError) as error:
                kept = {"error": str(error)}
            if kept:
                result["corpus"] = kept
        return result


def play_twice(
    profile: Profile,
    name: str | None,
    *,
    speed: int = 0,
    port: Path | str | None = None,
    label: str | None = None,
    pak: str | None = None,
    oracle: bool = False,
    oracle_pictures: bool = False,
    cpak: str | None = None,
    fps_unlock: bool = False,
    start: str | None = None,
    take: bool = False,
    state_diff: bool = True,
    sanitize: bool = False,
) -> dict[str, Any]:
    """Two replays back to back; the second's `same_twice` compares it with the first."""
    if take:
        raise DecompError("tas.play", "--take is a live run; it cannot be replayed twice")
    if name is None and start:
        name, start = start, None
    options = {
        "speed": speed,
        "port": port,
        "label": label,
        "pak": pak,
        "oracle": oracle,
        "oracle_pictures": oracle_pictures,
        "cpak": cpak,
        "fps_unlock": fps_unlock,
        "start": start,
        "state_diff": state_diff,
        "sanitize": sanitize,
    }
    play(profile, name, **options)
    return play(profile, name, **options)


def compare(recorded: dict[str, Any] | None, now: dict[str, Any]) -> str:
    """better / same / worse / new: a clean end beats any stop, then more polls reached beats fewer."""
    if now.get("oracle", {}).get("verdict") == "fail":
        return "worse"
    if not recorded:
        return "new"

    def standing(row: dict[str, Any]) -> tuple[int, int]:
        return (
            int(row.get("outcome") == "ok" and row.get("oracle", {}).get("verdict") != "fail"),
            int(row.get("polls_reached") or 0),
        )

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
    if result.get("selected"):
        lines.append(f"  selective replay: {result['selected']['why']}")
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
    if result.get("coverage"):
        lines.append(result["coverage"])
    if "ranked" in result:
        lines.append(tas_rank.render(result["ranked"]))
    return "\n".join(lines)


ALL_JOBS = 4  # `play --all` default parallelism: 33-47 load on the rig came from a full-capacity sweep (10-25)
ALL_NICE = 10


def all_jobs() -> int:
    """Default parallel replays of `play --all`: the slot pool capacity, at most ALL_JOBS (env ABU_TAS_JOBS overrides;
    an explicit --jobs is taken as given)."""
    try:
        wanted = int(os.environ.get("ABU_TAS_JOBS", ""))
    except ValueError:
        wanted = ALL_JOBS
    return max(1, min(slots.capacity(), wanted if wanted > 0 else ALL_JOBS))


def _lower_priority(level: int = ALL_NICE) -> None:
    """A sweep yields the CPU to lane builds and the train: this process and every replay it starts run at nice
    ALL_NICE or lower priority already (the children inherit it)."""
    try:
        if os.nice(0) < level:
            os.nice(level - os.nice(0))
    except OSError:
        pass


@_play_signals
def play_all(
    profile: Profile,
    *,
    jobs: int = 0,
    now: datetime | None = None,
    twice: bool = False,
    run_ids: dict[str, str] | None = None,
    changed: bool = False,
    resume: bool = False,
    shard: str | None = None,
    nice: int = 0,
    gdb_all: bool = False,
    **options: Any,
) -> tuple[dict[str, Any], int]:
    """Replay every saved movie in parallel at turbo, block until done, one row per movie against its recorded
    standing. `twice` replays each movie twice (same_twice then compares the two); `run_ids` names the run folders
    (the page knows them before the runs start). `options` are `play`'s own (speed, port, label, pak, cpak, oracle,
    fps_unlock and the inspection options), applied to every movie.

    The recorded standing is the best row so far in `runs/standing.json`, moved only when a movie is not worse, so a
    regression stays `worse` until fixed. Exit status 1 when any movie is worse.
    Speed: `jobs` 0 (the default) is the host's slot pool capacity, at most 4 (`all_jobs`, module slots;
    every replay still waits for its slot, so other work is never starved).
    A movie first runs without the gdb bridge; one that does not end ok is
    deleted and replayed under the bridge, so every outcome, subject and stack is the bridge's (`gdb_all` runs every
    movie under it as before). `changed` replays only the movies the sources changed since their last good run can
    have touched (module tas_select); the others keep their recorded standing and say `skipped` with that run."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cfg = settings(profile)
    names = sorted(p.stem for p in cfg["movies"].glob("*.tas")) if cfg["movies"].is_dir() else []
    names = in_shard(names, shard)
    health = tas_health.read(cfg, profile)
    skipped: dict[str, tuple[Path, dict[str, Any]]] = {}
    why_skipped = ""
    if changed or resume:
        newest: dict[str, tuple[Path, dict[str, Any]]] = {}
        code = code_hash(port_of(cfg)) if resume else ""
        for run, row in run_rows(cfg):  # oldest first: the newest good plain movie run of each movie wins
            if (
                row.get("input") == "movie"
                and row.get("status") == "done"
                and row.get("outcome")
                and not (row.get("stopped") or row.get("taken") or row.get("sanitizer") or row.get("start"))
                and (not resume or equivalent_build(row, code))  # resume: a run on equal native code is this build's
            ):
                newest[row["movie"]] = (run, row)
        if resume:
            names_to_run, skipped, why_skipped = tas_select.resume_plan(names, newest)
        else:
            names_to_run, skipped, why_skipped = tas_select.plan(profile, cfg, names, newest)
    else:
        names_to_run = names
    jobs = jobs or all_jobs()
    _lower_priority(nice or ALL_NICE)
    options.setdefault("slot_class", "background")
    options.setdefault("batch", _stamp(now) + "-all")  # the findings of this run share one batch id
    began, cpu0 = time.monotonic(), resource.getrusage(resource.RUSAGE_CHILDREN)

    def one(name: str) -> dict[str, Any]:
        try:
            if twice:
                play(profile, name, now=now, health=health, **options)
            run_id = (run_ids or {}).get(name) if not twice else None
            if not twice and not gdb_all and not options.get("sanitize"):
                try:
                    return play(
                        profile,
                        name,
                        now=now,
                        health=health,
                        run_id=run_id,
                        supervise=False,
                        first_pass=True,
                        **options,
                    )
                except NeedsSupervisor as failed:
                    shutil.rmtree(failed.run, ignore_errors=True)  # the bridge's replay takes its place
            return play(profile, name, now=now, health=health, run_id=run_id, **options)
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
    for name, (run, last) in skipped.items():  # unaffected: the last good run stands
        done[name] = {
            "movie": name,
            "outcome": last["outcome"],
            "polls_reached": last.get("polls_reached", 0),
            "polls": last.get("polls", 0),
            "run": str(run),
            "skipped": run.name,
            "wall_seconds": 0,
            "cpu_seconds": 0,
        }
    with ThreadPoolExecutor(max_workers=max(1, min(jobs, len(names_to_run) or 1))) as pool:
        for future in as_completed([pool.submit(one, name) for name in names_to_run]):
            row = future.result()
            progress(row)  # one row per movie as each finishes; the table and JSON below are unchanged
            done[row["movie"]] = row
    results = [done[name] for name in names]
    sanitize = bool(options.get("sanitize"))
    path = cfg["runs"] / "standing.json"
    try:
        standing = json.loads(path.read_text())
    except (OSError, ValueError):
        standing = {}
    rows = []
    for result in results:
        recorded = standing.get(result["movie"])
        verdict = compare(recorded, result)
        if verdict != "worse" and not sanitize:  # the standing is the page port's
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
                "skipped": result.get("skipped"),
                "same_twice": result.get("same_twice"),
                "fn": result.get("fn"),
                "frame": result.get("frame", ""),
                "wall_seconds": result.get("wall_seconds", 0),
                "cpu_seconds": result.get("cpu_seconds", 0),
                "sanitizer": result.get("sanitizer"),
            }
        )
    cpu1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = round(cpu1.ru_utime + cpu1.ru_stime - cpu0.ru_utime - cpu0.ru_stime, 1)
    cfg["runs"].mkdir(parents=True, exist_ok=True)
    if not sanitize:
        path.write_text(json.dumps(standing, indent=1, sort_keys=True) + "\n")
    worse = [r["movie"] for r in rows if r["vs_recorded"] == "worse"]
    timing = {"wall_seconds": round(time.monotonic() - began, 1), "cpu_seconds": cpu}
    ranked = ranked_work(profile, cfg, health, rows)
    result = {"build": tas_health.header(health), "movies": rows, "worse": worse, **timing, "ranked": ranked}
    if changed or resume:
        result["selected"] = {"replayed": len(names_to_run), "skipped": len(skipped), "why": why_skipped}
    try:  # the unioned coverage never fails a replay
        report = None if sanitize else tas_coverage.build(profile, cfg, jobs=jobs)
        if report:
            result["coverage"] = tas_coverage.line(report)
    except DecompError as error:
        result["coverage"] = f"coverage: {error}"
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

    try:
        standing = json.loads((cfg["runs"] / "standing.json").read_text())
    except (OSError, ValueError):
        return []
    return [{"movie": name, **row} for name, row in sorted(standing.items())]
