"""The original game beside the port: `tas play --oracle` replays the same movie in the recompiled original ROM (Cake
Taste's oracle host) and compares the two pictures poll by poll.

Section `tas.oracle`: {"host": path, "env": {...}, "shot_every": N (default 25), "render_lead": K (default 2),
"timeout_seconds": T (default 300)}.
`host` is the oracle executable (cake-taste-64 hosts/n64recomp `n64host`); `env` its fixed environment (the original
ROM `N64R_ROM`, a config folder `N64R_CONFIG` runs may share). The host speaks the port's frame protocol under N64R_
names, and the run folder gets the same files for both sides:
- the movie: `N64R_TAS_PLAY` = the run's movie.raw (poll-keyed, as the port's `N64C_TAS_PLAY`), `N64R_TAS_EXIT_AFTER`,
  and the run's Expansion Pak (`N64R_PAK` on|off, as the port's `--pak`) and Controller Pak (`N64R_CPAK` = the card
  file, its own copy; unset = no card, as the port's `--cpak`);
- the latest picture: `N64R_VIEW` = oracle.bmp (the port's `N64C_VIEW` = view.bmp), served as /oracle/<id>;
- pictures at chosen polls, only when asked (`tas play --oracle-pictures`, the page, or `pictures = true` in this
  section; default off so batch and monkey runs never fill the disk): every `shot_every` polls both write
  shots/<side>/poll-NNNNNN.ppm (`N64C_TAS_SHOT_EVERY` / `N64C_TAS_SHOT_DIR` and the N64R_ twins);
- without pictures nothing reads a rendered frame (the guest reads RDRAM, never the renderer), and the software
  Vulkan draw was ~90% of the oracle's CPU (pp-1p-match: ~930 of ~1030 s): `N64R_TAS_RENDER_LEAD` = `render_lead` has
  the host draw only the frames of the `render_lead` polls before each `shot_every` point (the latest picture,
  oracle.bmp) and before the exit; `render_lead` 0 draws every frame. With pictures every frame is drawn;
- poll timing: `N64C_TAS_TRACE` / `N64R_TAS_TRACE` = port-trace.txt / oracle-trace.txt, one "poll vi" line per poll.
The oracle runs headless (SDL offscreen: RT64 on a Vulkan headless surface, no display server) with
`N64R_CLOCK=entries`, so its guest time comes from the work done, not the host: the same movie gives the same
pictures every time, at any speed. It always runs at full speed beside the port.

`compare` scores each poll both sides pictured: `diff` is the share of pixels that differ visibly (a channel off by
32..64 of 255 or more; two offset 64-wide quantisations must both differ), 0 = the same picture. The two runs can
drift (the port reaches a screen a few polls earlier or later): `drift` is the shot offset (in polls, oracle minus
port) whose oracle picture matches the port's clearly better than the same poll's, 0 when aligned, and `vi_drift` the
VI retrace difference at that poll from the two traces. Drift is shown, never corrected away: `diff` is always the
same-poll score. Rows are cached in diff.json as both sides' pictures are complete.
"""

from __future__ import annotations

import json
import mmap
import os
import re
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any, NamedTuple

from abu_decomp import DecompError, procguard
from abu_decomp.config import Profile, check_keys

