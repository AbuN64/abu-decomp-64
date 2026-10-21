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

        with mock.patch.object(tas.subprocess, "Popen", Port), mock.patch.object(tas.os, "killpg") as kill:
            result = tas.play(self.profile, "m", now=T0)
        kill.assert_called_once()
        self.assertEqual(result["outcome"], "hang")


if __name__ == "__main__":
    unittest.main()
