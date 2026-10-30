"""`tas play --oracle` and the port | original comparison (module tas_oracle). Mocked: no port or oracle runs."""

import hashlib
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from abu_decomp import DecompError, tas, tas_oracle, tas_serve
from abu_decomp.config import load_profile

W, H = tas_oracle.WIDTH, tas_oracle.HEIGHT


def picture(rgb=(0, 0, 0), box=None, box_rgb=(255, 255, 255)) -> bytes:
    """W x H RGB bytes, optionally with a filled box (x0, y0, x1, y1)."""
    pixels = bytearray(bytes(rgb) * (W * H))
    if box:
        x0, y0, x1, y1 = box
        for y in range(y0, y1):
            pixels[(y * W + x0) * 3 : (y * W + x1) * 3] = bytes(box_rgb) * (x1 - x0)
    return bytes(pixels)


def write_shot(folder: Path, poll: int, pixels: bytes) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"poll-{poll:06d}.ppm").write_bytes(tas_oracle.PPM_HEAD + pixels)


class VerdictTests(unittest.TestCase):
    """The verdict reads state-diff.json as `tas_statediff.finish` writes it."""

    @staticmethod
    def record(**coverage):
        full = {
            "complete": True,
            "port_first": 1,
            "port_last": 100,
            "oracle_first": 1,
            "oracle_last": 100,
            "only_port": 0,
            "only_oracle": 0,
            "first_missing": None,
        }
        return {
            "outcome": "ok",
            "oracle": {"outcome": "ok"},
            "polls": 100,
            "state_diff": {"polls_compared": 100, "divergent": 0, "coverage": {**full, **coverage}},
        }

    def test_full_coverage_passes(self):
        self.assertEqual(tas_oracle.verdict(self.record()), "ok")

    def test_the_pp_1p_slice_with_a_one_sided_tail_fails(self):
        # pp-1p-match: port_polls 59874, oracle_polls 23368, polls_compared 23368, divergent 0 passed before.
        legacy = {
            "outcome": "ok",
            "oracle": {"outcome": "ok"},
            "state_diff": {"polls_compared": 23368, "port_polls": 59874, "oracle_polls": 23368, "divergent": 0},
        }
        self.assertEqual(tas_oracle.verdict(legacy), "fail")  # a state-diff with no coverage never passes
        tail = self.record(complete=False, port_last=59874, oracle_last=23368, only_port=36506)
        self.assertEqual(tas_oracle.verdict(tail), "fail")

    def test_prefix_and_interior_gaps_fail(self):
        self.assertEqual(tas_oracle.verdict(self.record(complete=False, oracle_first=5, only_port=4)), "fail")
        self.assertEqual(tas_oracle.verdict(self.record(complete=False, only_oracle=10)), "fail")

    def test_equal_traces_that_stop_before_the_movie_end_fail(self):
        self.assertEqual(tas_oracle.verdict(self.record(port_last=60, oracle_last=60)), "fail")

    def test_divergence_and_bad_outcomes_still_fail(self):
        record = self.record()
        record["state_diff"]["divergent"] = 1
        self.assertEqual(tas_oracle.verdict(record), "fail")
        record = self.record()
        record["oracle"]["outcome"] = "crash"
        self.assertEqual(tas_oracle.verdict(record), "fail")


