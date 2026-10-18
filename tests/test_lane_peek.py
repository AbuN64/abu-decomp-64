import contextlib
import io
import json
import os
import subprocess
import sys
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

    def test_a_silent_log_with_no_holder_and_no_marker_is_dead_with_its_refusal(self):
        self.write("slow: a stage ran long", "Refused: unbake setup needs a host", "exiting")
        os.utime(self.log, (NOW - 3600, NOW - 3600))
        with mock.patch.object(peek, "process_rows", return_value=[]), mock.patch.object(peek, "_fds", return_value=[]):
            text = printed(peek.job, "J", str(self.log), NOW, {})
        self.assertEqual(
            text,
            "== DEAD J: log idle 60 min, no process holds it, no success marker; "
            "last: Refused: unbake setup needs a host\n",
        )

    def test_a_held_or_recent_log_is_not_dead(self):
        self.write("match started (last run 60.0s)")
        os.utime(self.log, (NOW - 3600, NOW - 3600))
        rows = [["9", "1", "5", "0", "-", f"bash run.sh {self.log}"]]
        with (
            mock.patch.object(peek, "process_rows", return_value=rows),
            mock.patch.object(peek, "_fds", return_value=[]),
        ):
            self.assertIn("== JOB J: match", printed(peek.job, "J", str(self.log), NOW, {}))
        os.utime(self.log, (NOW - 30, NOW - 30))
        with mock.patch.object(peek, "process_rows", return_value=[]), mock.patch.object(peek, "_fds", return_value=[]):
            self.assertIn("== JOB J: match", printed(peek.job, "J", str(self.log), NOW, {}))

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


def claude_line(at, content, **extra):
    return json.dumps({"timestamp": at, "message": {"role": "assistant", "content": content, **extra}})


def tool(command):
    return [{"type": "tool_use", "name": "Bash", "input": {"command": command}}]


class StaleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def task(self, *lines):
        path = self.root / "task.output"
        path.write_text("\n".join(lines) + "\n")
        return path

    def test_old_last_action_is_flagged_with_the_command_in_flight(self):
        path = self.task(
            claude_line(NOW - 3600, tool("echo a")), claude_line(NOW - 1800, tool("python3 -m unittest discover"))
        )
        text = printed(peek.activity, 5.0, [f"L={path}"], NOW)
        self.assertIn("== STALE L: no new action for 30 min, in flight: python3 -m unittest discover\n", text)

    def test_recent_action_finished_agent_and_stopped_unit_are_not_flagged(self):
        path = self.task(claude_line(NOW - 60, tool("echo a")))
        self.assertNotIn("STALE", printed(peek.activity, 5.0, [f"L={path}"], NOW))
        old = self.task(claude_line(NOW - 3600, [{"type": "text", "text": "done"}], stop_reason="end_turn"))
        self.assertNotIn("STALE", printed(peek.activity, 5.0, [f"L={old}"], NOW))
        old = self.task(claude_line(NOW - 3600, tool("x")))
        self.assertNotIn("STALE", printed(peek.activity, 5.0, [f"L={old}"], NOW, peek.Thresholds(), {"L": False}))

    def test_threshold_comes_from_the_thresholds(self):
        path = self.task(claude_line(NOW - 3600, tool("x")))
        self.assertNotIn("STALE", printed(peek.activity, 5.0, [f"L={path}"], NOW, peek.Thresholds(stale_minutes=90)))

    def test_lines_without_a_timestamp_take_the_earlier_one(self):
        """Streamed content blocks of one message land on several lines, some without a timestamp (printed as ?)."""
        bare = json.dumps({"message": {"role": "assistant", "content": tool("echo bare")}})
        path = self.task(claude_line(NOW - 100, tool("echo a")), bare)
        text = printed(peek.activity, 5.0, [f"L={path}"], NOW)
        self.assertNotIn("  ?  ", text)
        self.assertEqual(
            [t for t, _, _ in peek.claude([claude_line(NOW - 100, tool("a")), bare])], [NOW - 100, NOW - 100]
        )

    def test_a_transcript_without_stamps_uses_the_file_time(self):
        path = self.task(json.dumps({"message": {"role": "assistant", "content": tool("x")}}))
        os.utime(path, (NOW - 7200, NOW - 7200))
        self.assertIn("STALE L: no new action for 120 min", printed(peek.activity, 5.0, [f"L={path}"], NOW))


