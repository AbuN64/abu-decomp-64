"""`tas serve` against the Runner contract (orch plans/GAME-RUNNER.md): live.json from run folders, the baked site
under /data, /wall, the reach relay on /video, frames, logs, the 1x sound as a growing WAV, play / stop. Mocked: no
port runs."""

import gzip
import hashlib
import io
import json
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from PIL import Image

from abu_decomp import DecompError, tas, tas_oracle, tas_serve
from abu_decomp.config import load_profile


def capture_bmp(width=3, height=2, bits=24, top_down=False, seq=42):
    # Distinct rows and columns; a width of three exercises row padding at 24 bpp.
    rgb = bytes((v + y * 31 + x * 73) % 256 for y in range(height) for x in range(width) for v in (3, 9, 17))
    stride = ((width * bits + 31) // 32) * 4
    head = bytearray(70)  # non-default pixel offset catches header-as-pixels bugs
    struct.pack_into("<2sIII", head, 0, b"BM", 70 + height * stride, seq, 70)
    struct.pack_into("<IiiHHI", head, 14, 40, width, -height if top_down else height, 1, bits, 0)
    body = bytearray()
    for y in range(height) if top_down else range(height - 1, -1, -1):
        line = bytearray()
        for x in range(width):
            i = (y * width + x) * 3
            line += rgb[i : i + 3][::-1] + (b"\0" if bits == 32 else b"")
        body += line + bytes(stride - len(line))
    return bytes(head) + body, rgb


def png_rgb(png):
    with Image.open(io.BytesIO(png)) as image:
        return *image.size, image.convert("RGB").tobytes()


def varint(value):
    out = bytearray()
    while value > 127:
        out.append(value & 127 | 128)
        value >>= 7
    return bytes(out + bytes([value]))


def reach_record(kind, poll, first, second=()):
    """The reach wire format (GAME-RUNNER 10a): a full record carries the bitset bytes in `first`; a delta carries the
    fresh rows and then the hot rows, each as a count and delta-coded sorted rows."""
    head = struct.pack("<BI", kind, poll)
    if kind == 1:
        return head + bytes(first)
    out = bytearray(head)
    for rows in (first, second):
        out += varint(len(rows))
        last = 0
        for row in sorted(rows):
            out += varint(row - last)
            last = row
    return bytes(out)


def bitset(rows, size=8):
    bits = bytearray(size)
    for row in rows:
        bits[row >> 3] |= 1 << (row & 7)
    return bytes(bits)


class Stream:
    """A raw HTTP client reading a tas frame stream: (length field, stamp, seq[, index]) headers and bodies."""

    def __init__(self, base, path, header):
        host, _, port = base.removeprefix("http://").partition(":")
        self.sock = socket.create_connection((host, int(port)), timeout=10)
        self.sock.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        self.header = header
        self.buffer = b""
        while b"\r\n\r\n" not in self.buffer:
            self.buffer += self.sock.recv(65536)
        _, _, self.buffer = self.buffer.partition(b"\r\n\r\n")

    def take(self, count):
        while len(self.buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError
            self.buffer += chunk
        out, self.buffer = self.buffer[:count], self.buffer[count:]
        return out

    def record(self):
        fields = self.header.unpack(self.take(self.header.size))
        flagged = bool(fields[0] & 0x80000000)
        return flagged, fields, self.take(fields[0] & 0x7FFFFFFF)

    def close(self):
        self.sock.close()


class PictureTests(unittest.TestCase):
    def test_stride_orientation_offset_and_pixel_size_preserve_each_pixel(self):
        for bits in (24, 32):
            for top_down in (False, True):
                with self.subTest(bits=bits, top_down=top_down):
                    bmp, rgb = capture_bmp(bits=bits, top_down=top_down)
                    png, seq = tas_serve.picture(bmp)
                    self.assertEqual(png_rgb(png), (3, 2, rgb))
                    self.assertEqual(seq, 42)

    def test_live_jpeg_keeps_dimensions_and_input_acknowledgement(self):
        bmp, _ = capture_bmp(320, 240)
        jpeg, seq = tas_serve.picture(bmp, "JPEG")
        with Image.open(io.BytesIO(jpeg)) as image:
            self.assertEqual((image.format, image.size, seq), ("JPEG", (320, 240), 42))
        self.assertLess(len(jpeg), len(bmp) // 2)

    def test_truncated_and_unsupported_captures_fail_closed(self):
        bmp, _ = capture_bmp()
        for data in (b"BM", bmp[:-1], b"P6\n1 1\n255\nabc", bmp[:28] + b"\x10\x00" + bmp[30:]):
            with self.subTest(data=data[:30]), self.assertRaises(ValueError):
                tas_serve.picture(data)


class Base(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "game.z64").write_bytes(b"rom")
        (self.root / "port").write_text("bin")
        movies = self.root / "movies"
        movies.mkdir()
        sha = hashlib.sha1(b"rom").hexdigest()
        (movies / "boot-idle.tas").write_text(f"# tas 1\n# rom-sha1 {sha}\n- *900\n")
        (movies / "model-preview.tas").write_text(f"# tas 1\n# rom-sha1 {sha}\nA *1400\n")
        (self.root / "inbox.md").write_text(
            "# inbox\nL1 /x/final-func_80256C90_de.diff exact  | LANDED 47ee537\n"
            "DEPLOY: decomp 1e51919  | DROPPED apply failed\nL2 /x/final-func_80209CD8_de.diff fuzzy\n"
        )
        (self.root / "states.json").write_text(json.dumps({"versions": {"us-rev1": {"none_fns": ["func_80000000"]}}}))
        self.plays = self.root / "runs" / "plays"
        self.plays.mkdir(parents=True)
        self.seat = self.root / "seat.json"
        self.project = self.root / "decomp"
        (self.project / "src").mkdir(parents=True)
        (self.project / "src" / "t.CPP").write_text(
            "#ifdef NON_MATCHING\nvoid func_80256C90_de(int a)\n{\n}\n#else\n"
            'INCLUDE_ASM("o", func_80256C90_de);\n#endif\n'
        )
        patch = mock.patch("abu_decomp.tas_triage._project", return_value=self.project)
        patch.start()
        self.addCleanup(patch.stop)

    def profile(self, **serve):
        table = {
            "movies": "movies",
            "runs": "runs",
            "port": "port",
            "rom": "game.z64",
            "args": [],
            "serve": {"listen": "127.0.0.1:0", "inbox": "inbox.md", "states": "states.json", **serve},
        }
        self.seat.write_text(json.dumps({"schema_version": 1, "name": "t", "tas": table}))
        return load_profile(self.seat)

    def make_run(self, run_id, **record):
        run = self.plays / run_id
        run.mkdir()
        (run / "run.json").write_text(json.dumps({"id": run_id, **record}))
        return run


class ServeTests(Base):
    def test_unknown_serve_keys_are_refused(self):
        with self.assertRaises(DecompError):
            tas_serve.Server(self.profile(display=":97"))

    def test_build_format_is_the_port_json_format_of_the_port_source(self):
        source = self.root / "ports"
        source.mkdir()
        self.assertIsNone(tas_serve.Server(self.profile()).state()["build"]["format"])  # no tas.serve.port_source
        server = tas_serve.Server(self.profile(port_source="ports"))
        self.assertIsNone(server.state()["build"]["format"])  # no port.json: a pre-format build
        (source / "port.json").write_text(json.dumps({"name": "x", "format": 1}))
        self.assertEqual(server.state()["build"]["format"], 1)
        (source / "port.json").write_text("{not json")
        self.assertIsNone(server.state()["build"]["format"])
        (source / "port.json").write_text(json.dumps({"format": "1"}))
        self.assertIsNone(server.state()["build"]["format"])

    def test_state_follows_the_contract(self):
        server = tas_serve.Server(self.profile())
        live = self.make_run(
            "20261022T170000Z-boot-idle", movie="boot-idle", status="running", speed=0, polls=900, started=10.0,
            pid=os.getpid(),
        )  # fmt: skip
        (live / "hash.txt").write_text("1 aa\n2 bb\n3 c")  # the last line is still being written
        self.make_run(
            "20261022T160000Z-model-preview", movie="model-preview", status="done", speed=0, polls=1400, started=5.0,
            outcome="crash", subject="host-fault-10", polls_reached=611, same_twice=True, fn="func_80256C90_de",
            kind="host fault", ended=6.0,
        )  # fmt: skip
        self.make_run("20261022T150000Z-boot-idle", movie="boot-idle", status="running", pid=2**22 + 7, started=1.0)
        state = server.state()
        self.assertEqual(set(state), {"now", "build", "runs"})
        self.assertTrue(all("port" not in r for r in state["runs"]))  # live.json rows carry no port path
        self.assertEqual(state["build"]["port"], str(self.root / "port"))
        self.assertGreater(state["build"]["built"], 0)
        runs = {r["id"]: r for r in state["runs"]}
        self.assertEqual([r["started"] for r in state["runs"]], [1.0, 5.0, 10.0])
        now = runs["20261022T170000Z-boot-idle"]
        self.assertEqual((now["status"], now["poll"], now["polls"], now["outcome"]), ("running", 2, 900, None))
        self.assertEqual((now["pinned"], now["took_at"], now["from"], now["ended"]), (False, None, None, None))
        self.assertFalse(now["stopped"])
        self.assertEqual(runs["20261022T150000Z-boot-idle"]["status"], "done")  # its process is gone
        done = runs["20261022T160000Z-model-preview"]
        self.assertEqual((done["outcome"], done["poll"], done["subject"]), ("crash", 611, "host-fault-10"))
        movies = {m["name"]: m for m in server.movies()}
        self.assertEqual(movies["boot-idle"]["polls"], 900)
        self.assertIsNone(movies["boot-idle"]["fn"])
        preview = movies["model-preview"]
        self.assertEqual((preview["outcome"], preview["reached"], preview["same_twice"]), ("crash", 611, True))
        self.assertEqual(preview["fn"], {"name": "func_80256C90_de", "state": "fuzzy"})
        self.assertEqual(preview["train"], {"line": "L1 /x/final-func_80256C90_de.diff exact", "mark": "LANDED"})
        self.assertEqual(preview["last_run"], 6.0)
        self.assertEqual((preview["root"], preview["from"], preview["at"]), ("model-preview", None, None))

    def test_takes_fold_under_their_root_movie(self):
        sha = hashlib.sha1(b"rom").hexdigest()
        movies = self.root / "movies"
        (movies / "boot-idle-take-20261023-010101.tas").write_text(
            f"# tas 1\n# rom-sha1 {sha}\n# from boot-idle\n# at 400\n- *500\n"
        )
        (movies / "boot-idle-take-20261023-010101-take-20261023-020202.tas").write_text(
            f"# tas 1\n# rom-sha1 {sha}\n- *9\n"
        )
        rows = {m["name"]: m for m in tas_serve.Server(self.profile()).movies()}
        take = rows["boot-idle-take-20261023-010101"]
        self.assertEqual((take["root"], take["from"], take["at"], take["polls"]), ("boot-idle", "boot-idle", 400, 500))
        self.assertEqual(rows["boot-idle-take-20261023-010101-take-20261023-020202"]["root"], "boot-idle")

    def test_history_pages_finished_runs_newest_first_and_pins(self):
        server = tas_serve.Server(self.profile())
        for n in range(5):
            self.make_run(f"r{n}", movie="boot-idle", status="done", started=float(n), outcome="ok", ended=n + 0.5)
        self.make_run("live", movie="boot-idle", status="running", started=9.0, pid=os.getpid())
        page = server.history(None, 2)
        self.assertEqual(([r["id"] for r in page["runs"]], page["more"]), (["r4", "r3"], True))
        page = server.history(3.0, 60)
        self.assertEqual(([r["id"] for r in page["runs"]], page["more"]), (["r2", "r1", "r0"], False))
        self.assertEqual(server.keep("r1", True), {"id": "r1", "pinned": True})
        self.assertTrue(server.history(2.0, 1)["runs"][0]["pinned"])
        self.assertEqual(server.keep("r1", False)["pinned"], False)
        with self.assertRaises(DecompError):
            server.keep("nope", True)

    def test_fn_state_reads_the_source_and_the_progress_states(self):
        server = tas_serve.Server(self.profile())
        (self.project / "src" / "u.CPP").write_text("void func_80209CD8_de(int a)\n{\n}\n")
        self.assertEqual(server.fn_state("func_80209CD8_de"), "exact")
        self.assertEqual(server.fn_state("func_80256C90_de"), "fuzzy")
        self.assertEqual(server.fn_state("func_80000000"), "none")
        self.assertEqual(server.fn_state("func_8041CAD8_de"), "none")

    def test_realtime_policy_waits_for_takeover_acknowledgement(self):
        run = self.make_run("prefix", speed=0, input="human", at=30)
        row = tas.read_record(run)
        (run / tas.TAKEN).write_text("retry")
        self.assertFalse(tas_serve.realtime(run, row))
        (run / "output.txt").write_text("n64c-tas: live input took over at poll 1\n")
        self.assertTrue(tas_serve.realtime(run, row))
        self.assertTrue(tas_serve.realtime(run, {"speed": 1}))
        (run / tas.TAKEN).unlink()
        self.assertFalse(tas_serve.realtime(run, row))

    def test_wav_header_is_a_stereo_s16_stream(self):
        head = tas_serve.wav_header(22050)
        self.assertEqual((head[:4], head[8:16], head[36:40], len(head)), (b"RIFF", b"WAVEfmt ", b"data", 44))
        channels, rate, _, align, bits = struct.unpack("<HIIHH", head[22:36])
        self.assertEqual((channels, rate, align, bits), (2, 22050, 4, 16))


class HttpTests(Base):
    def setUp(self):
        super().setUp()
        self.server = tas_serve.Server(self.profile())
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), tas_serve.handler(self.server))
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as reply:
            return reply.status, reply.headers.get("Content-Type"), reply.read()

    def live(self):
        """live.json as the writer would leave it now."""
        self.server.live.write()
        return json.loads((self.server.site / "live.json").read_bytes())

    def post(self, path, body):
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as reply:
                return reply.status, json.load(reply)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def status(self, path):
        try:
            return self.get(path)[0]
        except urllib.error.HTTPError as error:
            return error.code

    def test_slot_pool_endpoint_answers_on_a_real_server(self):
        status, _, body = self.get("/api/pool")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue({"capacity", "used", "queue", "holders", "landings"} <= set(data))

    def test_page_state_frame_and_log(self):
        status, kind, body = self.get("/")
        self.assertEqual((status, kind), (200, "text/html; charset=utf-8"))
        self.assertIn(b"<html", body.lower())
        self.assertEqual(self.live()["runs"], [])
        run = self.make_run("r1", movie="boot-idle", status="done", speed=0, started=1.0, outcome="ok")
        self.assertEqual(self.status("/frame/r1"), 404)  # no picture yet
        bmp, rgb = capture_bmp()
        (run / "view.bmp").write_bytes(bmp)
        _, kind, body = self.get("/frame/r1")
        self.assertEqual(kind, "image/png")
        self.assertEqual(png_rgb(body), (3, 2, rgb))
        (run / "output.txt").write_bytes(b"line one\nline two\n")
        self.assertEqual(self.get("/log/r1?from=0")[2], b"line one\nline two\n")
        self.assertEqual(self.get("/log/r1?from=9")[2], b"line two\n")
        self.assertEqual(self.status("/log/nope"), 404)
        self.assertEqual(self.status("/frame/..%2Fx"), 404)
        self.assertEqual(self.status("/audio/r1"), 204)  # turbo is explicitly muted even if PCM exists

    def fetch(self, path, **headers):
        request = urllib.request.Request(self.base + path, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as reply:
                return reply.status, reply.headers, reply.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def test_text_replies_gzip_and_answer_a_matching_etag_with_304(self):
        for n in range(1, 6):
            self.make_run(f"r{n}", movie="boot-idle", status="done", speed=0, started=float(n), outcome="ok")
        status, head, plain = self.fetch("/api/runs")
        self.assertEqual((status, head.get("Content-Encoding")), (200, None))
        status, head, packed = self.fetch("/api/runs", **{"Accept-Encoding": "gzip"})
        self.assertEqual((status, head["Content-Encoding"], head["Vary"]), (200, "gzip", "Accept-Encoding"))
        self.assertLess(len(packed), len(plain))
        self.assertEqual(json.loads(gzip.decompress(packed))["runs"], json.loads(plain)["runs"])
        tag = head["ETag"]
        status, head, body = self.fetch("/api/runs", **{"If-None-Match": tag, "Accept-Encoding": "gzip"})
        self.assertEqual((status, body), (304, b""))
        self.assertEqual(self.fetch("/api/runs", **{"If-None-Match": f'W/{tag}, "x"'})[0], 304)
        self.make_run("r9", movie="model-preview", status="done", speed=0, started=9.0, outcome="ok")
        self.assertEqual(self.fetch("/api/runs", **{"If-None-Match": tag})[0], 200)  # a changed state is sent again
        status, head, _ = self.fetch("/")
        self.assertEqual(self.fetch("/", **{"If-None-Match": head["ETag"]})[0], 304)
        self.assertEqual(self.fetch("/frame/r1")[0], 404)

    def test_images_are_never_compressed_or_tagged(self):
        run = self.make_run("r1", movie="boot-idle", status="done", speed=0, started=1.0, outcome="ok")
        bmp, _ = capture_bmp()
        (run / "view.bmp").write_bytes(bmp)
        status, head, body = self.fetch("/frame/r1", **{"Accept-Encoding": "gzip"})
        self.assertEqual(
            (status, head.get("Content-Encoding"), head.get("ETag"), len(body)),
            (200, None, None, len(tas_serve.picture(bmp)[0])),
        )

    def test_run_records_are_reread_only_when_they_change(self):
        run = self.make_run("r1", movie="boot-idle", status="done", speed=0, started=1.0, outcome="ok")
        self.assertEqual(tas.read_record(run)["outcome"], "ok")
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("re-read")):
            self.assertEqual(tas.read_record(run)["outcome"], "ok")
        record = run / tas.RECORD
        record.write_text(json.dumps({**json.loads(record.read_text()), "outcome": "fault"}) + "\n")
        self.assertEqual(tas.read_record(run)["outcome"], "fault")

    def test_audio_streams_the_growing_pcm_of_a_1x_run_as_wav(self):
        run = self.make_run("r2", movie="boot-idle", status="running", speed=1, started=1.0, pid=os.getpid())
        (run / "audio.pcm").write_bytes(b"\x01\x00\x02\x00" * 10)
        (run / "audio.pcm.rate").write_text("32000\n")

        def finish():
            time.sleep(0.3)
            with (run / "audio.pcm").open("ab") as handle:
                handle.write(b"\x03\x00\x04\x00" * 5)
            time.sleep(0.3)
            (run / "run.json").write_text(json.dumps({"id": "r2", "status": "done", "speed": 1}))

        threading.Thread(target=finish, daemon=True).start()
        status, kind, body = self.get("/audio/r2")
        self.assertEqual((status, kind), (200, "audio/wav"))
        self.assertEqual(struct.unpack("<I", body[24:28])[0], 32000)
        self.assertEqual(body[44:], b"\x01\x00\x02\x00" * 10 + b"\x03\x00\x04\x00" * 5)

    def test_audio_side_oracle_plays_the_original_at_wall_pace_from_the_run_start(self):
        late = self.make_run("r3", movie="boot-idle", status="done", speed=1, started=time.time() - 60)
        (late / "audio-oracle.pcm").write_bytes(b"\x05\x00\x06\x00" * 10)
        (late / "audio-oracle.pcm.rate").write_text("22050\n")
        (late / "audio.pcm").write_bytes(b"\x01\x00\x02\x00" * 10)
        (late / "audio.pcm.rate").write_text("32000\n")
        status, kind, body = self.get("/audio/r3?side=oracle")
        self.assertEqual((status, kind, struct.unpack("<I", body[24:28])[0]), (200, "audio/wav", 22050))
        self.assertEqual(body[44:], b"\x05\x00\x06\x00" * 10)
        self.assertEqual(self.get("/audio/r3")[2][44:], b"\x01\x00\x02\x00" * 10)  # the port's, unchanged
        early = self.make_run("r4", movie="boot-idle", status="done", speed=1, started=time.time() + 1000)
        (early / "audio-oracle.pcm").write_bytes(b"\x05\x00\x06\x00" * 10)
        (early / "audio-oracle.pcm.rate").write_text("22050\n")
        self.assertEqual(self.get("/audio/r4?side=oracle")[2][44:], b"")  # not yet due in the run's own time

    def test_play_and_stop(self):
        started = []

        def fake_play(profile, movie, *, speed=0, run_id=None, port=None, label=None, **kwargs):
            started.append((movie, speed, run_id, port, label))
            if label:
                hold.wait(10)  # the placeholder stays in `runs` until the run would write its record
            return {}

        hold = threading.Event()

        with mock.patch.object(tas, "play", fake_play):
            status, reply = self.post("/api/play", {"movie": "boot-idle", "speed": 1})
            self.assertEqual(status, 200)
            self.assertTrue(reply["id"].endswith("-boot-idle"))
            for _ in range(100):
                if started:
                    break
                time.sleep(0.01)
            self.assertEqual(started, [("boot-idle", 1, reply["id"], None, None)])
            exe = self.root / "lane-port"
            exe.write_text("bin")
            exe.chmod(0o755)
            status, reply = self.post("/api/play", {"movie": "boot-idle", "speed": 1, "port": str(exe), "label": "L-1"})
            self.assertEqual(status, 200)
            queued = {r["id"]: r for r in self.live()["runs"]}[reply["id"]]
            self.assertEqual((queued["label"], "port" in queued), ("L-1", False))
            for _ in range(100):
                if len(started) == 2:
                    break
                time.sleep(0.01)
            self.assertEqual(started[1][3:], (str(exe), "L-1"))
            hold.set()
            self.assertEqual(self.post("/api/play", {"movie": "boot-idle", "port": str(self.root / "nope")})[0], 400)
            self.assertEqual(self.post("/api/play", {"movie": "boot-idle", "label": "bad label!"})[0], 400)
        self.assertEqual(self.post("/api/play", {"movie": "nope", "speed": 0})[0], 400)
        run = self.make_run("r3", movie="boot-idle", status="running", speed=0, started=1.0, pid=os.getpid(), pgid=4242)
        with mock.patch.object(tas_serve.os, "killpg") as kill, mock.patch.object(tas_serve, "STOP_GRACE", 0.05):
            self.assertEqual(self.post("/api/stop", {"id": "r3"}), (200, {"id": "r3", "stopped": True}))
            time.sleep(0.3)
        self.assertEqual(
            kill.call_args_list, [mock.call(4242, tas_serve.signal.SIGTERM), mock.call(4242, tas_serve.signal.SIGKILL)]
        )
        self.assertTrue((run / "stopped").is_file())
        self.assertEqual(self.post("/api/stop", {"id": "missing"})[0], 400)

    def test_new_human_run_take_and_input_reach_the_run_socket(self):
        import socket

        started, paks, unlocks = [], [], []

        def fake_play(profile, movie, *, speed=0, run_id=None, port=None, label=None, pak=None, cpak=None, **kwargs):
            started.append((movie, speed, run_id, label))
            paks.append((pak, cpak))
            unlocks.append(kwargs.get("fps_unlock"))
            hold.wait(10)
            return {}

        hold = threading.Event()
        with mock.patch.object(tas, "play", fake_play):
            self.assertEqual(self.post("/api/play", {"label": "owner", "pak": "maybe"})[0], 400)
            self.assertEqual(self.post("/api/play", {"label": "owner", "cpak": "maybe"})[0], 400)
            status, reply = self.post("/api/play", {"label": "owner", "pak": "off", "cpak": "off", "fps_unlock": True})
            self.assertEqual(status, 200)
            self.assertTrue(reply["movie"].startswith("play-"))
            for _ in range(100):
                if started:
                    break
                time.sleep(0.01)
            self.assertEqual(started, [(None, 1, reply["id"], "owner")])
            row = {r["id"]: r for r in self.live()["runs"]}[reply["id"]]
            self.assertEqual((row["input"], row["label"], row["speed"], row["pak"]), ("human", "owner", 1, "off"))
            self.assertEqual(row["cpak"], "none")  # the page shows an empty slot for a run without the card
            self.assertEqual(paks, [("off", "off")])
            self.assertEqual((unlocks, row["fps_unlock"], row["shown"]), ([True], True, None))
            self.assertEqual(self.post("/api/play", {})[0], 400)  # one live-input run at a time
            hold.set()
        for _ in range(100):  # the placeholder goes once its run ends
            if not self.server.human_running():
                break
            time.sleep(0.01)
        run = self.make_run("r5", movie="boot-idle", status="running", speed=0, started=1.0, pid=os.getpid())
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(sock.close)
        sock.bind("\0" + tas.input_address(run)[1:])
        self.assertEqual(
            self.post("/api/input", {"id": "r5", "seq": 1, "buttons": 1, "x": 0, "y": 0})[0], 400
        )  # not taken
        self.assertEqual(self.post("/api/take", {"id": "r5"}), (200, {"id": "r5", "taken": True}))
        self.assertTrue((run / "taken").is_file())
        self.assertEqual(sock.recv(64), b"take")
        self.assertEqual(
            self.post("/api/input", {"id": "r5", "seq": 7, "buttons": 0x9000, "x": -80, "y": 5}), (200, {"ok": True})
        )
        self.assertEqual(sock.recv(64), b"7 9000 -80 5")
        self.assertEqual(self.post("/api/input", {"id": "r5", "seq": 8, "buttons": 0x10000})[0], 400)
        state = {r["id"]: r for r in self.live()["runs"]}
        self.assertEqual(state["r5"]["input"], "human")
        self.assertEqual(self.post("/api/play", {})[0], 400)  # r5 is driven by live input now
        self.assertEqual(self.post("/api/take", {"id": "nope"})[0], 400)

    def test_input_names_the_pad_of_a_multi_controller_run(self):
        import socket

        run = self.make_run("r6", movie="lobby", status="running", speed=0, started=1.0, pid=os.getpid(), pads=2)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(sock.close)
        sock.bind("\0" + tas.input_address(run)[1:])
        self.assertEqual(self.post("/api/take", {"id": "r6"}), (200, {"id": "r6", "taken": True}))
        self.assertEqual(sock.recv(64), b"take")
        self.assertEqual(
            self.post("/api/input", {"id": "r6", "seq": 1, "buttons": 16, "x": 0, "y": 0, "pad": 1})[0], 200
        )
        self.assertEqual(sock.recv(64), b"1 10 0 0 1")
        self.assertEqual(self.post("/api/input", {"id": "r6", "seq": 2, "buttons": 16, "x": 0, "y": 0})[0], 200)
        self.assertEqual(sock.recv(64), b"2 10 0 0")  # pad 0 keeps the old datagram
        self.assertEqual(
            self.post("/api/input", {"id": "r6", "seq": 3, "buttons": 16, "x": 0, "y": 0, "pad": 2})[0], 400
        )
        state = {r["id"]: r for r in self.live()["runs"]}
        self.assertEqual(state["r6"]["pads"], 2)

    def test_release_returns_a_taken_run_and_frees_live_input(self):
        run = self.make_run("r7", movie="boot-idle", status="running", speed=0, started=1.0, pid=os.getpid())
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(sock.close)
        sock.bind("\0" + tas.input_address(run)[1:])
        self.assertEqual(self.post("/api/input", {"id": "r7", "release": True})[0], 400)  # not taken
        self.post("/api/take", {"id": "r7"})
        self.assertEqual(sock.recv(64), b"take")
        self.assertEqual(self.post("/api/input", {"id": "r7", "release": True}), (200, {"id": "r7", "released": True}))
        self.assertEqual(sock.recv(64), b"release")
        self.assertTrue((run / "taken").exists())  # still a take
        self.assertIsNone(self.server.human_running())
        self.assertEqual(self.post("/api/input", {"id": "r7", "seq": 1, "buttons": 0, "x": 0, "y": 0})[0], 400)
        self.assertEqual({r["id"]: r for r in self.live()["runs"]}["r7"]["input"], "movie")
        self.post("/api/take", {"id": "r7"})  # a take after a release re-takes the run
        self.assertEqual(sock.recv(64), b"take")
        self.assertEqual(self.server.human_running(), "r7")

    def test_retry_from_a_poll_and_pin(self):
        source = self.make_run("src", movie="boot-idle", status="done", started=1.0, outcome="crash", polls_reached=3)
        (source / "polls.raw").write_text("8000 0 0\n0000 0 0\n1000 0 0\n")
        started = []
        hold = threading.Event()

        def fake_play(profile, movie, *, run_id=None, source=None, take_at=None, **kwargs):
            started.append((movie, run_id, source, take_at))
            hold.wait(10)
            return {}

        with mock.patch.object(tas, "play", fake_play):
            self.assertEqual(self.post("/api/play", {"from": "src", "at": 9})[0], 400)  # beyond its recording
            self.assertEqual(self.post("/api/play", {"from": "nope", "at": 1})[0], 400)
            status, reply = self.post("/api/play", {"from": "src", "at": 2, "label": "owner"})
            self.assertEqual((status, reply["movie"]), (200, "boot-idle"))
            row = {r["id"]: r for r in self.live()["runs"]}[reply["id"]]
            self.assertEqual(
                (row["input"], row["from"], row["at"], row["took_at"], row["polls"]), ("human", "src", 2, 2, 2)
            )
            self.assertFalse(row["stopped"])
            for _ in range(100):
                if started:
                    break
                time.sleep(0.01)
            self.assertEqual(started, [(None, reply["id"], "src", 2)])
            self.assertEqual(self.post("/api/play", {"from": "src", "at": 1})[0], 400)  # one live-input run at a time
            hold.set()
        self.assertEqual(self.post("/api/pin", {"id": "src", "pinned": True}), (200, {"id": "src", "pinned": True}))
        self.assertTrue((source / "pinned").is_file())
        page = json.loads(self.get("/api/runs?limit=5")[2])
        self.assertEqual(([r["id"] for r in page["runs"]], page["runs"][0]["pinned"]), (["src"], True))

    def test_coverage_table_and_the_static_gcovr_report(self):
        self.assertEqual(json.loads(self.get("/api/coverage")[2]), {})  # no table yet
        runs = self.root / "runs"
        table = {
            "generated": 1,
            "summary": {"version": "us-rev1"},
            "gcovr": {"line_percent": 1.5},
            "functions": [
                {"name": "a", "in_version": True, "scope": "game", "state": "oracle-only", "size": 4},
                {"name": "b", "in_version": True, "scope": "game", "state": "neither", "size": 9},
                {"name": "c", "in_version": True, "scope": "sdk", "state": "neither", "size": 99},
            ],
        }
        (runs / "coverage-table.json").write_text(json.dumps(table))
        (runs / "coverage").mkdir()
        (runs / "coverage" / "index.html").write_text("<html>report</html>")
        (runs / "secret.txt").write_text("no")
        view = json.loads(self.get("/api/coverage")[2])
        self.assertEqual([r["name"] for r in view["oracle_only"]], ["a"])
        self.assertEqual([r["name"] for r in view["neither"]], ["b"])  # sdk rows are not the owner's to record
        self.assertEqual(view["gcovr"], {"line_percent": 1.5})
        with urllib.request.urlopen(self.base + "/coverage/", timeout=10) as reply:
            self.assertEqual(
                (reply.headers["Content-Type"], reply.read()), ("text/html; charset=utf-8", b"<html>report</html>")
            )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/coverage/../secret.txt", timeout=10)
        self.assertEqual(caught.exception.code, 404)

    def test_every_frame_source_uses_the_same_decoder(self):
        run = self.make_run("r6", movie="boot-idle", status="running", speed=1, started=1.0, pid=os.getpid())
        bmp, rgb = capture_bmp(320, 240)
        (run / "view.bmp").write_bytes(bmp)
        (run / "oracle.bmp").write_bytes(bmp)
        shot = run / "shots" / "port" / "poll-000025.ppm"
        shot.parent.mkdir(parents=True)
        shot.write_bytes(tas_oracle.PPM_HEAD + rgb)
        for url in ("/frame/r6", "/oracle/r6", "/shot/r6?side=port&poll=25"):
            with self.subTest(url=url):
                self.assertEqual(png_rgb(self.get(url)[2]), (320, 240, rgb))
        # A completed run is still served through exactly the same frame path.
        (run / tas.RECORD).write_text(json.dumps({"status": "done", "speed": 1}))
        with urllib.request.urlopen(self.base + "/frame/r6", timeout=10) as reply:
            self.assertEqual(reply.headers["X-Input-Seq"], "42")
            self.assertEqual(png_rgb(reply.read()), (320, 240, rgb))
        with mock.patch.object(tas_serve, "picture", side_effect=AssertionError("decoded twice")):
            self.server.capture(run / "view.bmp")

    def test_turbo_audio_is_muted_with_pcm_present(self):
        for speed in (0, 2):
            run = self.make_run(f"s{speed}", status="running", speed=speed, pid=os.getpid())
            (run / tas.AUDIO).write_bytes(b"\0" * 8000)
            (run / (tas.AUDIO + ".rate")).write_text("32000")
            status, head, body = self.fetch(f"/audio/s{speed}")
            self.assertEqual((status, head["X-Audio-Muted"], body), (204, "not-realtime", b""))

    def test_stream_samples_latest_and_preserves_the_capture_tag(self):
        run = self.make_run("v", status="running", speed=1, pid=os.getpid())
        h = object.__new__(tas_serve.handler(self.server))
        h.send_response = h.send_header = h.end_headers = mock.Mock()
        h.connection, h.wfile = mock.Mock(), io.BytesIO()
        png = tas_serve.picture(capture_bmp()[0])[0]
        with (
            mock.patch.object(self.server, "capture", side_effect=[(1, png, 7), (1, png, 7), (3, png, 9)]),
            mock.patch.object(tas, "running", side_effect=[True, True, False]),
            mock.patch.object(tas_serve.time, "monotonic", side_effect=[0, 0.001, 0.034]),
            mock.patch.object(tas_serve.time, "sleep") as wait,
        ):
            h.video(run)
        data = h.wfile.getvalue()
        n = len(png)
        self.assertEqual(tas_serve.VIDEO_HEADER.unpack(data[:16]), (n, 1, 7))
        self.assertEqual(tas_serve.VIDEO_HEADER.unpack(data[n + 16 : n + 32]), (n, 3, 9))
        self.assertEqual(len(data), 2 * (n + 16))
        self.assertEqual(wait.call_count, 2)
        self.assertEqual(wait.call_args_list[1].args[0], 0.002)
        self.assertTrue(all(0 <= call.args[0] <= 1 / 30 for call in wait.call_args_list))

    def test_stream_disconnect_releases_the_viewer(self):
        run = self.make_run("v", status="running", speed=1, pid=os.getpid())
        h = object.__new__(tas_serve.handler(self.server))
        h.send_response = h.send_header = h.end_headers = mock.Mock()
        h.connection, h.wfile = mock.Mock(), mock.Mock()
        h.wfile.write.side_effect = BrokenPipeError
        with mock.patch.object(self.server, "capture", return_value=(1, b"png", 7)) as grab:
            h.video(run)
        grab.assert_called_once()


class LiveTests(Base):
    """live.json: written from an inotify watch on the run folders, never per request."""

    def start(self):
        self.server = tas_serve.Server(self.profile())
        self.server.live.start()
        self.path = self.server.site / "live.json"
        self.wait(lambda: self.path.is_file())
        return self.server

    def wait(self, check, seconds=3.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            result = check()
            if result:
                return result
            time.sleep(0.005)
        self.fail("timed out")

    def runs(self):
        try:
            return {r["id"]: r for r in json.loads(self.path.read_bytes())["runs"]}
        except (OSError, ValueError):
            return {}

    def test_run_changes_reach_live_json_within_half_a_second(self):
        server = self.start()
        time.sleep(0.7)  # the writer is idle: no rate limit pending
        began = time.monotonic()
        run = self.make_run("a", movie="boot-idle", status="running", speed=0, started=1.0, pid=os.getpid())
        self.wait(lambda: "a" in self.runs())
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertEqual(self.runs()["a"]["status"], "running")
        self.assertNotIn("port", self.runs()["a"])
        time.sleep(0.7)
        began = time.monotonic()
        record = run / tas.RECORD
        tmp = run / "run.json.tmp"
        tmp.write_text(json.dumps({"id": "a", "movie": "boot-idle", "status": "done", "outcome": "ok", "started": 1.0}))
        tmp.replace(record)  # how the port's run rewrites its record
        self.wait(lambda: self.runs()["a"]["status"] == "done")
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertEqual(self.runs()["a"]["outcome"], "ok")
        began = time.monotonic()
        server.keep("a", True)  # the pin route: it nudges the writer, the finished run is no longer watched
        self.wait(lambda: self.runs()["a"]["pinned"])
        self.assertLess(time.monotonic() - began, 0.5)

    def test_poll_ticks_follow_a_running_run_and_a_queued_run_shows(self):
        self.start()
        run = self.make_run("b", movie="boot-idle", status="running", speed=0, started=1.0, pid=os.getpid())
        self.wait(lambda: "b" in self.runs())
        (run / "hash.txt").write_text("1 aa\n2 bb\n3 cc\n")
        self.wait(lambda: self.runs()["b"]["poll"] == 3)
        (run / "hash.txt").write_text("1 aa\n2 bb\n3 cc\n4 dd\n5 ee\n")
        self.wait(lambda: self.runs()["b"]["poll"] == 5)
        self.server._placeholder("q1", "boot-idle", 0, self.server.cfg["port"], None)
        self.wait(lambda: self.runs().get("q1", {}).get("status") == "queued")
        self.server._forget("q1")
        self.wait(lambda: "q1" not in self.runs())

    def test_live_json_is_small_and_holds_running_queued_and_the_newest_finished(self):
        for n in range(60):
            self.make_run(
                f"d{n:02d}", movie="boot-idle", status="done", speed=0, started=float(n), outcome="ok", ended=n + 1.0
            )
        for n in range(3):
            self.make_run(f"r{n}", movie="boot-idle", status="running", speed=0, started=100.0 + n, pid=os.getpid())
        self.start()
        runs = json.loads(self.path.read_bytes())["runs"]
        self.assertEqual(len(runs), tas_serve.RECENT_SHOWN + 3)
        self.assertEqual({r["id"] for r in runs if r["status"] == "done"}, {f"d{n:02d}" for n in range(36, 60)})
        self.assertLess(len(self.path.read_bytes()), 15 * 1024)

    def test_an_unchanged_state_is_not_rewritten(self):
        self.start()
        stamp = self.path.stat().st_mtime_ns
        self.assertFalse(self.server.live.write())
        self.assertEqual(self.path.stat().st_mtime_ns, stamp)


class WallTests(Base):
    def test_one_connection_carries_a_thumb_of_every_running_run(self):
        server = tas_serve.Server(self.profile())
        http = ThreadingHTTPServer(("127.0.0.1", 0), tas_serve.handler(server))
        http.daemon_threads = True
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        bmp, _ = capture_bmp(320, 240)
        for n in range(12):
            run = self.make_run(
                f"w{n:02d}", movie="boot-idle", status="running", speed=0, started=float(n), pid=os.getpid()
            )
            (run / "view.bmp").write_bytes(bmp)
        self.make_run("old", movie="boot-idle", status="done", started=0.5, outcome="ok")
        wall = Stream(f"http://127.0.0.1:{http.server_address[1]}", "/wall", tas_serve.VIDEO_HEADER)
        self.addCleanup(wall.close)
        names, shown = {}, set()
        while len(shown) < 12:
            flagged, (_length, _stamp, index), body = wall.record()
            if flagged:
                self.assertTrue(body.startswith(b"{"))  # no run has reach records here: every flagged one is the table
                names[index] = json.loads(body)["id"]
            else:
                with Image.open(io.BytesIO(body)) as image:
                    self.assertEqual((image.format, image.size), ("JPEG", (192, 144)))
                shown.add(index)
        self.assertEqual(sorted(names.values()), [f"w{n:02d}" for n in range(12)])
        self.assertEqual(shown, set(names))  # every index was named before its first picture


class ReachTests(Base):
    def setUp(self):
        super().setUp()
        self.server = tas_serve.Server(self.profile())
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), tas_serve.handler(self.server))
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"

    def sender(self, run):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(sock.close)
        address = "\0" + tas.input_address(run)[1:] + ".reach"

        def send(data):
            sock.sendto(data, address)

        return send

    def until(self, check):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if check():
                return
            time.sleep(0.005)
        self.fail("timed out")

    def test_records_reach_a_video_client_in_order_full_first_and_end_in_reach_bin(self):
        run = self.make_run("x", movie="boot-idle", status="running", speed=0, started=1.0, pid=os.getpid())
        bmp, _ = capture_bmp(320, 240)
        (run / "view.bmp").write_bytes(bmp)
        self.server.reach.sync([run])
        relay = self.server.reach.runs["x"]
        send = self.sender(run)
        self.assertTrue(relay.listening.wait(5))
        send(reach_record(1, 10, bitset([0, 1])))
        send(reach_record(2, 11, [9], [0]))
        self.until(lambda: relay.count == 2)
        video = Stream(self.base, "/video/x", tas_serve.VIDEO_HEADER)
        self.addCleanup(video.close)
        flagged, (_length, _stamp, _seq), body = video.record()
        self.assertTrue(flagged)
        self.assertEqual(body, reach_record(1, 11, bitset([0, 1, 9], 8)[:2] + bytes(6)))  # a viewer joins on the sum
        later = [reach_record(2, 12, [17, 18], [1]), reach_record(2, 13, [], [9]), reach_record(2, 14, [30], [])]
        for record in later:
            send(record)
        got = []
        while len(got) < len(later):
            flagged, _, body = video.record()
            if flagged:
                got.append(body)
            else:
                self.assertTrue(body.startswith(b"\xff\xd8"))  # the JPEG frames keep coming between records
        self.assertEqual(got, later)
        final = bitset([0, 1, 9, 17, 18, 30], 8)
        record = run / tas.RECORD
        record.write_text(json.dumps({"id": "x", "status": "done"}))
        self.until(lambda: (run / "reach.bin").is_file())
        self.assertEqual((run / "reach.bin").read_bytes(), final)
        self.assertTrue(relay.done)

    def test_a_queued_run_is_listened_for_before_its_port_starts_and_until_it_leaves_the_queue(self):
        run = self.server.cfg["runs"] / "plays" / "q1"
        patch = mock.patch.object(tas_serve, "REACH_READ_TIMEOUT", 0.01)
        patch.start()
        self.addCleanup(patch.stop)
        self.server._placeholder("q1", "boot-idle", 0, self.server.cfg["port"], None)
        relay = self.server.reach.runs["q1"]
        self.assertTrue(relay.listening.wait(5))
        self.assertFalse(run.exists())
        self.sender(run)(reach_record(1, 5, bitset([2], 8)))  # the port's first record, before the run folder exists
        self.until(lambda: relay.count == 1)
        self.assertEqual(relay.snapshot()[0], reach_record(1, 5, bitset([2], 8)))
        self.until(lambda: relay.idle >= 3)  # it asked whether the run is alive, and a queued run keeps its socket
        self.assertFalse(relay.done)
        self.server._forget("q1")
        self.until(lambda: relay.done)
        self.assertFalse((run / "reach.bin").exists())  # no folder was made for it

    def test_the_wall_carries_each_running_runs_reach_records_under_its_index_full_first(self):
        runs = [
            self.make_run(n, movie="boot-idle", status="running", speed=0, started=float(i), pid=os.getpid())
            for i, n in enumerate(("a", "b"))
        ]
        bmp, _ = capture_bmp(320, 240)
        for run in runs:
            (run / "view.bmp").write_bytes(bmp)
        self.server.reach.sync(runs)
        relay = self.server.reach.runs["a"]
        send = self.sender(runs[0])
        self.assertTrue(relay.listening.wait(5))
        send(reach_record(1, 10, bitset([0, 3])))
        self.until(lambda: relay.count == 1)
        wall = Stream(self.base, "/wall", tas_serve.VIDEO_HEADER)
        self.addCleanup(wall.close)
        names, reach = {}, []
        while len(reach) < 2:
            flagged, (_length, _stamp, index), body = wall.record()
            if index == tas_serve.WALL_KEEPALIVE_INDEX:
                continue
            if flagged and body.startswith(b"{"):
                names[index] = json.loads(body)["id"]
            elif flagged:
                self.assertIn(index, names)  # named before its first record
                reach.append((names[index], body))
                if len(reach) == 1:
                    send(reach_record(2, 11, [5], [3]))
        self.assertEqual(reach, [("a", reach_record(1, 10, bitset([0, 3]))), ("a", reach_record(2, 11, [5], [3]))])

    def test_a_malformed_datagram_is_dropped_and_a_laggard_is_resynced_with_a_full_record(self):
        run = self.make_run("y", movie="boot-idle", status="running", speed=0, started=1.0, pid=os.getpid())
        self.server.reach.sync([run])
        relay = self.server.reach.runs["y"]
        self.assertTrue(relay.listening.wait(5))
        send = self.sender(run)
        send(reach_record(1, 1, bitset([2])))
        self.until(lambda: relay.count == 1)
        for bad in (b"", b"\x09\0\0\0\0", reach_record(2, 2, [1], [])[:-1], reach_record(2, 2, [1], []) + b"x"):
            send(bad)
        send(reach_record(2, 3, [3], []))
        self.until(lambda: relay.count == 2)
        self.assertEqual(relay.rejected, 4)
        self.assertEqual(relay.bits, bytearray(bitset([2, 3])))
        feed = self.server.reach.feed_for(run)
        self.assertEqual(feed.first(), reach_record(1, 3, bitset([2, 3])))
        with mock.patch.object(tas_serve, "REACH_KEPT_RECORDS", 2):
            for poll in range(4, 9):
                relay.feed(reach_record(2, poll, [poll + 8], []))
        records = feed.pending()
        self.assertEqual(records, [reach_record(1, 8, relay.bits)])  # the gap is one full record, not stale deltas
        self.assertEqual(feed.pending(), [])

    def test_a_finished_run_serves_its_reach_bin_once_as_a_full_record(self):
        run = self.make_run("z", movie="boot-idle", status="done", speed=0, started=1.0, polls_reached=77)
        (run / "reach.bin").write_bytes(bitset([5], 4))
        feed = self.server.reach.feed_for(run)
        self.assertEqual(feed.first(), reach_record(1, 77, bitset([5], 4)))
        self.assertEqual(feed.pending(), [])
        self.assertIsNone(self.server.reach.feed_for(self.make_run("n", status="done")))


