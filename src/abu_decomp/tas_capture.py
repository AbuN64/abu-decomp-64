"""One display + sound capture per port run, shared by the TAS page's live run and `tas play`.

`Capture` starts a private Xvfb display, a private PulseAudio null sink and ffmpeg grabs of both (MJPEG pictures, an MP3
stream and, for the eyes, raw PCM), so no two runs share a screen or a sound card and several run in parallel. A
`tas play` run publishes its capture into its run folder (`frame.jpg`, `audio.mp3`, `view.json`), which is how the page
shows a replay an agent started: the page picks the newest active run, whichever process owns it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from abu_decomp import DecompError

SINK = "tas"
PLAY_FPS = 10  # a replay's picture is for watching, not for the page's input: keep its CPU low
PUBLISH_SECONDS = 0.1
KIND_LIVE = "live"
KIND_PLAY = "proof replay"


def window_size(args: list[str]) -> str:
    if "--window" not in args or args.index("--window") + 1 >= len(args):
        raise DecompError("tas.serve", "tas.args has no --window WxH, so the page cannot size its display")
    return args[args.index("--window") + 1]


class Capture:
    def __init__(self, display: str | None, size: str, fps: int, pulse: Path, eyes: bool = True) -> None:
        self.display = display or ""  # empty: Xvfb picks a free one (-displayfd)
        self.size, self.fps, self.pulse, self.eyes = size, fps, pulse, eyes
        self.helpers: list[subprocess.Popen] = []
        self.frame = b""
        self.frame_ready = threading.Condition()
        self.listeners: list[list[bytes]] = []
        self.audio_ready = threading.Condition()
        self.pcm = bytearray()
        self.pcm_lock = threading.Lock()

    def start_display(self) -> None:
        width, height = self.size.split("x")
        screen = ["-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp"]
        if self.display:
            self.helpers.append(
                subprocess.Popen(["Xvfb", self.display, *screen], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            )
        else:
            read, write = os.pipe()
            xvfb = subprocess.Popen(
                ["Xvfb", "-displayfd", str(write), *screen],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(write,),
                start_new_session=True,
            )
            self.helpers.append(xvfb)
            os.close(write)
            with os.fdopen(read) as pipe:
                number = pipe.readline().strip()
            if not number.isdigit():
                raise DecompError("tas.capture", "Xvfb did not report a display number")
            self.display = f":{number}"
        grab = subprocess.Popen(
            [
                "ffmpeg", "-loglevel", "error", "-f", "x11grab", "-draw_mouse", "0",
                "-video_size", self.size, "-framerate", str(self.fps), "-i", self.display,
                "-f", "mjpeg", "-q:v", "3", "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )  # fmt: skip
        self.helpers.append(grab)
        threading.Thread(target=self._read_frames, args=(grab.stdout,), daemon=True).start()
        self.start_audio()

    def pulse_env(self) -> dict[str, str]:
        # No D-Bus: a second server on the same user bus would lose the org.PulseAudio1 name to the first and exit.
        return {
            **os.environ,
            "PULSE_RUNTIME_PATH": str(self.pulse),
            "PULSE_STATE_PATH": str(self.pulse),
            "DBUS_SESSION_BUS_ADDRESS": "disabled:",
        }

    def start_audio(self) -> None:
        self.pulse.mkdir(parents=True, exist_ok=True)
        socket = self.pulse / "native"
        env = self.pulse_env()
        self.helpers.append(
            subprocess.Popen(
                [
                    "pulseaudio", "-n", "--daemonize=no", "--exit-idle-time=-1", "--use-pid-file=no",
                    "-L", f"module-native-protocol-unix socket={socket} auth-anonymous=1",
                    "-L", f"module-null-sink sink_name={SINK} rate=48000",
                    "-L", "module-always-sink",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )  # fmt: skip
        subprocess.run(
            ["pactl", "--server", f"unix:{socket}", "info"], env=env, capture_output=True, timeout=10, check=False
        )
        encode = subprocess.Popen(
            [
                "ffmpeg", "-loglevel", "error", "-f", "pulse", "-server", f"unix:{socket}", "-i", f"{SINK}.monitor",
                "-ac", "2", "-b:a", "128k", "-f", "mp3", "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )  # fmt: skip
        self.helpers.append(encode)
        threading.Thread(target=self._read_audio, args=(encode.stdout,), daemon=True).start()
        if self.eyes:
            from abu_decomp import tas_eyes

            raw = subprocess.Popen(
                [
                    "ffmpeg", "-loglevel", "error", "-f", "pulse", "-server", f"unix:{socket}",
                    "-i", f"{SINK}.monitor", "-ac", "2", "-ar", str(tas_eyes.RATE), "-f", "s16le", "pipe:1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )  # fmt: skip
            self.helpers.append(raw)
            threading.Thread(target=self._read_pcm, args=(raw.stdout,), daemon=True).start()

    def _read_pcm(self, stream) -> None:
        from abu_decomp import tas_eyes

        keep = 10 * tas_eyes.RATE * tas_eyes.CHANNELS * 2
        while chunk := stream.read1(16384):
            with self.pcm_lock:
                self.pcm += chunk
                del self.pcm[:-keep]

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

    def port_env(self) -> dict[str, str]:
        """What points a port at this capture's display and sound card."""
        return {
            "DISPLAY": self.display,
            "SDL_VIDEODRIVER": "x11",
            "SDL_AUDIODRIVER": "pulse",
            "PULSE_SERVER": f"unix:{self.pulse / 'native'}",
            "PULSE_SINK": SINK,
        }

    def close(self) -> None:
        for helper in self.helpers:
            if helper.poll() is None:
                helper.terminate()
        for helper in self.helpers:
            try:
                helper.wait(5)
            except subprocess.TimeoutExpired:
                helper.kill()
                helper.wait()


