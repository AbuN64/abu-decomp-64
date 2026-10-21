import json
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest import mock

from abu_decomp import DecompError, tas_drive, tas_eyes, tas_serve
from abu_decomp.config import load_profile


def jpeg(color: str, size: str = "320x240") -> bytes:
    done = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={color}:s={size}", "-frames:v", "1",
         "-f", "mjpeg", "pipe:1"],
        capture_output=True, check=True,
    )  # fmt: skip
    return done.stdout


def tone(seconds: float, amp: int, right: int | None = None) -> bytes:
    import math

    frames = int(seconds * tas_eyes.RATE)
    out = bytearray()
    for i in range(frames):
        v = int(amp * math.sin(i * 0.05))
        out += struct.pack("<hh", v, v if right is None else int(right * math.sin(i * 0.05)))
    return bytes(out)


class EyesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_poll_counter_counts_complete_lines_incrementally(self):
        path = self.root / "r.raw"
        counter = tas_eyes.PollCounter()
        self.assertEqual(counter.update(path), 0)
        path.write_text("0000 0 0\n8000 0 0\n00")
        self.assertEqual(counter.update(path), 2)
        with path.open("a") as handle:
            handle.write("00 0 0\n")
        self.assertEqual(counter.update(path), 3)
        other = self.root / "o.raw"
        other.write_text("0000 0 0\n")
        self.assertEqual(counter.update(other), 1)

    def test_pick_spreads_frames_over_the_span(self):
        samples = [(p, bytes([p])) for p in range(0, 101, 5)]
        polls = [p for p, _ in tas_eyes.pick(samples, 5, 100)]
        self.assertEqual(polls, [0, 25, 50, 75, 100])
        self.assertEqual([p for p, _ in tas_eyes.pick(samples, 3, 20)], [80, 90, 100])
        self.assertEqual(tas_eyes.pick(samples, 1, 20), [samples[-1]])
        self.assertEqual(tas_eyes.pick([], 4, 20), [])

    def test_audio_stats_names_silence_and_channels(self):
        silent = tas_eyes.audio_stats(bytes(4 * 4800))
        self.assertTrue(silent["silent"])
        loud = tas_eyes.audio_stats(tone(0.2, 16000, right=0))
        self.assertFalse(loud["silent"])
        self.assertGreater(loud["peak"]["left"], 0.45)
        self.assertEqual(loud["peak"]["right"], 0.0)
        self.assertEqual(loud["seconds"], 0.2)

    def test_look_writes_small_pngs(self):
        samples = [(p, jpeg("red" if p < 30 else "blue", "1280x960")) for p in range(0, 61, 10)]
        result = tas_eyes.look(self.root / "look", samples, tone(1.0, 8000), frames=6, span=60, seconds=5, poll=60)
        for key in ("frame", "sheet", "spectrogram"):
            data = Path(result[key]).read_bytes()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertLessEqual(struct.unpack(">I", data[16:20])[0], 1280)
        self.assertEqual(result["sheet_info"]["polls"], [0, 10, 20, 40, 50, 60])
        self.assertFalse(result["audio"]["silent"])
        self.assertFalse((self.root / "look" / "work").exists())
        with self.assertRaises(DecompError):
            tas_eyes.look(self.root / "x", [], b"", frames=3, span=9, seconds=1, poll=0)


class PressTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "game.z64").write_bytes(b"rom")
        seat = self.root / "seat.json"
        table = {"movies": "m", "runs": "r", "port": "p", "rom": "game.z64", "args": ["--window", "64x48"]}
        seat.write_text(json.dumps({"schema_version": 1, "name": "t", "tas": table}))
        self.session = tas_serve.Session(load_profile(seat))
        self.recording = self.root / "r.raw"
        self.recording.write_text("")
        self.session.recording = self.recording

        class Alive:
            def poll(self):
                return None

        self.session.port = Alive()

    def run_polls(self, count, gap=0.002):
        def go():
            for _ in range(count):
                with self.recording.open("a") as handle:
                    handle.write("0000 0 0\n")
                time.sleep(gap)

        thread = threading.Thread(target=go)
        thread.start()
        self.addCleanup(thread.join)

    def test_pad_keys(self):
        self.assertEqual(tas_serve.pad_keys("start"), ["Return"])
        self.assertEqual(tas_serve.pad_keys("UP+A"), ["Up", "x"])
        self.assertEqual(tas_serve.pad_keys("x"), ["x"])
        with self.assertRaises(DecompError):
            tas_serve.pad_keys("F5")

    def test_press_is_keyed_to_polls(self):
        calls = []
        with mock.patch.object(tas_serve.subprocess, "run", side_effect=lambda cmd, **kw: calls.append(cmd)):
            self.run_polls(80)
            result = self.session.press(["START", "A"], hold=5, then=3)
        self.assertEqual(
            [c[1:] for c in calls], [["keydown", "Return"], ["keyup", "Return"], ["keydown", "x"], ["keyup", "x"]]
        )
        first, second = result["pressed"]
        self.assertGreaterEqual(first["to"] - first["from"], 5)
        self.assertGreaterEqual(second["from"], first["to"] + 3)

    def test_press_needs_a_live_run_and_releases_on_stall(self):
        self.session.port = None
        with self.assertRaises(DecompError):
            self.session.press(["A"], 1, 0)

        class Alive:
            def poll(self):
                return None

        self.session.port = Alive()
        calls = []
        clock = iter(range(0, 1000, 6))
        with (
            mock.patch.object(tas_serve.subprocess, "run", side_effect=lambda cmd, **kw: calls.append(cmd)),
            mock.patch.object(tas_serve.time, "monotonic", side_effect=lambda: next(clock)),
            mock.patch.object(tas_serve.time, "sleep"),
            self.assertRaises(DecompError),
        ):
            self.session.press(["A"], 3, 0)
        self.assertEqual(calls[-1][1:], ["keyup", "x"])  # the key is never left held


class Page(BaseHTTPRequestHandler):
    seen: ClassVar[list] = []

    def log_message(self, *a):
        pass

    def reply(self, value, status=200):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        Page.seen.append(("GET", self.path))
        self.reply({"error": "no run"}, 400) if self.path.startswith("/look?frames=0") else self.reply(
            {"movies": [1], "recordings": [], "polls": 7, "path": self.path}
        )

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Page.seen.append(("POST", self.path, body))
        self.reply({"ok": True})


class DriveTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "game.z64").write_bytes(b"rom")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        table = {"movies": "m", "runs": "r", "port": "p", "rom": "game.z64", "args": ["--window", "64x48"]}
        table["serve"] = {"listen": f"127.0.0.1:{server.server_address[1]}"}
        (root / "seat.json").write_text(json.dumps({"schema_version": 1, "name": "t", "tas": table}))
        self.profile = load_profile(root / "seat.json")
        Page.seen = []

    def test_verbs_hit_the_page(self):
        tas_drive.new(self.profile, None)
        tas_drive.new(self.profile, "intro")
        tas_drive.press(self.profile, ["A", "UP+B"], 4, 9)
        tas_drive.mark(self.profile, "odd")
        tas_drive.stop(self.profile)
        state = tas_drive.state(self.profile)
        tas_drive.look(self.profile, 8, 90, 3.0)
        self.assertEqual(
            Page.seen[:5],
            [
                ("POST", "/new", {}),
                ("POST", "/play", {"name": "intro"}),
                ("POST", "/press", {"buttons": ["A", "UP+B"], "hold": 4, "then": 9}),
                ("POST", "/mark", {"note": "odd"}),
                ("POST", "/stop", {}),
            ],
        )
        self.assertEqual(state, {"polls": 7, "path": "/state"})
        self.assertEqual(Page.seen[-1], ("GET", "/look?frames=8&span=90&seconds=3.0"))

    def test_page_errors_become_decomp_errors(self):
        with self.assertRaisesRegex(DecompError, "no run"):
            tas_drive.look(self.profile, 0, 1, 1.0)


if __name__ == "__main__":
    unittest.main()