class DataTests(Base):
    def setUp(self):
        super().setUp()
        self.server = tas_serve.Server(self.profile())
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), tas_serve.handler(self.server))
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"
        self.site = self.server.site
        (self.site / "fn").mkdir(parents=True)

    def fetch(self, path, **headers):
        request = urllib.request.Request(self.base + path, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as reply:
                return reply.status, reply.headers, reply.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def test_static_files_of_the_page_are_typed_gzipped_and_confined(self):
        with mock.patch.object(tas_serve, "STATIC", self.root / "static"):
            (self.root / "static").mkdir()
            (self.root / "static" / "u.js").write_text("var u;" * 500)
            (self.root / "static" / "icons.svg").write_text("<svg>" + "<path/>" * 500 + "</svg>")
            (self.root / "secret.txt").write_text("no")
            status, head, body = self.fetch("/static/u.js", **{"Accept-Encoding": "gzip"})
            self.assertEqual((status, head["Content-Type"], head["Content-Encoding"]), (200, "text/javascript", "gzip"))
            self.assertEqual(gzip.decompress(body), ("var u;" * 500).encode())
            status, head, body = self.fetch("/static/icons.svg", **{"Accept-Encoding": "gzip"})
            self.assertEqual((status, head["Content-Type"], head["Content-Encoding"]), (200, "image/svg+xml", "gzip"))
            self.assertEqual(self.fetch("/static/u.js", **{"If-None-Match": head["ETag"]})[0], 200)  # a tag per file
            tag = self.fetch("/static/u.js")[1]["ETag"]
            self.assertEqual(self.fetch("/static/u.js", **{"If-None-Match": tag})[0], 304)
            for path in ("/static/nope.js", "/static/../secret.txt", "/static/%2e%2e/secret.txt"):
                self.assertEqual(self.fetch(path)[0], 404, path)

    def test_baked_files_with_gz_siblings_etags_and_cache_rules(self):
        plain = json.dumps({"rows": list(range(500))}).encode()
        (self.site / "fns.1a2b3c4d.json").write_bytes(plain)
        (self.site / "fns.1a2b3c4d.json.gz").write_bytes(gzip.compress(plain))
        (self.site / "live.json").write_bytes(plain)
        (self.site / "manifest.json").write_bytes(b"{}")
        (self.site / "movies.json").write_bytes(plain)
        (self.site / "fn" / "Foo.json").write_bytes(b'{"c": 1}')
        (self.site / "reach").mkdir()
        (self.site / "reach" / "m.bin").write_bytes(b"\x01\x02")
        status, head, body = self.fetch("/data/fns.1a2b3c4d.json", **{"Accept-Encoding": "gzip"})
        self.assertEqual((status, head["Content-Encoding"], head["Content-Type"]), (200, "gzip", "application/json"))
        self.assertEqual(gzip.decompress(body), plain)
        self.assertEqual(head["Cache-Control"], "public, max-age=31536000, immutable")
        tag = head["ETag"]
        self.assertEqual(
            self.fetch("/data/fns.1a2b3c4d.json", **{"If-None-Match": tag, "Accept-Encoding": "gzip"})[::2], (304, b"")
        )
        status, head, body = self.fetch("/data/fns.1a2b3c4d.json")  # no gzip accepted: the plain bytes
        self.assertEqual((status, head.get("Content-Encoding"), body), (200, None, plain))
        self.assertNotEqual(head["ETag"], tag)
        status, head, body = self.fetch("/data/live.json", **{"Accept-Encoding": "gzip"})  # compressed once per tag
        self.assertEqual(
            (head["Cache-Control"], head["Content-Encoding"], gzip.decompress(body)), ("no-store", "gzip", plain)
        )
        self.assertEqual(self.fetch("/data/manifest.json")[1]["Cache-Control"], "no-store")
        self.assertEqual(self.fetch("/data/movies.json")[1]["Cache-Control"], "no-cache")
        status, head, body = self.fetch("/data/fn/Foo.json", **{"Accept-Encoding": "gzip"})
        self.assertEqual((head.get("Content-Encoding"), body), (None, b'{"c": 1}'))  # small: sent as it is
        status, head, body = self.fetch("/data/reach/m.bin")
        self.assertEqual((head["Content-Type"], body), ("application/octet-stream", b"\x01\x02"))

    def test_other_paths_are_404(self):
        (self.site / "x.json.gz").write_bytes(b"z")
        (self.root / "runs" / "secret.txt").write_text("no")
        for path in (
            "/data/nope.json",
            "/data/x.json.gz",
            "/data/",
            "/data/../secret.txt",
            "/data/%2e%2e/secret.txt",
            "/data/fn",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.fetch(path)[0], 404)

    def test_the_routes_the_runner_page_replaced_are_gone(self):
        for path in (
            "/api/state", "/api/board", "/api/corpus", "/api/points", "/api/maze", "/api/fn/func_80256C90_de",
            "/api/calls/func_80256C90_de", "/fn/func_80256C90_de", "/api/phase/map",
        ):  # fmt: skip
            with self.subTest(path=path):
                self.assertEqual(self.fetch(path)[0], 404)
        request = urllib.request.Request(self.base + "/api/play-all", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 404)
        for path in ("/api/pool", "/api/runs", "/api/coverage", "/"):  # the kept ones still answer
            self.assertEqual(self.fetch(path)[0], 200)


if __name__ == "__main__":
    unittest.main()