_KEYS = frozenset({"host", "env", "shot_every", "render_lead", "pictures", "timeout_seconds", "rebuild", "lock", "pins", "pins_repo"})
VIEW, LOG, DIFF = "oracle.bmp", "oracle.txt", "diff.json"
AUDIO = "audio-oracle.pcm"  # the original's sound, as the port's audio.pcm (+ ".rate")
PORT_TRACE, ORACLE_TRACE = "port-trace.txt", "oracle-trace.txt"
CALLS_FIRST = 16  # calls per (caller, function) each side records for the per-call behaviour (RUN/calls.jsonl)
SHOTS = "shots"
SIDES = ("port", "oracle")
WIDTH, HEIGHT = 320, 240
PPM_HEAD = f"P6\n{WIDTH} {HEIGHT}\n255\n".encode()
PPM_SIZE = len(PPM_HEAD) + WIDTH * HEIGHT * 3
DRIFT_SHOTS = 3  # oracle shots either side of a poll searched for a better match
DRIFT_MARGIN = 0.02  # a shifted match counts as drift when it beats the same poll by this much and by half
DIVERGE = (0.01, 0.02)  # picture-diff shares `first_divergence` reports a first poll for (1% and 2% of pixels)
_SHOT = re.compile(r"poll-(\d{6,})\.ppm")
_GRID_A = bytes(v >> 6 for v in range(256))
_GRID_B = bytes((v + 32) >> 6 for v in range(256))
_NONZERO = bytes([0] + [1] * 255)
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def settings(profile: Profile) -> dict[str, Any] | None:
    """The `tas.oracle` section, None when the profile has none (no oracle on this seat)."""
    table = profile.section("tas").get("oracle")
    if not table:
        return None
    check_keys(profile.source_file, "tas.oracle", table, frozenset({"host"}), _KEYS)
    every = int(table.get("shot_every", 25))
    if every < 1:
        raise DecompError("tas.oracle", f"shot_every is a poll count >= 1, not {every}")
    lead = int(table.get("render_lead", 2))
    if lead < 0:
        raise DecompError("tas.oracle", f"render_lead is a poll count >= 0, not {lead}")
    keys = ("pins", "pins_repo", "lock")
    stamp = {key: profile.path(f"tas.oracle.{key}", table[key]) for key in keys if key in table}
    return {
        **stamp,
        "rebuild": str(table.get("rebuild", "")),
        "host": profile.path("tas.oracle.host", table["host"]),
        "env": {str(k): str(v) for k, v in table.get("env", {}).items()},
        "shot_every": every,
        "render_lead": lead,
        "pictures": bool(table.get("pictures", False)),
        "timeout_seconds": int(table.get("timeout_seconds", 300)),
    }


def pins_lag(oracle: dict[str, Any]) -> str:
    """Why the oracle host is older than its cake-taste-64 tree (section keys `pins` = the stamp the oracle ensure
    writes, `pins_repo` = the tree), empty when current or no stamp is configured."""
    from abu_decomp import tas_health

    return tas_health.pins_lag(oracle, "cake_taste", "page oracle", "cake-taste-64")


def required(profile: Profile, fresh: bool = True) -> dict[str, Any]:
    """The configured oracle, refusing a missing host and (unless `fresh` is False, a host named by hand) one built
    before the tree's last commit: an old oracle lacks the Controller Pak and the call recorder and fails silently."""
    oracle = settings(profile)
    if oracle is None:
        raise DecompError("tas.oracle", "this profile has no tas.oracle section (the original game's host)")
    if not oracle["host"].is_file() or not os.access(oracle["host"], os.X_OK):
        raise DecompError("tas.oracle", f"no oracle host at {oracle['host']}")
    lag = pins_lag(oracle) if fresh else ""
    if lag:
        raise DecompError(
            "tas.oracle", f"{lag}; rebuild it first: {oracle['rebuild'] or '(no tas.oracle.rebuild set)'}"
        )
    return oracle


def port_env(oracle: dict[str, Any], run: Path) -> dict[str, str]:
    """What the port adds to its environment so its poll timing lines up with the oracle's and, when `pictures` is on,
    its pictures too. Without pictures no frame is dumped (a monkey batch wrote ~650 .ppm per run and filled the rig
    disk twice, 10-23): only the trace, the run's latest picture (view.bmp) and a fault's own capture remain."""
    env = {"N64C_TAS_TRACE": str(run / PORT_TRACE)}
    if oracle.get("pictures"):
        shots = run / SHOTS / "port"
        shots.mkdir(parents=True, exist_ok=True)
        env.update(N64C_TAS_SHOT_EVERY=str(oracle["shot_every"]), N64C_TAS_SHOT_DIR=str(shots))
    return env


