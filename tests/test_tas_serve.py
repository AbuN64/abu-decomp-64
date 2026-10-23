"""`tas serve` against the page contract (orch plans/TAS-PAGE-CONTRACT.md): state from run folders, frames, logs,
the 1x sound as a growing WAV, play / play-all / stop. Mocked: no port runs."""

import hashlib
import json
import os
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

from abu_decomp import DecompError, tas, tas_serve
from abu_decomp.config import load_profile


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
        self.assertEqual(set(state), {"now", "build", "runs", "movies", "cycle"})
        self.assertEqual(state["build"]["port"], str(self.root / "port"))
        self.assertGreater(state["build"]["built"], 0)
        runs = {r["id"]: r for r in state["runs"]}
        self.assertEqual([r["started"] for r in state["runs"]], [1.0, 5.0, 10.0])
        now = runs["20261022T170000Z-boot-idle"]
        self.assertEqual((now["status"], now["poll"], now["polls"], now["outcome"]), ("running", 2, 900, None))
        self.assertEqual(now["frame"], "/frame/20261022T170000Z-boot-idle")
        self.assertEqual(runs["20261022T150000Z-boot-idle"]["status"], "done")  # its process is gone
        done = runs["20261022T160000Z-model-preview"]
        self.assertEqual((done["outcome"], done["poll"], done["subject"]), ("crash", 611, "host-fault-10"))
        movies = {m["name"]: m for m in state["movies"]}
        self.assertEqual(movies["boot-idle"]["polls"], 900)
        self.assertIsNone(movies["boot-idle"]["fn"])
        preview = movies["model-preview"]
        self.assertEqual((preview["outcome"], preview["reached"], preview["same_twice"]), ("crash", 611, True))
        self.assertEqual(preview["fn"], {"name": "func_80256C90_de", "state": "fuzzy"})
        self.assertEqual(preview["train"], {"line": "L1 /x/final-func_80256C90_de.diff exact", "mark": "LANDED"})
        self.assertEqual(preview["last_run"], 6.0)
        cycle = state["cycle"]
        self.assertEqual(cycle["lanes"], [])  # no lanes folder configured
        self.assertEqual([row["mark"] for row in cycle["inbox"]], ["LANDED", "DROPPED", None])
        self.assertEqual(cycle["inbox"][1]["line"], "DEPLOY: decomp 1e51919")

    def test_cycle_lanes_and_labelled_runs(self):
        lanes = self.root / "lanes"
        for name, brief in (("TAS-CYCLE-1", "KIND: cycle\nOWNER\n# TAS-CYCLE-1 (Sonnet): splash off center.\n"),
                            ("TAS-CYCLE-2", "KIND: cycle\n# TAS-CYCLE-2: texturing\n"),
                            ("FUZZY-A", "KIND: impl\n# not a cycle\n"), ("OTHER", "")):  # fmt: skip
            (lanes / name).mkdir(parents=True)
            (lanes / name / "brief.md").write_text(brief)
        (lanes / "OTHER" / "cycle.json").write_text('{"title": "from cycle.json"}')
        (lanes / "TAS-CYCLE-1" / "STATUS.md").write_text("17:47 start\n17:55 found it\n\n")
        (lanes / "TAS-CYCLE-2" / "STATUS.md").write_text("17:47 start\n")
        (lanes / "TAS-CYCLE-2" / "RESULT.md").write_text("\nDONE: fixed\nmore\n")
        server = tas_serve.Server(self.profile(lanes="lanes"))
        self.make_run("r1", movie="boot-idle", status="running", speed=1, started=1.0, pid=os.getpid(),
                      label="TAS-CYCLE-1", port="/x/ragewars")  # fmt: skip
        state = server.state()
        rows = {r["name"]: r for r in state["cycle"]["lanes"]}
        self.assertEqual(set(rows), {"TAS-CYCLE-1", "TAS-CYCLE-2", "OTHER"})
        self.assertEqual([r["name"] for r in state["cycle"]["lanes"]][-1], "TAS-CYCLE-2")  # done goes last
        one = rows["TAS-CYCLE-1"]
        self.assertEqual((one["title"], one["status"], one["result"], one["runs"]),
                         ("TAS-CYCLE-1 (Sonnet): splash off center.", "17:55 found it", None, 1))  # fmt: skip
        self.assertGreater(one["status_at"], 0)
        self.assertEqual(rows["TAS-CYCLE-2"]["result"], "DONE: fixed")
        self.assertEqual(rows["OTHER"]["title"], "from cycle.json")
        run = state["runs"][0]
        self.assertEqual((run["label"], run["port"]), ("TAS-CYCLE-1", "/x/ragewars"))

    def test_fn_state_reads_the_source_and_the_progress_states(self):
        server = tas_serve.Server(self.profile())
        (self.project / "src" / "u.CPP").write_text("void func_80209CD8_de(int a)\n{\n}\n")
        self.assertEqual(server.fn_state("func_80209CD8_de"), "exact")
        self.assertEqual(server.fn_state("func_80256C90_de"), "fuzzy")
        self.assertEqual(server.fn_state("func_80000000"), "none")
        self.assertEqual(server.fn_state("func_8041CAD8_de"), "none")

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

    def test_page_state_frame_and_log(self):
        status, kind, body = self.get("/")
        self.assertEqual((status, kind), (200, "text/html; charset=utf-8"))
        self.assertIn(b"/api/state", body)
        self.assertEqual(json.loads(self.get("/api/state")[2])["runs"], [])
        run = self.make_run("r1", movie="boot-idle", status="done", speed=0, started=1.0, outcome="ok")
        self.assertEqual(self.status("/frame/r1"), 404)  # no picture yet
        (run / "view.bmp").write_bytes(b"BMpicture")
        self.assertEqual(self.get("/frame/r1")[1:], ("image/bmp", b"BMpicture"))
        (run / "output.txt").write_bytes(b"line one\nline two\n")
        self.assertEqual(self.get("/log/r1?from=0")[2], b"line one\nline two\n")
        self.assertEqual(self.get("/log/r1?from=9")[2], b"line two\n")
        self.assertEqual(self.status("/log/nope"), 404)
        self.assertEqual(self.status("/frame/..%2Fx"), 404)
        self.assertEqual(self.status("/audio/r1"), 404)  # turbo runs have no sound

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

    def test_play_play_all_and_stop(self):
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
            queued = {r["id"]: r for r in json.loads(self.get("/api/state")[2])["runs"]}[reply["id"]]
            self.assertEqual((queued["label"], queued["port"]), ("L-1", str(exe)))
            for _ in range(100):
                if len(started) == 2:
                    break
                time.sleep(0.01)
            self.assertEqual(started[1][3:], (str(exe), "L-1"))
            hold.set()
            self.assertEqual(self.post("/api/play", {"movie": "boot-idle", "port": str(self.root / "nope")})[0], 400)
            self.assertEqual(self.post("/api/play", {"movie": "boot-idle", "label": "bad label!"})[0], 400)
        self.assertEqual(self.post("/api/play", {"movie": "nope", "speed": 0})[0], 400)
        release = threading.Event()
        with mock.patch.object(tas, "play_all", side_effect=lambda *a, **k: release.wait(10)) as play_all:
            status, reply = self.post("/api/play-all", {})
            self.assertEqual(len(reply["ids"]), 2)
            state = json.loads(self.get("/api/state")[2])
            queued = {r["id"]: r for r in state["runs"]}
            self.assertEqual({queued[i]["status"] for i in reply["ids"]}, {"running"})
            for _ in range(100):
                if play_all.called:
                    break
                time.sleep(0.01)
            self.assertEqual(set(play_all.call_args.kwargs["run_ids"].values()), set(reply["ids"]))
            release.set()
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

        started = []

        def fake_play(profile, movie, *, speed=0, run_id=None, port=None, label=None, **kwargs):
            started.append((movie, speed, run_id, label))
            hold.wait(10)
            return {}

        hold = threading.Event()
        with mock.patch.object(tas, "play", fake_play):
            status, reply = self.post("/api/play", {"label": "owner"})
            self.assertEqual(status, 200)
            self.assertTrue(reply["movie"].startswith("play-"))
            for _ in range(100):
                if started:
                    break
                time.sleep(0.01)
            self.assertEqual(started, [(None, 1, reply["id"], "owner")])
            row = {r["id"]: r for r in json.loads(self.get("/api/state")[2])["runs"]}[reply["id"]]
            self.assertEqual((row["input"], row["label"], row["speed"]), ("human", "owner", 1))
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
        state = {r["id"]: r for r in json.loads(self.get("/api/state")[2])["runs"]}
        self.assertEqual(state["r5"]["input"], "human")
        self.assertEqual(self.post("/api/play", {})[0], 400)  # r5 is driven by live input now
        self.assertEqual(self.post("/api/take", {"id": "nope"})[0], 400)

    def test_frame_long_poll_names_the_frame_and_the_input_it_shows(self):
        run = self.make_run("r6", movie="boot-idle", status="running", speed=1, started=1.0, pid=os.getpid())
        picture = bytearray(b"BM" + bytes(60))
        picture[6:10] = (42).to_bytes(4, "little")
        (run / "view.bmp").write_bytes(bytes(picture))
        request = urllib.request.urlopen(self.base + "/frame/r6", timeout=10)
        stamp = int(request.headers["X-Frame"])
        self.assertEqual((request.headers["X-Input-Seq"], request.read()), ("42", bytes(picture)))
        began = time.monotonic()
        with urllib.request.urlopen(self.base + f"/frame/r6?after={stamp}", timeout=10) as reply:
            self.assertEqual(reply.status, 204)  # nothing newer within a second
        self.assertGreater(time.monotonic() - began, 0.9)

        def newer():
            time.sleep(0.2)
            picture[6:10] = (43).to_bytes(4, "little")
            (run / "view.tmp").write_bytes(bytes(picture))
            os.utime(run / "view.tmp", ns=(stamp * 1000 + 5_000_000,) * 2)
            (run / "view.tmp").replace(run / "view.bmp")

        threading.Thread(target=newer, daemon=True).start()
        with urllib.request.urlopen(self.base + f"/frame/r6?after={stamp}", timeout=10) as reply:
            self.assertEqual((reply.status, reply.headers["X-Input-Seq"]), (200, "43"))
            self.assertGreater(int(reply.headers["X-Frame"]), stamp)


if __name__ == "__main__":
    unittest.main()
