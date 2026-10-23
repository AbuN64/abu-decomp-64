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
        self.assertEqual(set(state), {"now", "build", "runs", "movies", "lanes", "inbox"})
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
        movies = {m["name"]: m for m in state["movies"]}
        self.assertEqual(movies["boot-idle"]["polls"], 900)
        self.assertIsNone(movies["boot-idle"]["fn"])
        preview = movies["model-preview"]
        self.assertEqual((preview["outcome"], preview["reached"], preview["same_twice"]), ("crash", 611, True))
        self.assertEqual(preview["fn"], {"name": "func_80256C90_de", "state": "fuzzy"})
        self.assertEqual(preview["train"], {"line": "L1 /x/final-func_80256C90_de.diff exact", "mark": "LANDED"})
        self.assertEqual(preview["last_run"], 6.0)
        self.assertEqual((preview["root"], preview["from"], preview["at"]), ("model-preview", None, None))
        self.assertEqual(state["lanes"], [])  # no lanes folder configured
        self.assertEqual([row["mark"] for row in state["inbox"]], ["LANDED", "DROPPED", None])
        self.assertEqual(state["inbox"][1]["line"], "DEPLOY: decomp 1e51919")

    def test_takes_fold_under_their_root_movie(self):
        sha = hashlib.sha1(b"rom").hexdigest()
        movies = self.root / "movies"
        (movies / "boot-idle-take-20261023-010101.tas").write_text(
            f"# tas 1\n# rom-sha1 {sha}\n# from boot-idle\n# at 400\n- *500\n"
        )
        (movies / "boot-idle-take-20261023-010101-take-20261023-020202.tas").write_text(
            f"# tas 1\n# rom-sha1 {sha}\n- *9\n"
        )
        rows = {m["name"]: m for m in tas_serve.Server(self.profile()).state()["movies"]}
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

    def test_board_reads_progress_history_landed_and_the_ranked_list(self):
        states = self.root / "states.json"
        states.write_text(json.dumps({
            "generated": 5, "head": "abc", "head_subject": "s",
            "bridge": {"versions": {"us-rev1": {"linked": True, "booted": True, "units_in": 3, "units_out": 1}}},
            "versions": {"us-rev1": {"exact_percent": 90.5, "fuzzy_percent": 100.0, "bytes_total": 10,
                                     "code": {"exact": 9, "fuzzy": 1, "none": 0}, "functions": {"exact": 4},
                                     "none_fns": [["func_8000_de", 4, "src/a.c"]]}},
        }))  # fmt: skip
        (self.root / "history.jsonl").write_text('{"generated": 1, "pct": {"us-rev1": [89.0, 99.0]}}\nbad\n'
                                                 '{"generated": 2, "pct": {"us-rev1": [90.5, 100.0]}}\n')  # fmt: skip
        ranked = mock.patch.object(tas_serve.Server, "ranked", return_value=[{"rank": 1, "kind": "owes a match"}])
        landed = mock.patch.object(
            tas_serve.tas_board, "landed", return_value=[{"sha": "f", "time": 1, "subject": "Match x"}]
        )
        with ranked, landed:
            server = tas_serve.Server(self.profile())
            board = server.board()
            for _ in range(200):
                if server.work.view()["rows"] is not None:
                    break
                time.sleep(0.01)
            work = server.work.view()
        version = board["decomp"]["versions"][0]
        self.assertEqual(
            (version["name"], version["exact_pct"], version["none_fns"]), ("us-rev1", 90.5, ["func_8000_de"])
        )
        self.assertEqual(version["spark"], [[1, 89.0, 99.0], [2, 90.5, 100.0]])
        self.assertEqual(version["bridge"]["units_in"], 3)
        self.assertEqual(board["landed"][0]["subject"], "Match x")
        self.assertEqual(work["rows"], [{"rank": 1, "kind": "owes a match"}])
        self.assertIsNone(work["error"])

    def test_lanes_and_labelled_runs(self):
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
        rows = {r["name"]: r for r in state["lanes"]}
        self.assertEqual(set(rows), {"TAS-CYCLE-1", "TAS-CYCLE-2", "FUZZY-A", "OTHER"})  # every lane with a brief
        self.assertEqual(
            (rows["FUZZY-A"]["kind"], rows["TAS-CYCLE-1"]["kind"], rows["OTHER"]["kind"]), ("impl", "cycle", "cycle")
        )
        self.assertEqual([r["name"] for r in state["lanes"]][-1], "TAS-CYCLE-2")  # done goes last
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
            self.assertEqual({queued[i]["status"] for i in reply["ids"]}, {"queued"})
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

        started, paks = [], []

        def fake_play(profile, movie, *, speed=0, run_id=None, port=None, label=None, pak=None, cpak=None, **kwargs):
            started.append((movie, speed, run_id, label))
            paks.append((pak, cpak))
            hold.wait(10)
            return {}

        hold = threading.Event()
        with mock.patch.object(tas, "play", fake_play):
            self.assertEqual(self.post("/api/play", {"label": "owner", "pak": "maybe"})[0], 400)
            self.assertEqual(self.post("/api/play", {"label": "owner", "cpak": "maybe"})[0], 400)
            status, reply = self.post("/api/play", {"label": "owner", "pak": "off", "cpak": "off"})
            self.assertEqual(status, 200)
            self.assertTrue(reply["movie"].startswith("play-"))
            for _ in range(100):
                if started:
                    break
                time.sleep(0.01)
            self.assertEqual(started, [(None, 1, reply["id"], "owner")])
            row = {r["id"]: r for r in json.loads(self.get("/api/state")[2])["runs"]}[reply["id"]]
            self.assertEqual((row["input"], row["label"], row["speed"], row["pak"]), ("human", "owner", 1, "off"))
            self.assertEqual(row["cpak"], "none")  # the page shows an empty slot for a run without the card
            self.assertEqual(paks, [("off", "off")])
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
            row = {r["id"]: r for r in json.loads(self.get("/api/state")[2])["runs"]}[reply["id"]]
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