def movie_env(play: Path, pak: str, pads: int, exit_after: int, card: Path | None) -> dict[str, str]:
    """The host variables that replay a movie: the deterministic clock (guest time from the work done), the
    Expansion Pak, the pads, the poll-keyed movie, when the host stops and the Controller Pak card, if any."""
    env = {
        "N64R_CLOCK": "entries",
        "N64R_PAK": pak,
        "N64R_PADS": str(pads),  # controllers the guest sees; each movie.raw line holds that many pads
        "N64R_TAS_PLAY": str(play),
        "N64R_TAS_EXIT_AFTER": str(exit_after),
    }
    if card:
        env["N64R_CPAK"] = str(card)
    return env


def start(
    oracle: dict[str, Any],
    run: Path,
    exit_after: int,
    pak: str = "on",
    extra: dict[str, str] | None = None,
    card: Path | None = None,
    pads: int = 1,
) -> subprocess.Popen:
    """The oracle replaying the run's movie.raw with the run's Expansion Pak setting (`N64R_PAK`) and Controller Pak
    card (`N64R_CPAK`, its own copy of the card the port starts with; unset: an empty slot), in its own process
    group, output in oracle.txt. `extra` adds host variables (the coverage run sets N64R_GOLDEN_DIR)."""
    env = {
        **os.environ,
        "SDL_VIDEODRIVER": "offscreen",
        "SDL_AUDIODRIVER": "dummy",
        "N64R_CLOCK": "entries",
        **oracle["env"],
        **movie_env(run / "movie.raw", pak, pads, exit_after, None),
        "N64R_VIEW": str(run / VIEW),
        "N64R_AUDIO_OUT": str(run / AUDIO),
        "N64R_TAS_SHOT_EVERY": str(oracle["shot_every"]),  # the host's entry-count checkpoints (behaviour) follow it
        "N64R_TAS_TRACE": str(run / ORACLE_TRACE),
        "N64R_TAS_RENDER_LEAD": "0" if oracle.get("pictures") else str(oracle["render_lead"]),
        **(extra or {}),
    }
    if oracle.get("pictures"):  # no SHOT_DIR, no .ppm: the host still writes its checkpoints
        shots = run / SHOTS / "oracle"
        shots.mkdir(parents=True, exist_ok=True)
        env["N64R_TAS_SHOT_DIR"] = str(shots)
    env.pop("N64R_CPAK", None)
    if card:
        env["N64R_CPAK"] = str(card)
    argv = [str(oracle["host"])]
    events = os.environ.get("N64C_TRACE_EVENTS")
    gdb = shutil.which("gdb") if events else None
    if events and gdb and not host_tags_polls(oracle["host"]):
        raise DecompError(
            "tas.oracle",
            f"the oracle host {oracle['host']} has no {POLL_SYMBOL}: its events would carry no poll; rebuild it "
            f"({oracle.get('rebuild') or 'no tas.oracle.rebuild set'})",
        )
    if events and gdb:
        # the event trace's twin (TAS-COVER-6 ran it by hand): dprintf lines land in oracle.txt, `split_events`
        # moves them to oracle-events.txt; the outcome is then gdb's, so the run says the trace was on
        argv = [gdb, "-q", "-nx", "-batch", "-iex", "set dprintf-style gdb", "-iex", "set pagination off"]
        for command in event_commands(events):
            argv += ["-ex", command]
        argv += ["-ex", "run", "--args", str(oracle["host"])]
    with (run / LOG).open("w") as output:
        return subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            **procguard.owned(),
            cwd=run,
        )


ORACLE_EVENTS = "oracle-events.txt"


POLL_SYMBOL = "n64host_tas_poll"  # the host's exported poll counter (cake-taste-64 golden.cpp), read by every dprintf
_EVENT = re.compile(r"^EV\|([^|@]+)@(\d+)((?:\|[^|]*)*)$")


class Event(NamedTuple):
    """One traced call: the function, the controller poll it ran after (the counter's value, so the change a state
    sample sees at poll N was written by the events tagged N-1) and its arguments as printed (integers, or the `name=value` fields a
    port-side gdb init prints)."""

    function: str
    poll: int
    args: tuple[str, ...]