class ScoreTests(unittest.TestCase):
    def test_diff_share_counts_visibly_different_pixels(self):
        black = picture()
        self.assertEqual(tas_oracle.diff_share(black, black), 0.0)
        self.assertEqual(tas_oracle.diff_share(black, picture((31, 31, 31))), 0.0)  # under 32: never counts
        self.assertEqual(tas_oracle.diff_share(picture((100, 0, 0)), picture((70, 0, 0))), 0.0)
        self.assertEqual(tas_oracle.diff_share(black, picture((0, 64, 0))), 1.0)  # 64 or more: always counts
        self.assertEqual(tas_oracle.diff_share(black, picture(box=(0, 0, W // 2, H))), 0.5)
        with self.assertRaises(ValueError):
            tas_oracle.diff_share(black, black[:-3])

    def test_bmp_is_bottom_up_bgr(self):
        rgb = picture((1, 2, 3), box=(0, 0, W, 1), box_rgb=(10, 20, 30))  # top row differs
        data = tas_oracle.bmp(rgb)
        self.assertEqual(data[:2], b"BM")
        self.assertEqual(int.from_bytes(data[18:22], "little"), W)
        self.assertEqual(len(data), 54 + W * H * 3)
        self.assertEqual(data[54:57], bytes((3, 2, 1)))  # first stored row is the bottom one, BGR
        self.assertEqual(data[-3:], bytes((30, 20, 10)))  # last stored row is the top one

    def test_outcome(self):
        self.assertEqual(tas_oracle.outcome(0), {"outcome": "ok"})
        self.assertEqual(tas_oracle.outcome(None), {"outcome": "hang"})
        self.assertEqual(tas_oracle.outcome(-11), {"outcome": "crash", "subject": "SIGSEGV"})
        self.assertEqual(tas_oracle.outcome(5), {"outcome": "exit", "subject": "exit 5"})


class CompareTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run_dir = Path(directory.name)

    def shots(self, side, frames):
        for poll, pixels in frames.items():
            write_shot(self.run_dir / "shots" / side, poll, pixels)

    def test_same_poll_scores_drift_and_vi_drift(self):
        a, b, c, d = (picture(box=(0, 0, W * k // 8, H)) for k in (1, 3, 5, 7))
        # The port shows each screen one shot (10 polls) after the original does.
        self.shots("port", {10: a, 20: a, 30: b, 40: c, 50: d})
        self.shots("oracle", {10: a, 20: b, 30: c, 40: d, 50: d, 60: d, 70: d, 80: d})
        (self.run_dir / "port-trace.txt").write_text("10 20\n20 40\n30 60\n")
        (self.run_dir / "oracle-trace.txt").write_text("10 21\n20 42\n30 63\n40 8")  # the last line is half written
        (self.run_dir / "shots" / "port" / "poll-000060.ppm").write_bytes(tas_oracle.PPM_HEAD + b"\0")  # incomplete
        rows = {r["poll"]: r for r in tas_oracle.compare(self.run_dir, 10, oracle_done=False)}
        self.assertEqual(sorted(rows), [10, 20, 30, 40, 50])
        self.assertEqual((rows[10]["diff"], rows[10]["drift"], rows[10]["vi_drift"]), (0.0, 0, 1))
        self.assertEqual((rows[30]["diff"], rows[30]["drift"], rows[30]["vi_drift"]), (0.25, -10, 3))
        self.assertEqual(rows[40]["drift"], -10)
        self.assertIsNone(rows[40]["vi_drift"])  # no complete trace line for poll 40 on the oracle side
        self.assertEqual((rows[50]["diff"], rows[50]["drift"]), (0.0, 0))
        cached = json.loads((self.run_dir / "diff.json").read_text())
        self.assertEqual([r["poll"] for r in cached["rows"]], [10, 20, 30, 40, 50])  # oracle shots to 80: all settled
        with mock.patch.object(tas_oracle, "diff_share", side_effect=AssertionError("recomputed")):
            self.assertEqual(len(tas_oracle.compare(self.run_dir, 10, oracle_done=False)), 5)  # read from the cache

    def test_rows_wait_for_their_neighbours_until_the_oracle_ends(self):
        a = picture()
        self.shots("port", {10: a, 20: a})
        self.shots("oracle", {10: a, 20: a})
        self.assertEqual(len(tas_oracle.compare(self.run_dir, 10, oracle_done=False)), 2)
        self.assertFalse((self.run_dir / "diff.json").exists())  # poll 10 could still match poll 40
        self.assertEqual(len(tas_oracle.compare(self.run_dir, 10, oracle_done=True)), 2)
        self.assertEqual(len(json.loads((self.run_dir / "diff.json").read_text())["rows"]), 2)

    def test_first_divergence_names_the_first_aligned_poll_over_each_threshold(self):
        rows = [
            {"poll": 25, "diff": 0.015, "drift": 0, "vi_drift": 0},
            {"poll": 50, "diff": 0.5, "drift": 1, "vi_drift": 3},
            {"poll": 75, "diff": 0.03, "drift": 0, "vi_drift": 2},
        ]
        got = tas_oracle.first_divergence(rows)
        self.assertEqual(got["diverge_1pct"], {"poll": 25, "diff": 0.015, "vi_drift": 0})
        self.assertEqual(got["diverge_2pct"], {"poll": 75, "diff": 0.03, "vi_drift": 2})
        self.assertEqual(tas_oracle.first_divergence([]), {"diverge_1pct": None, "diverge_2pct": None})

    def test_summary(self):
        self.assertIsNone(tas_oracle.summary(self.run_dir, {"id": "x"}))
        self.shots("port", {25: picture(), 50: picture((0, 255, 0))})
        self.shots("oracle", {25: picture(), 50: picture()})
        (self.run_dir / "oracle-trace.txt").write_text("49 70\n50 71\n")
        record = {"oracle": {"status": "done", "every": 25, "polls_reached": 50, "outcome": "ok", "fps": 900.0}}
        got = tas_oracle.summary(self.run_dir, record)
        self.assertEqual(got["frame"], f"/oracle/{self.run_dir.name}")
        self.assertEqual((got["status"], got["poll"], got["outcome"], got["every"]), ("done", 50, "ok", 25))
        self.assertEqual((got["max_diff"], got["max_poll"], got["diff"], got["diff_poll"]), (1.0, 50, 1.0, 50))
        running = tas_oracle.summary(self.run_dir, {"oracle": {"status": "running", "every": 25, "pid": os.getpid()}})
        self.assertEqual((running["status"], running["poll"], running["outcome"]), ("running", 50, None))
        ended = tas_oracle.summary(self.run_dir, {"oracle": {"status": "running", "every": 25, "pid": 2**22 + 7}})
        self.assertEqual((ended["status"], ended["poll"]), ("done", 50))  # reaped before the record says so


class PlayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "game.z64").write_bytes(b"rom")
        (self.root / "port").write_text("")
        self.host = self.root / "n64host"
        self.host.write_text("")
        self.host.chmod(0o755)
        (self.root / "movies").mkdir()
        sha = hashlib.sha1(b"rom").hexdigest()
        (self.root / "movies" / "m.tas").write_text(f"# tas 1\n# rom-sha1 {sha}\nA *50\n")
        self.seat = self.root / "seat.json"
        self.tas = {"movies": "movies", "runs": "runs", "port": "port", "rom": "game.z64", "args": [], "exit_after": 7}
        self.profile = self.load(oracle={"host": "n64host", "env": {"N64R_ROM": "orig.z64"}, "shot_every": 25})

    def load(self, **extra):
        self.seat.write_text(json.dumps({"schema_version": 1, "name": "t", "tas": {**self.tas, **extra}}))
        return load_profile(self.seat)

    def test_settings(self):
        self.assertIsNone(tas_oracle.settings(self.load()))
        with self.assertRaises(DecompError):
            tas_oracle.required(self.load())
        with self.assertRaises(DecompError):
            tas_oracle.required(self.load(oracle={"host": "missing"}))
        with self.assertRaises(DecompError):
            tas_oracle.settings(self.load(oracle={"host": "n64host", "shot_every": 0}))
        got = tas_oracle.required(self.profile)
        self.assertEqual((got["host"], got["shot_every"], got["timeout_seconds"]), (self.host, 25, 300))

    def test_a_stale_oracle_is_refused_loudly(self):
        """The oracle ensure stamps the cake-taste-64 sha it built; a host older than the tree's HEAD is refused."""
        tree = self.root / "tree"
        tree.mkdir()
        git = ["git", "-C", str(tree), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git[:3], "init", "-q"], check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "one"], check=True)
        head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        stamp = self.root / "oracle-pins.json"
        table = {"host": "n64host", "pins": "oracle-pins.json", "pins_repo": "tree", "rebuild": "page-port.py ensure"}
        stamp.write_text(json.dumps({"cake_taste": head}))
        self.assertEqual(tas_oracle.required(self.load(oracle=table))["host"], self.host)
        stamp.write_text(json.dumps({"cake_taste": "0" * 40}))
        with self.assertRaises(DecompError) as raised:
            tas_oracle.required(self.load(oracle=table))
        self.assertIn("older than pins", str(raised.exception))
        self.assertIn("page-port.py ensure", str(raised.exception))
        stamp.unlink()  # a build that ended without its stamp is unknown, also refused
        with self.assertRaises(DecompError):
            tas_oracle.required(self.load(oracle=table))
        self.assertEqual(tas_oracle.required(self.load(oracle=table), fresh=False)["host"], self.host)

    def test_play_runs_the_original_beside_the_port(self):
        seen = {}
        host = str(self.host)

        class Process:
            def __init__(self, argv, env, stdout, **kwargs):
                self.pid = 4242 if argv[0] == host else 4343
                self.stdin = io.StringIO()
                if argv[0] == host:  # the oracle: pictures at polls 25 and 50, its trace
                    seen["oracle"] = (env, kwargs.get("cwd"))
                    for poll in (25, 50):
                        write_shot(Path(env["N64R_TAS_SHOT_DIR"]), poll, picture())
                    Path(env["N64R_TAS_TRACE"]).write_text("25 40\n50 80\n57 90\n")
                    Path(env["N64R_VIEW"]).write_bytes(b"BM")
                    return
                seen["port"] = env
                write_shot(Path(env["N64C_TAS_SHOT_DIR"]), 25, picture())
                write_shot(Path(env["N64C_TAS_SHOT_DIR"]), 50, picture(box=(0, 0, W // 4, H)))
                Path(env["N64C_TAS_TRACE"]).write_text("25 40\n50 82\n")
                Path(env["N64C_TAS_RECORD"]).write_text("8000 0 0\n" * 50)
                stdout.write("n64c-tas: done polls=57\n")

        def fake_wait(process, timeout, run=None):
            return 0, 0.5

        with mock.patch.object(tas.subprocess, "Popen", Process), mock.patch.object(tas, "_wait", fake_wait):
            result = tas.play(self.profile, "m", oracle=True, oracle_pictures=True, pak="off", cpak="on")
        run = Path(result["run"])
        oracle_env, cwd = seen["oracle"]
        self.assertEqual(cwd, run)
        self.assertEqual(oracle_env["N64R_TAS_PLAY"], str(run / "movie.raw"))
        self.assertEqual((oracle_env["N64R_TAS_EXIT_AFTER"], oracle_env["N64R_TAS_SHOT_EVERY"]), ("7", "25"))
        self.assertEqual((oracle_env["N64R_CLOCK"], oracle_env["SDL_VIDEODRIVER"]), ("entries", "offscreen"))
        self.assertEqual(oracle_env["N64R_AUDIO_OUT"], str(run / "audio-oracle.pcm"))  # beside the port's audio.pcm
        self.assertEqual(oracle_env["N64R_PAK"], "off")  # the run's Expansion Pak, as the port's --pak
        self.assertEqual(oracle_env["N64R_CPAK"], str(run / "oracle-cpak.mpk"))  # its own copy of the port's start card
        self.assertEqual((run / "oracle-cpak.mpk").read_bytes(), (run / "cpak-start.mpk").read_bytes())
        self.assertEqual((oracle_env["N64R_VIEW"], oracle_env["N64R_ROM"]), (str(run / "oracle.bmp"), "orig.z64"))
        port_env = seen["port"]
        self.assertEqual(port_env["N64C_TAS_SHOT_EVERY"], "25")
        self.assertEqual(port_env["N64C_TAS_SHOT_DIR"], str(run / "shots" / "port"))
        self.assertEqual(port_env["GCOV_PREFIX"], str(run / "gcov"))  # counters stay in the run, not the build tree
        self.assertEqual(port_env["N64C_TAS_TRACE"], str(run / "port-trace.txt"))
        oracle = result["oracle"]
        self.assertEqual((oracle["outcome"], oracle["polls_reached"], oracle["status"]), ("ok", 57, "done"))
        self.assertEqual((oracle["max_diff"], oracle["max_poll"], oracle["vi_drift"]), (0.25, 50, -2))
        record = tas.read_record(run)
        self.assertEqual((record["oracle"]["outcome"], record["oracle"]["every"]), ("ok", 25))
        with self.assertRaises(DecompError):
            tas.play(self.profile, None, oracle=True)  # a human run has no movie to replay
        with self.assertRaises(DecompError):
            tas.play(self.load(), "m", oracle=True)  # no oracle on this seat

    def test_no_picture_is_dumped_unless_asked(self):
        # a monkey batch dumped ~650 .ppm per run and filled the rig disk; the oracle keeps its checkpoints
        seen = {}
        host = str(self.host)

        class Process:
            def __init__(self, argv, env, stdout, **kwargs):
                self.pid = 4242 if argv[0] == host else 4343
                self.stdin = io.StringIO()
                seen["oracle" if argv[0] == host else "port"] = env
                if argv[0] != host:
                    Path(env["N64C_TAS_RECORD"]).write_text("8000 0 0\n" * 50)
                    stdout.write("n64c-tas: done polls=57\n")

        with (
            mock.patch.object(tas.subprocess, "Popen", Process),
            mock.patch.object(tas, "_wait", lambda *a, **k: (0, 0.5)),
        ):
            result = tas.play(self.profile, "m", oracle=True)
        run = Path(result["run"])
        for key in ("N64C_TAS_SHOT_DIR", "N64C_TAS_SHOT_EVERY"):
            self.assertNotIn(key, seen["port"])
        self.assertNotIn("N64R_TAS_SHOT_DIR", seen["oracle"])
        self.assertEqual(seen["oracle"]["N64R_TAS_SHOT_EVERY"], "25")  # the host's entry-count checkpoints stay
        self.assertEqual(seen["port"]["N64C_TAS_TRACE"], str(run / "port-trace.txt"))
        self.assertFalse((run / "shots").exists())
        self.assertIsNone(result["oracle"]["max_diff"])


class PortEnvTests(unittest.TestCase):
    def test_every_port_process_counts_into_its_own_folder_whatever_the_profile_says(self):
        cfg = {"env": {"GCOV_PREFIX": "/build", "N64C_X": "1"}}
        with mock.patch.dict(os.environ, {"GCOV_PREFIX": "/elsewhere"}):
            env = tas.port_env(cfg, Path("/runs/r1/dumps"))
        self.assertEqual((env["GCOV_PREFIX"], env["N64C_X"]), ("/runs/r1/dumps/gcov", "1"))
        self.assertEqual(env["N64C_CALLS"], "/runs/r1/dumps")  # every replay records the port's calls

    def test_the_state_dump_replay_spawns_the_port_with_that_environment(self):
        from types import SimpleNamespace

        from abu_decomp import tas_statediff

        with tempfile.TemporaryDirectory() as name:
            run = Path(name) / "run"
            cache = Path(name) / "cache"
            run.mkdir()
            cache.mkdir()
            (run / "movie.raw").write_text("1\n2\n")
            (cache / "dump-2.bin").write_bytes(b"x")  # nothing missing: only the port is spawned
            cfg = {"port": "port", "args": [], "env": {"GCOV_PREFIX": "/build"}, "timeout_seconds": 1}
            session = SimpleNamespace(
                cfg=cfg, run=run, pads=1, pak="on", oracle_dir=cache, folder=Path(name), oracle_cfg={}
            )
            seen = []
            popen = lambda argv, env, **_: seen.append(env)  # noqa: E731
            with (
                mock.patch.object(tas_statediff.subprocess, "Popen", popen),
                mock.patch.object(tas, "_wait"),
                mock.patch.dict(os.environ, {"GCOV_PREFIX": "/elsewhere"}),
            ):
                tas_statediff._dump_pass(session, [2], mock.Mock())
        self.assertEqual([env["GCOV_PREFIX"] for env in seen], [str(run / tas_statediff.DUMPS / "gcov")])

    def test_the_state_dump_replay_drops_the_port_rom_copy_and_counters_but_keeps_the_dumps(self):
        from types import SimpleNamespace

        from abu_decomp import tas_statediff

        with tempfile.TemporaryDirectory() as name:
            run, cache = Path(name) / "run", Path(name) / "cache"
            run.mkdir()
            cache.mkdir()
            (run / "movie.raw").write_text("1\n2\n")
            (cache / "dump-2.bin").write_bytes(b"x")
            cfg = {"port": "port", "args": [], "env": {}, "timeout_seconds": 1}
            session = SimpleNamespace(
                cfg=cfg, run=run, pads=1, pak="on", oracle_dir=cache, folder=Path(name), oracle_cfg={}
            )
            dumps = run / tas_statediff.DUMPS

            def popen(argv, env, **_):  # the port leaves its ROM copy, counters and one dump
                (dumps / "user" / "base").mkdir(parents=True)
                (dumps / "user" / "base" / "rom.z64").write_bytes(b"r" * 8)
                (dumps / "gcov" / "home").mkdir(parents=True)
                (dumps / "gcov" / "home" / "a.gcda").write_bytes(b"g")
                (dumps / "state-2.bin").write_bytes(b"d")

            with mock.patch.object(tas_statediff.subprocess, "Popen", popen), mock.patch.object(tas, "_wait"):
                tas_statediff._dump_pass(session, [2], mock.Mock())
            self.assertEqual(
                sorted(p.name for p in dumps.iterdir() if p.name != "output.txt"), ["movie.raw", "state-2.bin"]
            )


class ServeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "game.z64").write_bytes(b"rom")
        (root / "port").write_text("bin")
        (root / "movies").mkdir()
        seat = root / "seat.json"
        table = {"movies": "movies", "runs": "runs", "port": "port", "rom": "game.z64", "args": []}
        seat.write_text(json.dumps({"schema_version": 1, "name": "t", "tas": table}))
        self.server = tas_serve.Server(load_profile(seat))
        self.run = root / "runs" / "plays" / "r1"
        self.run.mkdir(parents=True)
        oracle = {"status": "done", "every": 25, "polls_reached": 50, "outcome": "ok"}
        (self.run / "run.json").write_text(
            json.dumps({"id": "r1", "movie": "m", "status": "done", "outcome": "ok", "started": 1.0, "oracle": oracle})
        )
        write_shot(self.run / "shots" / "port", 25, picture((200, 0, 0)))
        write_shot(self.run / "shots" / "oracle", 25, picture())
        (self.run / "oracle.bmp").write_bytes(tas_oracle.bmp(bytes(W * H * 3)))
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), tas_serve.handler(self.server))
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as reply:
                return reply.status, reply.headers.get("Content-Type"), reply.read()
        except urllib.error.HTTPError as error:
            return error.code, None, b""

    def test_state_oracle_picture_shots_and_scores(self):
        row = self.server.state()["runs"][0]["oracle"]
        self.assertEqual(
            (row["frame"], row["status"], row["max_diff"], row["max_poll"]), ("/oracle/r1", "done", 1.0, 25)
        )
        self.assertEqual(
            self.get("/oracle/r1")[1:], ("image/png", tas_serve.picture(tas_oracle.bmp(bytes(W * H * 3)))[0])
        )
        status, kind, body = self.get("/shot/r1?side=port&poll=25")
        self.assertEqual((status, kind), (200, "image/png"))
        self.assertEqual(body, tas_serve.picture((self.run / "shots/port/poll-000025.ppm").read_bytes())[0])
        self.assertEqual(self.get("/shot/r1?side=oracle&poll=50")[0], 404)
        self.assertEqual(self.get("/shot/r1?side=other&poll=25")[0], 404)
        diff = json.loads(self.get("/diff/r1")[2])
        self.assertEqual(diff, {"every": 25, "rows": [{"poll": 25, "diff": 1.0, "drift": 0, "vi_drift": None}]})
        (self.run / "run.json").write_text(json.dumps({"id": "r1", "movie": "m", "status": "done", "started": 1.0}))
        self.assertEqual(self.get("/diff/r1")[0], 404)  # a run without the oracle


if __name__ == "__main__":
    unittest.main()
