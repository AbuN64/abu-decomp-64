import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from abu_decomp import DecompError, tas
from abu_decomp.config import load_profile

T0 = datetime(2026, 10, 20, 19, 0, tzinfo=UTC)


class CodecTests(unittest.TestCase):
    def test_round_trip_compresses_equal_polls(self):
        polls = [(0, 0, 0)] * 3 + [(0x9000, 0, 0)] * 2 + [(0x0400, 5, -80)]
        text = tas.encode(polls, {"rom-sha1": "ab"})
        self.assertEqual(text, "# tas 1\n# rom-sha1 ab\n- *3\nA+START *2\nDOWN 5,-80\n")
        self.assertEqual(tas.decode(text), ({"tas": "1", "rom-sha1": "ab"}, polls))

    def test_raw_round_trip(self):
        polls = [(0x8000, -1, 2), (0, 0, 0)]
        self.assertEqual(tas.parse_raw("# c\n" + tas.format_raw(polls)), polls)

    def test_bad_input_names_the_line(self):
        for text, where in (
            ("# tas 1\nA+JUMP\n", "unknown button"),
            ("# tas 1\nA 1,2 3\n", "line 2"),
            ("A\n", "# tas 1"),
            ("# tas 1\nA 300,0\n", "line 2"),
        ):
            with self.assertRaises(DecompError) as caught:
                tas.decode(text)
            self.assertIn(where, str(caught.exception))
        with self.assertRaises(DecompError):
            tas.parse_raw("8000 0\n")
        with self.assertRaises(DecompError):
            tas.encode([(0x00C0, 0, 0)], {})  # bits the controller does not have

    def test_classify(self):
        self.assertEqual(tas.classify(0, "n64c-tas: done polls=9\n")["outcome"], "ok")
        self.assertEqual(
            tas.classify(125, "debug-reason: host-fault-00002b88\n"),
            {"outcome": "crash", "subject": "host-fault-00002b88"},
        )
        self.assertEqual(
            tas.classify(125, "debug-reason: func_80001234\n"), {"outcome": "stop", "subject": "func_80001234"}
        )
        self.assertEqual(tas.classify(139, "debug-signal: SIGSEGV\n"), {"outcome": "crash", "subject": "SIGSEGV"})
        self.assertEqual(
            tas.classify(78, "x\nboot-missing: code-pointer-8011fe88\n"),
            {"outcome": "stop", "subject": "code-pointer-8011fe88"},
        )
        self.assertEqual(tas.classify(None, ""), {"outcome": "hang"})
        self.assertEqual(tas.classify(1, "boom"), {"outcome": "exit", "subject": "exit 1"})


def _fake_wait(process, timeout, run=None):
    """tas._wait over a mocked Popen: its wait() stands in for wait4; the CPU reads 1.5 s."""
    try:
        return process.wait(timeout), 1.5
    except subprocess.TimeoutExpired:
        tas.os.killpg(process.pid, 9)
        process.wait()
        return None, 1.5


class MovieTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "game.z64").write_bytes(b"rom")
        (self.root / "port").write_text("")
        seat = self.root / "seat.json"
        seat.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "t",
                    "tas": {
                        "movies": "movies",
                        "runs": "runs",
                        "port": "port",
                        "rom": "game.z64",
                        "args": ["--user-dir", "{user_dir}"],
                        "exit_after": 7,
                        "timeout_seconds": 5,
                    },
                }
            )
        )
        self.profile = load_profile(seat)
        patch = mock.patch.object(tas, "_wait", _fake_wait)
        patch.start()
        self.addCleanup(patch.stop)
        self.live = self.root / "runs" / "live"
        self.live.mkdir(parents=True)

    def test_save_newest_recording_then_list_show_delete(self):
        (self.live / "20261020T180000Z.raw").write_text("0000 0 0\n")
        (self.live / "20261020T181000Z.raw").write_text("0000 0 0\n0000 0 0\n8000 0 0\n")
        saved = tas.save(self.profile, "press-a", source=None, note="press  A", replace=False, now=T0)
        self.assertEqual(saved["polls"], 3)
        self.assertTrue(saved["source"].endswith("181000Z.raw"))
        header, polls = tas.decode((self.root / "movies" / "press-a.tas").read_text())
        self.assertEqual(header["rom-sha1"], hashlib.sha1(b"rom").hexdigest())
        self.assertEqual(header["note"], "press A")
        self.assertEqual(polls, [(0, 0, 0), (0, 0, 0), (0x8000, 0, 0)])
        with self.assertRaises(DecompError):
            tas.save(self.profile, "press-a", source=None, note="", replace=False)
        with self.assertRaises(DecompError):
            tas.save(self.profile, "Press A", source=None, note="", replace=False)
        rows = tas.listing(self.profile)
        self.assertEqual([m["name"] for m in rows["movies"]], ["press-a"])
        self.assertEqual(len(rows["recordings"]), 2)
        self.assertEqual(tas.show(self.profile, "press-a")["polls"], 3)
        tas.delete(self.profile, "press-a")
        self.assertEqual(tas.listing(self.profile)["movies"], [])

    def test_play_refuses_another_rom(self):
        (self.root / "movies").mkdir()
        (self.root / "movies" / "m.tas").write_text("# tas 1\n# rom-sha1 0000\nA\n")
        with self.assertRaises(DecompError) as caught:
            tas.play(self.profile, "m")
        self.assertIn("recorded on ROM", str(caught.exception))

    def test_play_runs_the_port_under_the_bridge_and_reports_the_crash(self):
        (self.root / "movies").mkdir()
        sha = hashlib.sha1(b"rom").hexdigest()
        (self.root / "movies" / "m.tas").write_text(f"# tas 1\n# rom-sha1 {sha}\nA *2\n")
        seen = {}

        class Port:
            def __init__(self, argv, env, stdout, **kwargs):
                seen.update(argv=argv, env=env)
                run = Path(argv[argv.index("--run-dir") + 1])
                self.assert_movie = (run / "movie.raw").read_text()
                Path(env["N64C_TAS_RECORD"]).write_text("8000 0 0\n")
                (run / "failure-stack.txt").write_text("#0 game\n")
                stdout.write("debug-reason: host-fault-00002b88\n")
                stdout.flush()
                self.pid = 1
                self.stdin = io.StringIO()

            def wait(self, timeout=None):
                return 125

        with mock.patch.object(tas.subprocess, "Popen", Port):
            result = tas.play(self.profile, "m", now=T0)
        self.assertEqual(result["outcome"], "crash")
        self.assertEqual(result["subject"], "host-fault-00002b88")
        self.assertEqual((result["polls"], result["polls_reached"]), (2, 1))
        self.assertTrue(result["stacks"][0].endswith("failure-stack.txt"))
        self.assertEqual(seen["env"]["N64C_TAS_EXIT_AFTER"], "7")
        self.assertIn(tas.STOP_FUNCTION, seen["argv"])
        user = seen["argv"][-1]
        self.assertTrue(user.endswith("/user") and Path(user).is_dir())

    def test_play_kills_a_hang(self):
        (self.root / "movies").mkdir()
        sha = hashlib.sha1(b"rom").hexdigest()
        (self.root / "movies" / "m.tas").write_text(f"# tas 1\n# rom-sha1 {sha}\n-\n")

        class Port:
            pid = 4242
            stdin = io.StringIO()

            def __init__(self, *args, **kwargs):
                self.calls = 0

            def wait(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired("port", timeout)
                return -9

        with (
            mock.patch.object(tas.subprocess, "Popen", Port),
            mock.patch.object(tas.os, "killpg") as kill,
        ):
            result = tas.play(self.profile, "m", now=T0)
        kill.assert_called_once()
        self.assertEqual(result["outcome"], "hang")

    def movie(self, text="A *2\n"):
        (self.root / "movies").mkdir(exist_ok=True)
        sha = hashlib.sha1(b"rom").hexdigest()
        (self.root / "movies" / "m.tas").write_text(f"# tas 1\n# rom-sha1 {sha}\n{text}")

    def port(self, hashes, seen):
        """A port stand-in: writes the hash lines it is given and its picture, then ends cleanly."""

        class Port:
            pid = 1
            stdin = io.StringIO()

            def __init__(self, argv, env, stdout, **kwargs):
                seen.append(env)
                run = Path(argv[argv.index("--run-dir") + 1])
                seen.append(json.loads((run / "run.json").read_text()))
                Path(env["N64C_TAS_HASH"]).write_text(hashes)
                Path(env["N64C_VIEW"]).write_bytes(b"BM")
                Path(env["N64C_TAS_RECORD"]).write_text("8000 0 0\n8000 0 0\n")
                stdout.write("n64c-tas: done polls=2\n")

            def wait(self, timeout=None):
                return 0

        return mock.patch.object(tas.subprocess, "Popen", Port)

    def test_every_play_is_visible_hashed_and_turbo_by_default(self):
        self.movie()
        seen = []
        with self.port("1 aa\n2 bb\n", seen):
            result = tas.play(self.profile, "m", now=T0)
        env, during = seen
        run = Path(result["run"])
        self.assertEqual((env["N64C_VIEW"], env["N64C_TAS_HASH"]), (str(run / "view.bmp"), str(run / "hash.txt")))
        self.assertEqual(env["N64C_SPEED"], "0")
        self.assertEqual(env["N64C_AUDIO_OUT"], str(run / "audio.pcm"))  # every speed (the port paces turbo sound)
        self.assertEqual(env["N64C_INPUT"], tas.input_address(run))  # any run can be taken over
        self.assertRegex(env["N64C_INPUT"], r"^@abu-tas-[0-9a-f]{24}$")  # abstract: no socket path length limit
        self.assertEqual((during["status"], during["movie"], during["speed"], during["polls"]), ("running", "m", 0, 2))
        record = tas.read_record(run)
        self.assertEqual((record["status"], record["outcome"], record["polls_reached"]), ("done", "ok", 2))
        self.assertIsNone(result["same_twice"])  # nothing to compare with yet
        self.assertEqual(tas.last_poll(run / "hash.txt"), 2)

    def test_a_lane_runs_its_own_build_under_its_label(self):
        self.movie()
        exe = self.root / "lane-port"
        exe.write_text("lane build")
        exe.chmod(0o755)
        seen = []
        with self.port("1 aa\n2 bb\n", seen):
            result = tas.play(self.profile, "m", now=T0, port=exe, label="TAS-CYCLE-1")
        during = seen[1]
        self.assertEqual((during["port"], during["label"]), (str(exe.resolve()), "TAS-CYCLE-1"))
        self.assertEqual(during["build"], tas.build_id(exe))
        self.assertEqual(tas.read_record(Path(result["run"]))["label"], "TAS-CYCLE-1")
        with self.assertRaises(DecompError):
            tas.play(self.profile, "m", port=self.root / "missing")
        with self.assertRaises(DecompError):
            tas.play(self.profile, "m", label="no spaces")

    def test_a_human_run_and_a_taken_run_are_saved_as_movies(self):
        self.movie()
        seen = []
        with self.port("1 aa\n2 bb\n", seen):
            human = tas.play(self.profile, None, now=T0)
        env, during = seen
        self.assertNotIn("N64C_TAS_PLAY", env)
        self.assertEqual((env["N64C_SPEED"], env["N64C_VIEW_FPS"]), ("1", "60"))
        self.assertEqual(
            (during["input"], during["label"], during["movie"]), ("human", "owner", "play-20261020-190000")
        )
        self.assertEqual(human["saved"], "play-20261020-190000")
        _, polls = tas.decode((self.root / "movies" / "play-20261020-190000.tas").read_text())
        self.assertEqual(polls, [(0x8000, 0, 0)] * 2)
        self.assertIsNone(human["same_twice"])

        def take(argv, env, stdout, **kwargs):  # the page takes the replay over while it runs
            Path(argv[argv.index("--run-dir") + 1], "taken").write_text("page\n")

        with self.port("1 aa\n2 bb\n", []):
            first = tas.play(self.profile, "m", now=T0)
        port = self.port("1 aa\n2 cc\n", [])
        with port as Port:
            init = Port.__init__

            def both(self_, argv, env, stdout, **kwargs):
                init(self_, argv, env, stdout, **kwargs)
                take(argv, env, stdout)

            Port.__init__ = both
            taken = tas.play(self.profile, "m", now=T0)
        self.assertTrue(taken["taken"])
        self.assertTrue(taken["saved"].startswith("m-take-"))
        self.assertIsNone(taken["same_twice"])  # never compared with the movie's replays ...
        with self.port("1 aa\n2 bb\n", []):
            again = tas.play(self.profile, "m", now=T0)
        self.assertEqual(again["compared_with"], Path(first["run"]).name)  # ... and never their reference
        self.assertIs(again["same_twice"], True)

    def test_speed_one_plays_sound_and_waits_for_the_movie_in_real_time(self):
        self.movie("A *600\n")
        seen, waited = [], []

        def wait(process, timeout, run=None):
            waited.append(timeout)
            return process.wait(), 1.0

        with self.port("1 aa\n", seen), mock.patch.object(tas, "_wait", wait):
            result = tas.play(self.profile, "m", speed=1, now=T0)
        self.assertEqual(seen[0]["N64C_AUDIO_OUT"], str(Path(result["run"]) / "audio.pcm"))
        self.assertEqual(seen[0]["N64C_SPEED"], "1")
        self.assertAlmostEqual(waited[0], 5 + (600 + 7) / 60)
        with self.assertRaises(DecompError):
            tas.play(self.profile, "m", speed=-1)

    def test_same_twice_compares_with_the_previous_run_on_the_same_build(self):
        self.movie()
        with self.port("1 aa\n2 bb\n", []):
            first = tas.play(self.profile, "m", now=T0)
            second = tas.play(self.profile, "m", now=T0)
        self.assertNotEqual(first["run"], second["run"])
        self.assertTrue(Path(second["run"]).name.endswith("-m-2"))
        self.assertIs(second["same_twice"], True)
        self.assertEqual(second["compared_with"], Path(first["run"]).name)
        with self.port("1 aa\n2 cc\n", []):
            third = tas.play(self.profile, "m", now=T0)
        self.assertIs(third["same_twice"], False)
        (self.root / "port").write_text("rebuilt")  # another build: nothing to compare with
        with self.port("1 aa\n2 cc\n", []):
            fourth = tas.play(self.profile, "m", now=T0)
        self.assertIsNone(fourth["same_twice"])
        with self.port("1 aa\n2 cc\n", []):
            twice = tas.play_twice(self.profile, "m")
        self.assertIs(twice["same_twice"], True)

    def test_a_run_whose_process_vanished_reads_as_done_without_outcome(self):
        run = self.root / "runs" / "plays" / "20261021T000000Z-m"
        run.mkdir(parents=True)
        (run / "run.json").write_text(json.dumps({"id": run.name, "status": "running", "pid": 2**22 + 12345}))
        ((_, row),) = tas.run_rows(tas.settings(self.profile))
        self.assertEqual((row["status"], row["outcome"]), ("done", None))

    def test_fn_of_names_the_function_an_outcome_points_at(self):
        self.assertEqual(tas.fn_of({"subject": "func_80256C90_de+0x10"}), "func_80256C90_de")
        self.assertEqual(tas.fn_of({"subject": "func_8041CAD8_de"}), "func_8041CAD8_de")
        self.assertEqual(
            tas.fn_of({"subject": "host-fault-1", "owner": "func_80209CD8_de at src/a.CPP:3"}), "func_80209CD8_de"
        )
        self.assertIsNone(tas.fn_of({"subject": "exit 3"}))

    def test_table_has_a_same_twice_column(self):
        row = {"movie": "m", "outcome": "ok", "subject": "", "polls_reached": 2, "polls": 2, "vs_recorded": "same"}
        text = tas.table({"movies": [{**row, "same_twice": True, "next": ""}]})
        self.assertIn("same_twice", text)
        self.assertIn(" yes ", text)


class StallTests(unittest.TestCase):
    def test_a_taken_run_is_a_hang_only_after_its_polls_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "taken").write_text("page\n")
            clock = [0.0]

            def monotonic():
                clock[0] += 1.0
                return clock[0]

            def wait4(pid, flags):
                if flags == 0:  # the kill's reap
                    return pid, 9, mock.Mock(ru_utime=1.0, ru_stime=0.0)
                return 0, 0, None

            process = mock.Mock(pid=4242)
            with (
                mock.patch.object(tas.os, "wait4", wait4),
                mock.patch.object(tas.time, "sleep"),
                mock.patch.object(tas.time, "monotonic", monotonic),
                mock.patch.object(tas.os, "killpg") as kill,
                mock.patch.object(tas, "last_poll", lambda path: int(min(clock[0], 100))),  # polls stop at t=100
                mock.patch.object(tas.os, "waitstatus_to_exitcode", return_value=-9),
            ):
                code, _ = tas._wait(process, timeout=5.0, run=run)
            self.assertIsNone(code)
            kill.assert_called_once()
            self.assertGreater(clock[0], 100 + tas.STALL_SECONDS - 2)  # not at the 5 s timeout: after the stall


if __name__ == "__main__":
    unittest.main()