def event_commands(spec: str) -> list[str]:
    """The original's dprintf lines for `--trace-events FUNC[/N],...`: a recompiled function is
    fn(uint8_t *rdram, recomp_context *ctx), so guest argument i (a0..a3) is the 64-bit register slot at ctx+32+8i
    ($rsi at entry). Lines `EV|FUNC@POLL|a0|..`, the port's format (supervisor/gdb.py): POLL is the host's own poll
    counter `n64host_tas_poll`, so the two files diff line by line and per poll."""
    out = []
    for item in (s.strip() for s in spec.split(",")):
        if not item:
            continue
        name, _, count = item.partition("/")
        n = min(int(count or 0), 4)
        fmt = "EV|" + name + "@%ld" + "|%d" * n + "\\n"
        args = [f"(long){POLL_SYMBOL}"] + [f"*(int*)($rsi+{32 + 8 * i})" for i in range(n)]
        out.append(f'dprintf *{name},"{fmt}", ' + ", ".join(args))
    return out


def host_tags_polls(host: Path) -> bool:
    """Whether the oracle host carries the poll counter the event lines read (a host built before the tag would stall
    gdb's dprintf on a missing symbol, or print events with no poll)."""
    try:
        with host.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as image:
            return image.find(POLL_SYMBOL.encode()) >= 0
    except (OSError, ValueError):
        return False


def parse_events(text: str, source: str = "events") -> list[Event]:
    """The `EV|FUNC@POLL|args` lines of a text, in order. A line without its poll is refused (an event no poll can
    place compares nothing): the same form from the port's bridge and the original's gdb."""
    events = []
    for number, line in enumerate((x for x in text.splitlines() if x.startswith("EV|")), 1):
        found = _EVENT.match(line)
        if not found:
            raise DecompError("tas.oracle", f"{source} event {number} carries no poll (want EV|FUNC@POLL|..): {line!r}")
        events.append(Event(found[1], int(found[2]), tuple(found[3].split("|")[1:])))
    return events


def read_events(path: Path) -> list[Event] | None:
    """The events a side left in `path`, None when it left no file (the run traced no events)."""
    try:
        return parse_events(path.read_text(errors="replace"), path.name)
    except FileNotFoundError:
        return None


def events_in(events: list[Event], poll: int) -> list[str]:
    """The calls tagged `poll`, in the order they ran: `FUNC(a0,..)`."""
    return [f"{e.function}({','.join(map(str, e.args))})" for e in events if e.poll == poll]


def split_events(run: Path) -> int:
    """Move the `EV|` lines of the original's output into RUN/oracle-events.txt; returns how many. Each must carry its
    poll (`parse_events` refuses the run otherwise)."""
    try:
        lines = [x for x in (run / LOG).read_text(errors="replace").splitlines() if x.startswith("EV|")]
    except OSError:
        return 0
    parse_events("\n".join(lines), LOG)
    if lines:
        (run / ORACLE_EVENTS).write_text("\n".join(lines) + "\n")
    return len(lines)


def outcome(code: int | None) -> dict[str, Any]:
    """ok / hang (killed at the timeout) / crash (a signal) / exit."""
    if code is None:
        return {"outcome": "hang"}
    if code == 0:
        return {"outcome": "ok"}
    if code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = f"signal {-code}"
        return {"outcome": "crash", "subject": name}
    return {"outcome": "exit", "subject": f"exit {code}"}


def run_facts(
    code: int | None, cpu: float | None, wall: float | None, polls: int, wanted: int, log: Path | None
) -> dict[str, Any]:
    """How one host run ended, for the run record: exit and its outcome, CPU, wall, the polls it reached of the
    `wanted` and where its output is kept."""
    return {
        **outcome(code),
        "exit": code,
        "cpu_seconds": cpu,
        "wall_seconds": wall,
        "polls": polls,
        "wanted_polls": wanted,
        "log": str(log) if log else None,
    }


def startup_note(code: int | None, polls: int) -> str:
    """A named finding for an original that died by a signal before its first poll: the host (its GPU driver, EGL, the
    loader) failed, not the game, so the run has nothing of the original to compare (TAS-COVER-2/3: an EGL SIGSEGV read
    as "the original left no state trace"). "" otherwise."""
    if code is None or code >= 0 or polls > 0:
        return ""
    return (
        f"the original's host died by {outcome(code).get('subject')} before poll 1: a host fault (GPU driver, EGL, "
        "loader), not the game; this run has no original to compare"
    )


