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
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, events, tas, tas_tabs
from abu_decomp.config import Profile

KEYS = {"ArrowLeft": "Left", "ArrowRight": "Right", "ArrowUp": "Up", "ArrowDown": "Down", "Enter": "Return"}
_LETTER = re.compile(r"[a-z]")
_SERVE_KEYS = frozenset({"listen", "display", "fps", "ai_log"})
_SINK = "tas"


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
    """The one live port run, its recording and the last replay result."""

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
        self.replay: dict[str, Any] = {}
        self.frame = b""
        self.frame_ready = threading.Condition()
        self.helpers: list[subprocess.Popen] = []
        self.pulse = self.cfg["runs"] / "pulse"
        self.listeners: list[list[bytes]] = []
        self.audio_ready = threading.Condition()

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
        env = {**os.environ, "PULSE_RUNTIME_PATH": str(self.pulse), "PULSE_STATE_PATH": str(self.pulse)}
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

    def new_run(self) -> dict[str, Any]:
        with self.lock:
            self._stop_port()
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            live = self.cfg["runs"] / "live"
            user_dir = self.cfg["runs"] / "users" / stamp
            user_dir.mkdir(parents=True, exist_ok=True)
            live.mkdir(parents=True, exist_ok=True)
            self.recording, self.log, self.exit = live / f"{stamp}.raw", live / f"{stamp}.log", None
            env = {**self.env(), "N64C_TAS_RECORD": str(self.recording)}
            for key in ("N64C_TAS_PLAY", "N64C_TAS_EXIT_AFTER"):
                env.pop(key, None)
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

    def state(self) -> dict[str, Any]:
        running = bool(self.port and self.port.poll() is None)
        if self.port and not running:
            self.exit = self.port.returncode
        polls = len(tas.parse_raw(self.recording.read_text())) if self.recording and self.recording.is_file() else 0
        text = self.log.read_text(errors="replace") if self.log and self.log.is_file() else ""
        # A debug-reason line: the gdb bridge holds the stop (process alive, picture frozen), so not running, not a hang.
        held = tas.classify(None if running and "debug-reason:" not in text else self.exit or 0, text) if text else {}
        stopped = held if held.get("outcome") in ("stop", "crash") else {}
        running = running and not stopped
        tail = text.splitlines()[-3:]
        return {
            "stopped": stopped,
            "stacks": [str(p) for p in sorted(self.run_dir.glob("*.txt"))]
            if self.run_dir and self.run_dir.is_dir()
            else [],
            "running": running,
            "exit": self.exit,
            "polls": polls,
            "tail": tail,
            "replay": self.replay,
            **tas.listing(self.profile),
        }

    def tab(self, name: str) -> list[str]:
        if name == "ai":
            return tas_tabs.ai_lines(self.ai_log) if self.ai_log else ["tas.serve.ai_log is not configured"]
        if name == "port":
            return tas_tabs.port_lines(self.log, self.run_dir, bool(self.state()["stopped"]))
        if name == "functions":
            return tas_tabs.function_lines(events.events_path(self.profile))
        raise DecompError("tas.serve", f"no tab {name}")

    def save(self, name: str, note: str) -> dict[str, Any]:
        if not self.recording:
            raise DecompError("tas.serve", "nothing recorded yet: start a run first")
        return tas.save(self.profile, name, source=self.recording, note=note, replace=False)

    def play(self, name: str) -> dict[str, Any]:
        self.replay = {"movie": name, "outcome": "running"}

        def run() -> None:
            try:
                self.replay = tas.play(self.profile, name)
            except DecompError as error:
                self.replay = {"movie": name, "outcome": "error", "subject": str(error)}

        threading.Thread(target=run, daemon=True).start()
        return self.replay

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
                elif self.path == "/key":
                    session.key(str(body.get("key", "")), bool(body.get("down")))
                    self.json({})
                elif self.path == "/save":
                    self.json(session.save(str(body.get("name", "")), str(body.get("note", ""))))
                elif self.path == "/play":
                    self.json(session.play(str(body.get("name", ""))))
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
