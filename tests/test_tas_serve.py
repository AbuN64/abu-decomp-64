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
        self.assertEqual(cycle["target"], {"movie": "model-preview", "fn": "func_80256C90_de", "kind": "host fault"})
        self.assertEqual([row["mark"] for row in cycle["inbox"]], ["LANDED", "DROPPED", None])
        self.assertEqual(cycle["inbox"][1]["line"], "DEPLOY: decomp 1e51919")

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

        def fake_play(profile, movie, *, speed=0, run_id=None, **kwargs):
            started.append((movie, speed, run_id))
            return {}

        with mock.patch.object(tas, "play", fake_play):
            status, reply = self.post("/api/play", {"movie": "boot-idle", "speed": 1})
            self.assertEqual(status, 200)
            self.assertTrue(reply["id"].endswith("-boot-idle"))
            for _ in range(100):
                if started:
                    break
                time.sleep(0.01)
            self.assertEqual(started, [("boot-idle", 1, reply["id"])])
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
        with mock.patch.object(tas_serve.os, "killpg") as kill:
            self.assertEqual(self.post("/api/stop", {"id": "r3"}), (200, {"id": "r3", "stopped": True}))
        kill.assert_called_once_with(4242, tas_serve.signal.SIGTERM)
        self.assertTrue((run / "stopped").is_file())
        self.assertEqual(self.post("/api/stop", {"id": "missing"})[0], 400)


if __name__ == "__main__":
    unittest.main()