def trace(path: Path) -> dict[int, int]:
    """poll -> VI retrace from a "poll vi" trace (a half-written last line is left out)."""
    rows: dict[int, int] = {}
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return rows
    for line in text[: text.rfind("\n") + 1].splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            rows[int(parts[0])] = int(parts[1])
    return rows


def reached(run: Path) -> int:
    rows = trace(run / ORACLE_TRACE)
    return max(rows) if rows else 0


def shots(run: Path, side: str) -> dict[int, Path]:
    """poll -> complete picture of one side (a shot still being written is left out)."""
    folder = run / SHOTS / side
    out = {}
    for path in folder.iterdir() if folder.is_dir() else []:
        match = _SHOT.fullmatch(path.name)
        try:
            complete = path.stat().st_size == PPM_SIZE
        except OSError:
            complete = False
        if match and complete:
            out[int(match[1])] = path
    return out


def pixels(path: Path) -> bytes | None:
    """The RGB bytes of a shot (P6 320x240), None when it is not one."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) != PPM_SIZE or not data.startswith(PPM_HEAD):
        return None
    return data[len(PPM_HEAD) :]


def diff_share(a: bytes, b: bytes) -> float:
    """Share of pixels that differ visibly: some channel lands in another 64-wide bucket on both of two grids offset
    by 32 (so a difference under 32 never counts and one of 64 or more always does). Whole-image integer ops."""
    if len(a) != len(b) or not a or len(a) % 3:
        raise ValueError("pictures of different sizes")
    count = len(a) // 3
    both = -1
    for grid in (_GRID_A, _GRID_B):
        changed = (int.from_bytes(a.translate(grid)) ^ int.from_bytes(b.translate(grid))).to_bytes(len(a))
        per_pixel = int.from_bytes(changed[0::3]) | int.from_bytes(changed[1::3]) | int.from_bytes(changed[2::3])
        flags = int.from_bytes(per_pixel.to_bytes(count).translate(_NONZERO))
        both &= flags
    return round((count - both.to_bytes(count).count(0)) / count, 4)


def _alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _lock(run: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(run), threading.Lock())


def compare(run: Path, every: int, oracle_done: bool) -> list[dict[str, Any]]:
    """Every poll both sides pictured, oldest first: {"poll", "diff", "drift", "vi_drift"}. A row is cached in
    diff.json once the oracle pictures it is compared with are all there (or the oracle has ended)."""
    with _lock(run):
        try:
            cached = {int(r["poll"]): r for r in json.loads((run / DIFF).read_text())["rows"]}
        except (OSError, ValueError, KeyError, TypeError):
            cached = {}
        port, orig = shots(run, "port"), shots(run, "oracle")
        if not port or not orig:
            return sorted(cached.values(), key=lambda r: r["poll"])
        last = max(orig)
        port_vi, oracle_vi = trace(run / PORT_TRACE), trace(run / ORACLE_TRACE)
        rows = dict(cached)
        settled = set(cached)
        for poll in sorted(set(port) & set(orig) - set(cached)):
            mine, same = pixels(port[poll]), pixels(orig[poll])
            if mine is None or same is None:
                continue
            score = diff_share(mine, same)
            best, drift = score, 0
            for k in range(1, DRIFT_SHOTS + 1):
                for shift in (-k * every, k * every):
                    other = pixels(orig[poll + shift]) if poll + shift in orig else None
                    if other is not None:
                        shifted = diff_share(mine, other)
                        if shifted < best:
                            best, drift = shifted, shift
            if not (best < score - DRIFT_MARGIN and best < score / 2):
                drift = 0
            vi = oracle_vi[poll] - port_vi[poll] if poll in port_vi and poll in oracle_vi else None
            rows[poll] = {"poll": poll, "diff": score, "drift": drift, "vi_drift": vi}
            if oracle_done or poll + DRIFT_SHOTS * every <= last:
                settled.add(poll)  # every neighbour it could match is there: final
        if settled != set(cached):
            tmp = run / (DIFF + ".tmp")
            tmp.write_text(json.dumps({"every": every, "rows": [rows[p] for p in sorted(settled)]}) + "\n")
            tmp.replace(run / DIFF)
        return [r for _, r in sorted(rows.items())]


def first_divergence(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Where port and oracle stop agreeing, so a lane knows from which poll an oracle comparison is no longer valid:
    per threshold in DIVERGE, the first aligned poll (drift 0: a drifted poll is the same screen reached earlier or
    later, not a divergence) whose same-poll `diff` is over it, with the diff and the VI retrace difference there.
    None when there is none."""
    out: dict[str, Any] = {}
    for share in DIVERGE:
        hit = next((r for r in rows if r["diff"] > share and not r["drift"]), None)
        out[f"diverge_{round(share * 100)}pct"] = (
            {"poll": hit["poll"], "diff": hit["diff"], "vi_drift": hit["vi_drift"]} if hit else None
        )
    return out


