"""`decomp tas serve`: one web page to play the port live, record the run, and save, replay or delete movies.

Section `tas.serve`: {"listen": "HOST:PORT", "display": ":N", "fps": N}. The port runs in its own Xvfb display at the
size its `--window` argument names; ffmpeg grabs that display as MJPEG for the page, and the page's key presses go to
the port window through xdotool, so the port reads them through its normal keyboard bindings. Every run the page starts
is a recording from reset (`N64C_TAS_RECORD`), so Save always stores a replayable movie of the current run.
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

from abu_decomp import DecompError, tas
from abu_decomp.config import Profile

KEYS = {"ArrowLeft": "Left", "ArrowRight": "Right", "ArrowUp": "Up", "ArrowDown": "Down", "Enter": "Return"}
_LETTER = re.compile(r"[a-z]")
_SERVE_KEYS = frozenset({"listen", "display", "fps"})


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
        self.fps = int(serve.get("fps", 15))
        self.size = window_size(self.cfg["args"])
        self.lock = threading.Lock()
        self.port: subprocess.Popen | None = None
        self.recording: Path | None = None
        self.log: Path | None = None
        self.exit: int | None = None
        self.replay: dict[str, Any] = {}
        self.frame = b""
        self.frame_ready = threading.Condition()
        self.helpers: list[subprocess.Popen] = []

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
                "6",
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.helpers.append(grab)
        threading.Thread(target=self._read_frames, args=(grab.stdout,), daemon=True).start()

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
        return {**os.environ, **self.cfg["env"], "DISPLAY": self.display, "SDL_VIDEODRIVER": "x11"}

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
            with self.log.open("w") as log:
                self.port = subprocess.Popen(
                    tas._argv(self.cfg, user_dir), env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
                )
            threading.Thread(target=self._focus, args=(self.port.pid,), daemon=True).start()
            return {"recording": str(self.recording)}

    def _focus(self, pid: int) -> None:
        subprocess.run(
            ["xdotool", "search", "--sync", "--pid", str(pid), "windowfocus"],
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
        tail = self.log.read_text(errors="replace").splitlines()[-3:] if self.log and self.log.is_file() else []
        return {
            "running": running,
            "exit": self.exit,
            "polls": polls,
            "tail": tail,
            "replay": self.replay,
            **tas.listing(self.profile),
        }

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
            if self.path == "/":
                self.reply(PAGE().encode(), "text/html; charset=utf-8")
            elif self.path == "/state":
                self.json(session.state())
            elif self.path == "/video":
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