# --- a replay's view of itself, in its run folder, for the page ---


def write_atomic(path: Path, data: bytes) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(data)
    temp.replace(path)


class PlayView:
    """A `tas play` run made visible: its own capture, published into the run folder until `finish`."""

    def __init__(self, cfg: dict[str, Any], run: Path, movie: str, stamp: str) -> None:
        self.run, self.movie = run, movie
        self.pulse = (
            cfg["runs"] / "pulse" / f"p{stamp}-{os.getpid()}"
        )  # short: a unix socket path holds at most 107 bytes
        self.capture = Capture(None, window_size(cfg["args"]), PLAY_FPS, self.pulse, eyes=False)
        self.stop = threading.Event()
        self.started = time.time()

    @classmethod
    def open(cls, cfg: dict[str, Any], run: Path, movie: str, stamp: str) -> PlayView | None:
        """The view for a replay, or None when this host cannot give the run a display (it then runs unseen)."""
        try:
            view = cls(cfg, run, movie, stamp)
        except DecompError:
            return None
        try:
            view.capture.start_display()
        except (OSError, DecompError):
            view.capture.close()
            return None
        view.write("running")
        threading.Thread(target=view._publish_frames, daemon=True).start()
        threading.Thread(target=view._publish_audio, daemon=True).start()
        return view

    def env(self) -> dict[str, str]:
        return self.capture.port_env()

    def write(self, state: str, result: dict[str, Any] | None = None) -> None:
        row = {
            "kind": KIND_PLAY,
            "movie": self.movie,
            "pid": os.getpid(),
            "started": self.started,
            "state": state,
            **(
                {k: result[k] for k in ("outcome", "subject", "polls_reached", "polls") if k in result}
                if result
                else {}
            ),
        }
        write_atomic(self.run / "view.json", json.dumps(row).encode())

    def _publish_frames(self) -> None:
        last = b""
        while not self.stop.is_set():
            with self.capture.frame_ready:
                frame = self.capture.frame
            if frame and frame is not last:
                write_atomic(self.run / "frame.jpg", frame)
                last = frame
            time.sleep(PUBLISH_SECONDS)

    def _publish_audio(self) -> None:
        queue: list[bytes] = []
        with self.capture.audio_ready:
            self.capture.listeners.append(queue)
        with (self.run / "audio.mp3").open("ab") as out:
            while not self.stop.is_set():
                with self.capture.audio_ready:
                    self.capture.audio_ready.wait(0.5)
                    chunks, queue[:] = list(queue), []
                if chunks:
                    out.write(b"".join(chunks))
                    out.flush()

    def finish(self, result: dict[str, Any]) -> None:
        """Keep the last picture, say how the run ended, release the display and sound card."""
        time.sleep(PUBLISH_SECONDS * 2)  # let the publisher write the final frame
        self.stop.set()
        with self.capture.frame_ready:
            frame = self.capture.frame
        if frame:
            write_atomic(self.run / "frame.jpg", frame)
        self.write("done", result)
        self.capture.close()
        shutil.rmtree(self.pulse, ignore_errors=True)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_views(runs: Path, keep: int = 12) -> list[dict[str, Any]]:
    """The newest replays' views, oldest first. A replay whose process is gone without finishing is `died`."""
    rows = []
    folder = runs / "plays"
    for run in sorted(folder.glob("*-*"))[-keep:] if folder.is_dir() else []:
        path = run / "view.json"
        if not path.is_file():
            continue
        try:
            row = json.loads(path.read_text())
        except ValueError:
            continue
        if row.get("state") == "running" and not alive(int(row.get("pid", 0))):
            row = {**row, "state": "done", "outcome": row.get("outcome", "died")}
        polls = run / "polls.raw"
        row |= {
            "kind": row.get("kind", KIND_PLAY),
            "run": str(run),
            "frame": str(run / "frame.jpg"),
            "audio": str(run / "audio.mp3"),
            "poll_count": row["polls_reached"]
            if "polls_reached" in row
            else (polls.read_bytes().count(b"\n") if polls.is_file() else 0),
        }
        rows.append(row)
    return rows
