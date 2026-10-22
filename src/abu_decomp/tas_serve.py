"""`decomp tas serve`: one web page to play the port live, record the run, and save, replay or delete movies.

Section `tas.serve`: {"listen": "HOST:PORT", "display": ":N", "fps": N}. The port runs in its own Xvfb display at the
size its `--window` argument names; ffmpeg grabs that display as MJPEG for the page, and the page's key presses go to
the port window through xdotool, so the port reads them through its normal keyboard bindings. Sound goes to a private
PulseAudio null sink (socket under `runs/pulse`) whose monitor ffmpeg encodes as an MP3 stream for the page. Every
run the page starts is a recording from reset (`N64C_TAS_RECORD`), so Save always stores a replayable movie of it.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, events, tas, tas_eyes, tas_tabs, tas_triage
from abu_decomp.config import Profile

KEYS = {"ArrowLeft": "Left", "ArrowRight": "Right", "ArrowUp": "Up", "ArrowDown": "Down", "Enter": "Return"}
_LETTER = re.compile(r"[a-z]")
_SERVE_KEYS = frozenset({"listen", "display", "fps", "ai_log"})
_SINK = "tas"
SAMPLE_SECONDS = 0.1  # the eyes keep a picture this often, stamped with the poll index
SAMPLES_KEPT = 200
PCM_SECONDS = 10
# The port's keyboard bindings (platform/n64compat/n64c_input_config.c), by controller button.
PAD_KEYS = {
    "A": "x", "B": "c", "Z": "z", "START": "Return", "L": "a", "R": "s",
    "CUP": "i", "CDOWN": "k", "CLEFT": "j", "CRIGHT": "l",
    "UP": "Up", "DOWN": "Down", "LEFT": "Left", "RIGHT": "Right",
}  # fmt: skip


def pad_keys(spec: str) -> list[str]:
    """X keysyms for a controller button name, a chord `UP+A`, or a bare keysym (`x`, `Return`)."""
    names = []
    for part in spec.split("+"):
        name = PAD_KEYS.get(part.upper()) or (part if part in PAD_KEYS.values() else None)
        if not name:
            raise DecompError("tas.press", f"unknown button {part!r}: use {', '.join(PAD_KEYS)}")
        names.append(name)
    return names


def keysym(key: str) -> str | None:
    """The X keysym for a browser `KeyboardEvent.key`, or None for keys the port does not bind."""
    if key in KEYS:
        return KEYS[key]
    return key.lower() if len(key) == 1 and _LETTER.fullmatch(key.lower()) else None


def window_size(args: list[str]) -> str:
    if "--window" not in args or args.index("--window") + 1 >= len(args):
        raise DecompError("tas.serve", "tas.args needs --window WxH so the display can match it")
    return args[args.index("--window") + 1]


class Session:
    """The one live port run: its recording and the movie driving it, if any."""

    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.cfg = tas.settings(profile)
        serve = dict(profile.section("tas").get("serve", {}))
        extra = serve.keys() - _SERVE_KEYS
        if extra:
            raise DecompError("tas.serve", f"unknown tas.serve keys: {', '.join(sorted(extra))}")
        self.listen = str(serve.get("listen", "127.0.0.1:8097"))
        self.display = str(serve.get("display", ":97"))
        self.fps = int(serve.get("fps", 30))
        self.ai_log = profile.path("tas.serve.ai_log", serve["ai_log"]) if "ai_log" in serve else None
        self.size = window_size(self.cfg["args"])
        self.lock = threading.Lock()
        self.port: subprocess.Popen | None = None
        self.recording: Path | None = None
        self.log: Path | None = None
        self.run_dir: Path | None = None
        self.exit: int | None = None
        self.ended_by_page = False
        self._claims: dict[str, Any] = {}
        self.movie: str | None = None
        self.movie_length = 0
        self.frame = b""
        self.frame_ready = threading.Condition()
        self.helpers: list[subprocess.Popen] = []
        self.pulse = self.cfg["runs"] / "pulse"
        self.listeners: list[list[bytes]] = []
        self.audio_ready = threading.Condition()
        self.counter = tas_eyes.PollCounter()
        self.counter_lock = threading.Lock()
        self.samples: deque[tas_eyes.Sample] = deque(maxlen=SAMPLES_KEPT)
        self.pcm = bytearray()
        self.pcm_lock = threading.Lock()

    def polls(self) -> int:
        """Controller polls the live run has made so far."""
        with self.counter_lock:
            return self.counter.update(self.recording)

    def _sample(self) -> None:
        while True:
            time.sleep(SAMPLE_SECONDS)
            poll = self.polls()
            with self.frame_ready:
                frame = self.frame
            if not frame:
                continue
            if self.samples and poll < self.samples[-1][0]:
                self.samples.clear()  # a new run restarted the count
            self.samples.append((poll, frame))

    def _read_pcm(self, stream) -> None:
        keep = PCM_SECONDS * tas_eyes.RATE * tas_eyes.CHANNELS * 2
        while chunk := stream.read1(16384):
            with self.pcm_lock:
                self.pcm += chunk
                del self.pcm[:-keep]

    def start_display(self) -> None:
        width, height = self.size.split("x")
        self.helpers.append(
            subprocess.Popen(
                ["Xvfb", self.display, "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        grab = subprocess.Popen(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-f",
                "x11grab",
                "-draw_mouse",
                "0",
                "-video_size",
                self.size,
                "-framerate",
                str(self.fps),
                "-i",
                self.display,
                "-f",
                "mjpeg",
                "-q:v",
                "3",
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.helpers.append(grab)
        threading.Thread(target=self._read_frames, args=(grab.stdout,), daemon=True).start()
        self.start_audio()

    def start_audio(self) -> None:
        self.pulse.mkdir(parents=True, exist_ok=True)
        socket = self.pulse / "native"
        # No D-Bus: a second page on the same user bus would lose the org.PulseAudio1 name to the first and exit.
        env = {
            **os.environ,
            "PULSE_RUNTIME_PATH": str(self.pulse),
            "PULSE_STATE_PATH": str(self.pulse),
            "DBUS_SESSION_BUS_ADDRESS": "disabled:",
        }
        self.helpers.append(
            subprocess.Popen(
                [
                    "pulseaudio",
                    "-n",
                    "--daemonize=no",
                    "--exit-idle-time=-1",
                    "--use-pid-file=no",
                    "-L",
                    f"module-native-protocol-unix socket={socket} auth-anonymous=1",
                    "-L",
                    f"module-null-sink sink_name={_SINK} rate=48000",
                    "-L",
                    "module-always-sink",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        subprocess.run(
            ["pactl", "--server", f"unix:{socket}", "info"], env=env, capture_output=True, timeout=10, check=False
        )
        encode = subprocess.Popen(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-f",
                "pulse",
                "-server",
                f"unix:{socket}",
                "-i",
                f"{_SINK}.monitor",
                "-ac",
                "2",
                "-b:a",
                "128k",
                "-f",
                "mp3",
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.helpers.append(encode)
        threading.Thread(target=self._read_audio, args=(encode.stdout,), daemon=True).start()
        raw = subprocess.Popen(
            [
                "ffmpeg", "-loglevel", "error", "-f", "pulse", "-server", f"unix:{socket}",
                "-i", f"{_SINK}.monitor", "-ac", "2", "-ar", str(tas_eyes.RATE), "-f", "s16le", "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )  # fmt: skip
        self.helpers.append(raw)
        threading.Thread(target=self._read_pcm, args=(raw.stdout,), daemon=True).start()
        threading.Thread(target=self._sample, daemon=True).start()

    def _read_audio(self, stream) -> None:
        while chunk := stream.read1(4096):
            with self.audio_ready:
                for queue in self.listeners:
                    queue.append(chunk)
                    del queue[:-64]  # a stalled listener drops old audio instead of growing
                self.audio_ready.notify_all()

    def _read_frames(self, stream) -> None:
        buffer = b""
        while chunk := stream.read1(65536):
            buffer += chunk
            while (end := buffer.find(b"\xff\xd9")) >= 0:
                start = buffer.find(b"\xff\xd8")
                if 0 <= start < end:
                    with self.frame_ready:
                        self.frame = buffer[start : end + 2]
                        self.frame_ready.notify_all()
                buffer = buffer[end + 2 :]

    def env(self) -> dict[str, str]:
        return {
            **os.environ,
            **self.cfg["env"],
            "DISPLAY": self.display,
            "SDL_VIDEODRIVER": "x11",
            "SDL_AUDIODRIVER": "pulse",
            "PULSE_SERVER": f"unix:{self.pulse / 'native'}",
            "PULSE_SINK": _SINK,
        }

    def new_run(self, movie: str | None = None) -> dict[str, Any]:
        """A visible run from reset, recorded; with a movie, the movie drives the pad until it ends and the
        page's keys take over, so the recording is the movie plus whatever is played after it."""
        polls = tas.movie_polls(self.cfg, movie) if movie else []
        with self.lock:
            self._stop_port()
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            live = self.cfg["runs"] / "live"
            user_dir = self.cfg["runs"] / "users" / stamp
            user_dir.mkdir(parents=True, exist_ok=True)
            live.mkdir(parents=True, exist_ok=True)
            self.recording, self.log, self.exit = live / f"{stamp}.raw", live / f"{stamp}.log", None
            self.ended_by_page = False
            env = {**self.env(), "N64C_TAS_RECORD": str(self.recording)}
            for key in ("N64C_TAS_PLAY", "N64C_TAS_EXIT_AFTER"):
                env.pop(key, None)
            self.movie, self.movie_length = movie, len(polls)
            if movie:
                source = live / f"{stamp}.movie.raw"
                source.write_text(tas.format_raw(polls))
                env["N64C_TAS_PLAY"] = str(source)
            self.run_dir = live / stamp
            # Under the gdb bridge a stop or host fault holds the picture on screen and writes its stacks; the
            # bridge's stdin stays open so the process is held until the next run replaces it.
            with self.log.open("w") as log:
                self.port = subprocess.Popen(
                    tas.supervised(self.cfg, self.run_dir, tas._argv(self.cfg, user_dir)),
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            threading.Thread(target=self._focus, daemon=True).start()
            return {"recording": str(self.recording)}

    def _focus(self) -> None:
        # The port is the only client of its display, so its window is the one window there.
        subprocess.run(
            ["xdotool", "search", "--sync", "--onlyvisible", "--name", ".", "windowfocus"],
            env=self.env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )

    def _stop_port(self) -> None:
        if self.port and self.port.poll() is None:
            os.killpg(self.port.pid, signal.SIGTERM)
            try:
                self.port.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(self.port.pid, signal.SIGKILL)
                self.port.wait()

    def key(self, key: str, down: bool) -> None:
        name = keysym(key)
        if name and self.port and self.port.poll() is None:
            subprocess.run(["xdotool", "keydown" if down else "keyup", name], env=self.env(), check=False)

    def _wait_polls(self, target: int, what: str) -> int:
        """Block until the run has made `target` polls (keyed to polls, not wall time); a run that stops polling
        for 10 s is an error, not a wait."""
        poll, seen = self.polls(), time.monotonic()
        while poll < target:
            time.sleep(0.005)
            now = self.polls()
            if now != poll:
                poll, seen = now, time.monotonic()
            elif time.monotonic() - seen > 10:
                raise DecompError("tas.press", f"{what}: the run made no poll for 10 s (at poll {poll})")
        return poll

    def press(self, buttons: list[str], hold: int, then: int) -> dict[str, Any]:
        """Press each button (or chord `UP+A`) in turn for `hold` polls, then wait `then` polls, so the
        recording shows exactly that input at any host speed."""
        if not (self.port and self.port.poll() is None):
            raise DecompError("tas.press", "no run is live: start one with `tas new`")
        steps = [(spec, pad_keys(spec)) for spec in buttons]
        first, rows = self.polls(), []
        for spec, keys in steps:
            start = self.polls()
            for key in keys:
                subprocess.run(["xdotool", "keydown", key], env=self.env(), check=False)
            try:
                end = self._wait_polls(start + hold, spec)
            finally:
                for key in keys:
                    subprocess.run(["xdotool", "keyup", key], env=self.env(), check=False)
            rows.append({"button": spec, "from": start, "to": end})
            self._wait_polls(end + then, spec)
        return {"pressed": rows, "from_poll": first, "polls": self.polls()}

    def look(self, frames: int, span: int, seconds: float) -> dict[str, Any]:
        if not self.run_dir:
            raise DecompError("tas.look", "no run: start one with `tas new`")
        with self.pcm_lock:
            pcm = bytes(self.pcm)
        samples = list(self.samples)
        return tas_eyes.look(
            self.run_dir / "look", samples, pcm, frames=frames, span=span, seconds=seconds, poll=self.polls()
        )

    def state(self) -> dict[str, Any]:
        running = bool(self.port and self.port.poll() is None)
        if self.port and not running:
            self.exit = self.port.returncode
        polls = len(tas.read_raw(self.recording)) if self.recording and self.recording.is_file() else 0
        text = self.log.read_text(errors="replace") if self.log and self.log.is_file() else ""
        # A debug-reason line: the gdb bridge holds the stop: process alive, picture frozen, not a hang.
        held = tas.classify(None if running and "debug-reason:" not in text else self.exit or 0, text) if text else {}
        stopped = held if held.get("outcome") in ("stop", "crash") and not self.ended_by_page else {}
        if stopped:
            stacks = [str(p) for p in sorted(self.run_dir.glob("*.txt"))] if self.run_dir else []
            key = (str(self.run_dir), stopped.get("subject"), len(stacks))
            if self._claims.get("key") != key:  # the page polls /state: classify once per stop
                self._claims = {"key": key, "claim": tas_triage.claim(self.profile, stopped, stacks)}
            stopped = {**stopped, **self._claims["claim"]}
        running = running and not stopped
        tail = text.splitlines()[-3:]
        return {
            "stopped": stopped,
            "stacks": [str(p) for p in sorted(self.run_dir.glob("*.txt"))]
            if self.run_dir and self.run_dir.is_dir()
            else [],
            "running": running,
            "run": str(self.run_dir) if self.run_dir else None,
            "marks": self.marks(),
            "exit": self.exit,
            "polls": polls,
            "tail": tail,
            "movie": self.movie,
            "movie_polls": self.movie_length,
            **tas.listing(self.profile),
        }

    def stop(self) -> dict[str, Any]:
        """End the live run (the page stays idle until the next /new)."""
        with self.lock:
            self._stop_port()
            self.ended_by_page = True
        return {"stopped": True}

    def marks(self) -> dict[str, Any]:
        """The weird moments marked in the current run: how many, and the last one."""
        path = self.run_dir / "marks.jsonl" if self.run_dir else None
        rows = (
            [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            if path and path.is_file()
            else []
        )
        return {"count": len(rows), "last": rows[-1] if rows else None}

    def mark(self, note: str) -> dict[str, Any]:
        """Mark this moment weird: save the current picture and poll index into the run folder and the movie up
        to this poll as `weird-STAMP`; the run keeps going."""
        if not self.recording or not self.run_dir:
            raise DecompError("tas.mark", "nothing is running: start a run first")
        with self.frame_ready:
            frame = self.frame
        if not frame:
            raise DecompError("tas.mark", "no video frame yet")
        polls = tas.read_raw(self.recording) if self.recording.is_file() else []
        poll = len(polls)
        if not poll:
            raise DecompError("tas.mark", "the run has no polls yet")
        marks = self.run_dir / "marks"
        marks.mkdir(parents=True, exist_ok=True)
        picture = marks / f"poll-{poll}.jpg"
        picture.write_bytes(frame)
        part = marks / f"poll-{poll}.raw"
        part.write_text(tas.format_raw(polls))
        name = "weird-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        note = " ".join(note.split())
        try:
            saved = tas.save(self.profile, name, source=part, note=note or "marked weird", replace=False)
        except DecompError:  # a second mark in the same second
            name = f"{name}-{poll}"
            saved = tas.save(self.profile, name, source=part, note=note or "marked weird", replace=True)
        row = {"poll": poll, "note": note, "frame": str(picture), "movie": saved["name"]}
        with (self.run_dir / "marks.jsonl").open("a") as handle:
            handle.write(json.dumps(row) + "\n")
        return row

    def tab(self, name: str) -> list[str]:
        if name == "ai":
            return tas_tabs.ai_lines(self.ai_log) if self.ai_log else ["tas.serve.ai_log is not configured"]
        if name == "port":
            log, run_dir = self.log, self.run_dir
            if not log:  # after a server restart the newest live run is still the one on screen
                logs = sorted((self.cfg["runs"] / "live").glob("*.log"))
                log, run_dir = (logs[-1], logs[-1].with_suffix("")) if logs else (None, None)
            text = log.read_text(errors="replace") if log and log.is_file() else ""
            return tas_tabs.port_lines(log, run_dir, "debug-reason:" in text or bool(self.state()["stopped"]))
        if name == "functions":
            return tas_tabs.function_lines(events.events_path(self.profile))
        raise DecompError("tas.serve", f"no tab {name}")

    def save(self, name: str, note: str) -> dict[str, Any]:
        if not self.recording:
            raise DecompError("tas.serve", "nothing recorded yet: start a run first")
        return tas.save(self.profile, name, source=self.recording, note=note, replace=False)

    def close(self) -> None:
        self._stop_port()
        for helper in self.helpers:
            helper.terminate()


PAGE = (Path(__file__).with_name("tas_page.html")).read_text


def handler(session: Session) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def reply(self, body: bytes, kind: str, status: int = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def json(self, value: Any, status: int = HTTPStatus.OK) -> None:
            self.reply(json.dumps(value).encode(), "application/json", status)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/":
                self.reply(PAGE().encode(), "text/html; charset=utf-8")
            elif path == "/state":
                self.json(session.state())
            elif path == "/tab":
                name = self.path.partition("name=")[2].split("&")[0]
                try:
                    self.json({"lines": session.tab(name)})
                except DecompError as error:
                    self.json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            elif path == "/look":
                query = dict(part.partition("=")[::2] for part in self.path.partition("?")[2].split("&") if part)
                try:
                    self.json(
                        session.look(
                            int(query.get("frames", 12)), int(query.get("span", 180)), float(query.get("seconds", 5))
                        )
                    )
                except (DecompError, ValueError) as error:
                    self.json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            elif path == "/video":
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                try:
                    while True:
                        with session.frame_ready:
                            session.frame_ready.wait(5)
                            frame = session.frame
                        self.wfile.write(
                            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                            + str(len(frame)).encode()
                            + b"\r\n\r\n"
                            + frame
                            + b"\r\n"
                        )
                except (BrokenPipeError, ConnectionResetError):
                    return
            elif path == "/audio":
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                queue: list[bytes] = []
                with session.audio_ready:
                    session.listeners.append(queue)
                try:
                    while True:
                        with session.audio_ready:
                            session.audio_ready.wait(5)
                            chunks, queue[:] = list(queue), []
                        self.wfile.write(b"".join(chunks))
                except (BrokenPipeError, ConnectionResetError):
                    return
                finally:
                    with session.audio_ready:
                        session.listeners.remove(queue)
            else:
                self.json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            try:
                if self.path == "/new":
                    self.json(session.new_run())
                elif self.path == "/stop":
                    self.json(session.stop())
                elif self.path == "/mark":
                    self.json(session.mark(str(body.get("note", ""))))
                elif self.path == "/press":
                    self.json(
                        session.press(
                            [str(b) for b in body.get("buttons", [])],
                            int(body.get("hold", 6)),
                            int(body.get("then", 12)),
                        )
                    )
                elif self.path == "/key":
                    session.key(str(body.get("key", "")), bool(body.get("down")))
                    self.json({})
                elif self.path == "/save":
                    self.json(session.save(str(body.get("name", "")), str(body.get("note", ""))))
                elif self.path == "/play":
                    self.json(session.new_run(str(body.get("name", ""))))
                elif self.path == "/delete":
                    self.json(tas.delete(session.profile, str(body.get("name", ""))))
                else:
                    self.json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except DecompError as error:
                self.json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    return Handler


def serve(profile: Profile) -> dict[str, Any]:
    session = Session(profile)
    host, _, port = session.listen.rpartition(":")
    server = ThreadingHTTPServer((host, int(port)), handler(session))
    session.start_display()
    print(f"tas page: http://{session.listen}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        session.close()
    return {"listen": session.listen}