class OwnedIdleTests(unittest.TestCase):
    PS = (
        "10 1 3000 5 ep_poll python3 -m unittest discover\n"
        "11 10 3000 1 ep_poll python3 worker\n"
        "12 1 3000 2500 - python3 /own/busy.py\n"
        "13 1 100 0 - python3 /own/young.py\n"
        "14 1 3000 0 - python3 /own/dir/tool.py\n"
        "15 1 3000 0 - unrelated\n"
    )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = Path(self.temp.name)
        for pid, cwd in {10: "/own/repo", 11: "/own/repo", 12: "/own/repo", 13: "/own/repo", 15: "/else"}.items():
            (self.proc / str(pid)).mkdir()
            os.symlink(cwd, self.proc / str(pid) / "cwd")

    def test_cwd_or_argv_under_the_owned_dirs_flags_starved_top_level_processes(self):
        rows = [r.split(None, 5) for r in self.PS.splitlines()]
        text = printed(peek.owned_idle, "L", ("/own",), "local", rows, peek.Thresholds(), str(self.proc))
        self.assertEqual(
            text.splitlines(),
            [
                "== IDLE L pid 10 on local: 50 min wall, 5s CPU (0%), wchan ep_poll: python3 -m unittest discover",
                "== IDLE L pid 14 on local: 50 min wall, 0s CPU (0%), wchan -: python3 /own/dir/tool.py",
            ],
        )

    def test_the_lane_supervisor_and_its_agent_wait_by_design_but_their_commands_are_flagged(self):
        rows = [
            ["30", "1", "3000", "0", "poll", "python3 runner.py --cwd /own --name L --owns /own"],
            ["31", "30", "3000", "0", "ep_poll", "codex app-server /own"],
            ["32", "31", "3000", "0", "ep_poll", "python3 -m unittest /own"],
        ]
        text = printed(peek.owned_idle, "L", ("/own",), "rig", rows, peek.Thresholds(), "/nope")
        self.assertEqual([line.split(":")[0] for line in text.splitlines()], ["== IDLE L pid 32 on rig"])

    def test_owned_dir_prefix_is_a_path_boundary_and_the_peek_is_never_flagged(self):
        rows = [[str(os.getpid()), "1", "3000", "0", "-", "python3 /own/x"], ["20", "1", "3000", "0", "-", "/owned/x"]]
        self.assertEqual(printed(peek.owned_idle, "L", ("/own",), "rig", rows, peek.Thresholds(), "/nope"), "")

    def test_run_checks_each_lane_on_local_and_rig_hosts_from_registry_facts(self):
        path = self.proc / "t.jsonl"
        path.write_text("")
        calls = []

        def ps(host):
            calls.append(host)
            return [["10", "1", "3000", "0", "-", "python3 /own/x"]]

        facts = {"L": peek.LaneFacts(("/own",)), "OFF": peek.LaneFacts(("/own",), running=False)}
        with mock.patch.object(peek, "process_rows", ps):
            text = printed(
                peek.run, 5.0, [f"L={path}", f"OFF={path}"], [], ["h unbake"], None, peek.Thresholds(), facts
            )
        self.assertEqual(calls, ["h", "local", "h"])
        self.assertIn("== IDLE L pid 10 on local:", text)
        self.assertIn("== IDLE L pid 10 on h:", text)
        self.assertNotIn("IDLE OFF", text)

    def test_owns_fall_back_to_launch_json_beside_the_transcript(self):
        (self.proc / "LAUNCH.json").write_text(json.dumps({"owns": ["/own"]}))
        self.assertEqual(peek.launch_owns(self.proc / "transcript.jsonl"), ("/own",))
        self.assertEqual(peek.launch_owns(self.proc / "nowhere" / "transcript.jsonl"), ())


class RunnerTimeoutTests(unittest.TestCase):
    def test_a_hung_test_fails_fast_with_a_stack_dump(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "test_hang.py").write_text(
                "import select, unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_hang(self):\n"
                "        select.select([], [], [], 60)\n"
            )
            runner = Path(__file__).resolve().parents[1] / "ci" / "run_tests.py"
            env = {**os.environ, "ABU_TEST_TIMEOUT": "1"}
            called = subprocess.run(
                [sys.executable, str(runner), directory], capture_output=True, text=True, env=env, timeout=30
            )
        self.assertEqual(called.returncode, 1)
        self.assertIn("test exceeded 1s", called.stderr)
        self.assertIn("test_hang", called.stderr)


if __name__ == "__main__":
    unittest.main()