def summary(run: Path, record: dict[str, Any]) -> dict[str, Any] | None:
    """The `oracle` object of a runs[] row (orch plans/TAS-PAGE-CONTRACT.md), None for a run without the oracle."""
    meta = record.get("oracle")
    if not isinstance(meta, dict):
        return None
    done = meta.get("status") == "done" or not _alive(meta.get("pid"))
    every = int(meta.get("every") or 1)
    rows = compare(run, every, done)
    worst = max(rows, key=lambda r: r["diff"]) if rows else None
    latest = rows[-1] if rows else None
    return {
        "frame": f"/oracle/{run.name}",
        "status": "done" if done else "running",
        "poll": int(meta["polls_reached"]) if "polls_reached" in meta else reached(run),
        "outcome": meta.get("outcome") if done else None,
        "subject": meta.get("subject") if done else None,
        "every": every,
        "diff": latest["diff"] if latest else None,
        "diff_poll": latest["poll"] if latest else None,
        "max_diff": worst["diff"] if worst else None,
        "max_poll": worst["poll"] if worst else None,
        "drift": latest["drift"] if latest else 0,
        "vi_drift": latest["vi_drift"] if latest else None,
        "fps": meta.get("fps"),
        **first_divergence(rows),
    }


def bmp(rgb: bytes) -> bytes:
    """A 320x240 24-bit BMP of PPM pixel bytes (top row first, RGB): the picture format the page reads."""
    row = WIDTH * 3
    head = bytearray(54)
    head[0:2] = b"BM"
    head[2:6] = (54 + row * HEIGHT).to_bytes(4, "little")
    head[10:14] = (54).to_bytes(4, "little")
    head[14:18] = (40).to_bytes(4, "little")
    head[18:22] = WIDTH.to_bytes(4, "little")
    head[22:26] = HEIGHT.to_bytes(4, "little")
    head[26:28] = (1).to_bytes(2, "little")
    head[28:30] = (24).to_bytes(2, "little")
    head[34:38] = (row * HEIGHT).to_bytes(4, "little")
    body = bytearray()
    for y in range(HEIGHT - 1, -1, -1):
        line = bytearray(rgb[y * row : (y + 1) * row])
        line[0::3], line[2::3] = line[2::3], line[0::3]  # RGB -> BGR
        body += line
    return bytes(head) + bytes(body)


def verdict(record: dict[str, Any]) -> str:
    """An oracle run passes only with successful outcomes and a state comparison that covered every poll of both the
    state samples (`state_polls`) and the poll traces (`trace_polls`): equal poll sets, both reaching the movie's last
    poll, and no unreviewed divergence."""
    state = record.get("state_diff") or {}
    movie = int(record.get("polls") or 0)
    if (
        record.get("outcome") != "ok"
        or record.get("oracle", {}).get("outcome") != "ok"
        or state.get("error")
        or not state.get("polls_compared")
        or state.get("divergent") != 0
    ):
        return "fail"
    for name in ("state_polls", "trace_polls"):
        covered = state.get(name) or {}
        if (
            not covered.get("complete")
            or (covered.get("port_last") or 0) < movie
            or (covered.get("oracle_last") or 0) < movie
        ):
            return "fail"
    return "ok"
