import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp.cli import main
from abu_decomp.lanes import peek

NOW = 1_000_000.0


def printed(function, *args):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        function(*args)
    return output.getvalue()


def process(stdout):
    return subprocess.CompletedProcess([], 0, stdout=stdout)


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / "job.log"

    def write(self, *lines):
        self.log.write_text("\n".join(lines) + "\n")

    def test_local_and_ssh_logs_are_read_by_their_tail(self):
        self.write("a", "b")
        self.assertEqual(peek.read_log(str(self.log)), ["a", "b"])
        with mock.patch("subprocess.run", return_value=process("x\ny\n")) as ran:
            self.assertEqual(peek.read_log("abu@rig:/state/job.log"), ["x", "y"])
        self.assertEqual(ran.call_args.args[0], ["ssh", "abu@rig", "tail", "-c", "65536", "/state/job.log"])

    def test_done_empty_and_stageless_jobs(self):
        self.write("work", "ALL-OK")
        self.assertEqual(printed(peek.job, "J", str(self.log), NOW, {}), "== JOB J: done (ALL-OK)\n")
        self.write("noise")
        self.assertEqual(printed(peek.job, "J", str(self.log), NOW, {}), "== JOB J: no open stage; last: noise\n")
        self.log.write_text("")
        self.assertEqual(printed(peek.job, "J", str(self.log), NOW, {}), "== JOB J: no open stage; last: empty\n")

    def test_unreadable_log_is_named(self):
        text = printed(peek.job, "J", str(Path(self.temp.name) / "missing"), NOW, {})
        self.assertTrue(text.startswith("== JOB J: unreadable ("))

    def test_open_stage_is_remembered_and_flagged_past_five_times_its_last_run(self):
        self.write("match started (last run 60.0s)")
        seen = {}
        self.assertEqual(printed(peek.job, "J", str(self.log), NOW, seen), "== JOB J: match | 0 min (limit 5)\n")
        self.assertEqual(list(seen.values()), [NOW])
        self.assertEqual(printed(peek.job, "J", str(self.log), NOW + 200, seen), "== JOB J: match | 3 min (limit 5)\n")
        text = printed(peek.job, "J", str(self.log), NOW + 400, seen)
        self.assertEqual(text, "== JOB J: match | STUCK? 7 min vs last run 60s\n")
        self.write("match started (last run 600.0s)")
        self.assertIn("(limit 50)", printed(peek.job, "K", str(self.log), NOW, {}))


class IdleTests(unittest.TestCase):
    PS = (
        "10 1 2000 5 futex_wait_queue python3 /bin/unbake match\n"
        "11 10 2000 2 futex_wait_queue python3 /bin/unbake worker\n"
        "12 1 2000 1500 - python3 /bin/unbake busy\n"
        "13 1 100 0 - python3 /bin/unbake young\n"
        "14 1 5000 0 - other\n"
    )

    def test_only_old_starved_top_level_matches_are_flagged(self):
        with mock.patch("subprocess.run", return_value=process(self.PS)) as ran:
            text = printed(peek.idle, r"abu@rig python3? \S*/bin/unbake", NOW)
        self.assertEqual(ran.call_args.args[0][:2], ["ssh", "abu@rig"])
        self.assertEqual(
            text,
            "== IDLE abu@rig pid 10: 33 min wall, 5s CPU (0%), wchan futex_wait_queue: python3 /bin/unbake match\n",
        )

    def test_local_host_reads_this_machine(self):
        with mock.patch("subprocess.run", return_value=process("")) as ran:
            self.assertEqual(printed(peek.idle, "local unbake", NOW), "")
        self.assertEqual(ran.call_args.args[0][0], "ps")


class RegistryTests(unittest.TestCase):
    def test_lines_split_into_lanes_jobs_and_rigs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lanes.active"
            path.write_text("# c\nA=/a/transcript.jsonl\n\njob J=h:/log\nrig h python3? \\S*/bin/unbake \n")
            self.assertEqual(
                peek.read_registry(path), (["A=/a/transcript.jsonl"], ["J=h:/log"], ["h python3? \\S*/bin/unbake"])
            )

    def test_run_writes_the_stage_clock_and_drops_day_old_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            log, state = Path(directory) / "j.log", Path(directory) / "state.json"
            log.write_text("s started (last run 10.0s)\n")
            state.write_text(json.dumps({"old": 0.0}))
            with mock.patch("time.time", return_value=NOW):
                text = printed(peek.run, 5.0, [], [f"J={log}"], [], state)
            self.assertIn("== JOB J: s | 0 min", text)
            self.assertEqual(list(json.loads(state.read_text()).values()), [NOW])

    def test_a_failing_rig_is_named_and_the_lanes_still_print(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "transcript.jsonl"
            transcript.write_text("")
            error = subprocess.TimeoutExpired("ssh", 60)
            with mock.patch("subprocess.run", side_effect=error):
                text = printed(peek.run, 5.0, [f"A={transcript}"], [], ["h unbake"], None)
            self.assertIn("== IDLE h unbake: unreadable", text)
            self.assertIn("== A: 0 actions", text)


class DecompPeekTests(unittest.TestCase):
    def test_lane_peek_reads_the_active_file_like_lanepeek(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "models.toml").write_text(
                'ladder = ["sonnet"]\n[models]\nsonnet = "sonnet"\n[kind.tooling]\nmin = "sonnet"\nmax = "sonnet"\n'
            )
            (root / "rules.md").write_text("x\n")
            line = {"at": 4_000_000_000, "value": {"method": "item/completed", "params": {"item": {"command": "ls x"}}}}
            (root / "A").mkdir()
            (root / "A/transcript.jsonl").write_text(json.dumps(line) + "\n")
            (root / "job.log").write_text("ALL-OK\n")
            (root / "lanes.active").write_text(f"A={root}/A/transcript.jsonl\njob J={root}/job.log\nrig h unbake\n")
            profile = {
                "schema_version": 1,
                "name": "seat",
                "lanes": {
                    "registry": "lanes.json",
                    "ladder": "models.toml",
                    "codex": "codex",
                    "socket_dir": "s",
                    "rules": "rules.md",
                    "active_file": "lanes.active",
                },
            }
            (root / "seat.json").write_text(json.dumps(profile))
            seat = ["--profile", str(root / "seat.json"), "lane", "peek"]

            def peeked(*extra):
                output = io.StringIO()
                with contextlib.redirect_stdout(output), mock.patch("subprocess.run", return_value=process("")):
                    code = main([*seat, *extra])
                return code, json.loads(output.getvalue())

            code, envelope = peeked("--minutes", "999999")
            self.assertEqual(code, 0)
            text = envelope["result"]["text"]
            self.assertLess(text.index("== JOB J: done"), text.index("== A: 1 actions"))
            self.assertEqual(envelope["result"]["lanes"], ["A"])
            self.assertTrue((root / ".peek-stages.json").is_file())
            code, envelope = peeked("--name", "A", "--minutes", "999999")
            self.assertNotIn("JOB", envelope["result"]["text"])
            self.assertEqual(peeked("--name", "ZZ")[1]["findings"][0]["key"], "lanes.unknown")


if __name__ == "__main__":
    unittest.main()
