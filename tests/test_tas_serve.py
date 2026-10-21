import json
import tempfile
import unittest
from pathlib import Path

from abu_decomp import DecompError, tas_serve, tas_tabs
from abu_decomp.config import load_profile


class ServeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "game.z64").write_bytes(b"rom")
        self.seat = self.root / "seat.json"

    def profile(self, **tas):
        table = {
            "movies": "movies",
            "runs": "runs",
            "port": "port",
            "rom": "game.z64",
            "args": ["--window", "640x480"],
            **tas,
        }
        self.seat.write_text(json.dumps({"schema_version": 1, "name": "t", "tas": table}))
        return load_profile(self.seat)

    def test_keysym_maps_only_bound_keys(self):
        self.assertEqual(tas_serve.keysym("ArrowDown"), "Down")
        self.assertEqual(tas_serve.keysym("Enter"), "Return")
        self.assertEqual(tas_serve.keysym("X"), "x")
        self.assertIsNone(tas_serve.keysym("F5"))
        self.assertIsNone(tas_serve.keysym("1"))

    def test_session_reads_serve_settings_and_window(self):
        session = tas_serve.Session(self.profile(serve={"listen": "0.0.0.0:9000", "display": ":5"}))
        self.assertEqual((session.listen, session.display, session.size), ("0.0.0.0:9000", ":5", "640x480"))
        with self.assertRaises(DecompError):
            tas_serve.Session(self.profile(serve={"port": 1}))

    def test_window_is_required_and_save_needs_a_run(self):
        with self.assertRaises(DecompError):
            tas_serve.window_size(["--rom", "x"])
        session = tas_serve.Session(self.profile())
        with self.assertRaises(DecompError):
            session.save("m", "")
        state = session.state()
        self.assertEqual((state["running"], state["polls"], state["movies"]), (False, 0, []))

    def test_held_stop_reports_stopped_not_running(self):
        session = tas_serve.Session(self.profile())

        class Alive:
            returncode = None

            def poll(self):
                return None

        session.port, session.log = Alive(), self.root / "run.log"
        session.log.write_text("boot\ndebug-reason: n64c_boot_stopped\n")
        state = session.state()
        self.assertEqual((state["running"], state["stopped"]["outcome"]), (False, "stop"))
        session.log.write_text("boot\n")
        self.assertTrue(session.state()["running"])

    def test_tabs(self):
        ai = self.root / "ai"
        ai.mkdir()
        rows = [
            {
                "type": "assistant",
                "timestamp": "2026-10-21T01:02:03Z",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "x"},
                        {"type": "text", "text": "hello  world"},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "ls", "description": "list"}},
                    ]
                },
            },
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "no"}]}},
        ]
        (ai / "a.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
        self.assertEqual(tas_tabs.ai_lines(ai), ["01:02:03 hello world", "01:02:03 > Bash: list"])
        threads = "Thread 2 (x):\n#0 idle\nThread 1 (y):\n#0 n64c_game_scheduler_main ()\n"
        self.assertEqual(tas_tabs.game_threads(threads), ["Thread 1 (y):", "#0 n64c_game_scheduler_main ()"])
        run = self.root / "run"
        run.mkdir()
        (run / "failure-stack.txt").write_text("S1\n")
        (run / "failure-threads.txt").write_text(threads)
        log = self.root / "p.log"
        log.write_text("L1\n")
        self.assertEqual(tas_tabs.port_lines(log, run, False), ["L1"])
        self.assertEqual(tas_tabs.port_lines(log, run, True)[:3], ["L1", "--- failure-stack.txt", "S1"])
        feed = self.root / "e.jsonl"
        feed.write_text(
            json.dumps({"kind": "LANE", "message": "x"})
            + "\n"
            + json.dumps(
                {
                    "kind": "PROBE",
                    "time": "2026-10-21T02:27:14+00:00",
                    "message": "m",
                    "payload": {"function": "f", "verdict": "same"},
                }
            )
        )
        self.assertEqual(tas_tabs.function_lines(feed), ["02:27:14 PROBE f same"])

    def test_play_runs_the_movie_visibly_and_keeps_recording(self):
        import hashlib
        from unittest import mock

        profile = self.profile()
        (self.root / "movies").mkdir()
        sha = hashlib.sha1(b"rom").hexdigest()
        (self.root / "movies" / "m.tas").write_text(f"# tas 1\n# rom-sha1 {sha}\nA *2\n")
        session = tas_serve.Session(profile)
        seen = {}

        class Port:
            pid = 7

            def __init__(self, argv, env, **kwargs):
                seen.update(env=env)

            def poll(self):
                return None

        with mock.patch.object(tas_serve.subprocess, "Popen", Port), mock.patch.object(tas_serve.threading, "Thread"):
            session.new_run("m")
        played = Path(seen["env"]["N64C_TAS_PLAY"])
        self.assertEqual(played.read_text(), "8000 0 0\n8000 0 0\n")
        self.assertIn("N64C_TAS_RECORD", seen["env"])
        self.assertNotIn("N64C_TAS_EXIT_AFTER", seen["env"])
        self.assertEqual((session.movie, session.movie_length), ("m", 2))


if __name__ == "__main__":
    unittest.main()
