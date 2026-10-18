import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_lane_control import LADDER

from abu_decomp.cli import main
from abu_decomp.lanes import continuation, control, registry

RULES = "LANE RULES x\n- keep output small\n"


def run(argv):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = main(argv)
    return code, json.loads(output.getvalue())


class BriefTests(unittest.TestCase):
    def test_continuation_drops_rules_and_older_continuations_and_points_at_the_old_handoff(self):
        old = "TASK do x\n\n## Continuation\n\nold pointer\n\n## Next\nkeep me\n\n" + RULES
        brief = continuation.continuation_brief(old, RULES, "A-1", Path("/l/A-1"), Path("/l/A-2"))
        self.assertTrue(brief.startswith("TASK do x\n\n## Next\nkeep me\n\n## Continuation\n\n"))
        self.assertNotIn("old pointer", brief)
        self.assertIn("read ../A-1/HANDOFF.md and ../A-1/STATUS.md", brief)
        self.assertEqual((brief.count("## Continuation"), brief.count("LANE RULES")), (1, 1))
        self.assertTrue(brief.endswith(RULES))
        again = continuation.continuation_brief(brief, RULES, "A-2", Path("/l/A-2"), Path("/l/A-3"))
        self.assertEqual((again.count("## Continuation"), again.count("LANE RULES")), (1, 1))
        self.assertIn("../A-2/HANDOFF.md", again)
        self.assertNotIn("../A-1/", again)

    def test_unit_spec_reads_systemd_quoted_exec_start(self):
        text = (
            "[Service]\nExecStart=\n"
            'ExecStart="/usr/bin/python3" "/x/lane.py" "--model" "gpt-6-astra" "--effort" "high" "--wall" "5400" '
            '"--continues" "5" "--done-when" "result" "--fanout" "disabled" "--max-agents" "0" "--dir" "/l/E-12" '
            '"--cwd" "/l/E-12" "--name" "engine-astra-12" "--kind" "tooling" '
            '"--owner-override" "owner 10-18: engine lane stays on Astra"\n'
        )
        spec = continuation.unit_spec(text)
        self.assertEqual(spec["owner_override"], "owner 10-18: engine lane stays on Astra")
        self.assertEqual((spec["wall"], spec["continues"], spec["max_agents"], spec["kind"]), (5400, 5, 0, "tooling"))


class ContinueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "models.toml").write_text(LADDER.replace("[models]\n", '[models]\n"gpt-6-astra" = "astra"\n'))
        (self.root / "rules.md").write_text(RULES)
        self.active = self.root / "lanes.active"
        self.active.write_text("# NAME=PATH\nOTHER=/x/transcript.jsonl\n")
        lanes = {"registry": "lanes.json", "ladder": "models.toml", "codex": "codex", "socket_dir": "sockets",
                 "rules": "rules.md", "active_file": "lanes.active"}  # fmt: skip
        (self.root / "seat.json").write_text(json.dumps({"schema_version": 1, "name": "w", "lanes": lanes}))
        self.seat = ["--profile", str(self.root / "seat.json")]
        self.units: set[str] = set()
        self.unit_files: dict[str, str] = {}
        self.started: list[list[str]] = []
        for patch in (
            mock.patch.object(registry, "active_units", lambda: set(self.units)),
            mock.patch.object(control, "start_unit", lambda command, directory: self.started.append(command)),
            mock.patch.object(continuation, "unit_text", lambda unit: self.unit_files.get(unit)),
            mock.patch.object(continuation, "known_units", lambda: list(self.unit_files)),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def lane(self, name, brief="TASK\n"):
        directory = self.root / name
        directory.mkdir()
        (directory / "brief.md").write_text(brief)
        return directory

    def flag(self, command, name):
        return command[command.index(name) + 1]

    def launch_a(self):
        directory = self.lane("A-1")
        code, envelope = run([
            *self.seat, "lane", "launch", "--name", "A-1", "--kind", "tooling", "--model", "sonnet",
            "--effort", "high", "--track", "official", "--owns", "/work/a", "--dir", str(directory),
            "--cwd", str(directory), "--wall", "3600", "--continues", "4", "--done-when", "result",
            "--fanout", "disabled", "--max-agents", "0",
        ])  # fmt: skip
        self.assertEqual(code, 0, envelope)
        self.units.add("lane-A-1")
        return directory

    def test_refused_while_the_old_unit_is_active(self):
        old = self.launch_a()
        code, envelope = run([*self.seat, "lane", "continue", "--from", str(old), "--name", "A-2"])
        self.assertEqual((code, envelope["findings"][0]["key"]), (1, "lanes.live"))
        self.assertFalse((self.root / "A-2").exists())
        self.assertEqual(len(self.started), 1)

    def test_inherits_the_registry_row_and_replaces_it_and_the_peek_line(self):
        old = self.launch_a()
        self.units.clear()
        code, envelope = run([*self.seat, "lane", "continue", "--from", str(old), "--name", "A-2"])
        self.assertEqual(code, 0, envelope)
        command = self.started[-1]
        for flag, value in (("--model", "sonnet"), ("--effort", "high"), ("--wall", "3600"), ("--continues", "4"),
                            ("--kind", "tooling"), ("--name", "A-2"), ("--dir", str(self.root / "A-2")),
                            ("--cwd", str(self.root / "A-2"))):  # fmt: skip
            self.assertEqual(self.flag(command, flag), value, flag)
        rows = json.loads((self.root / "lanes.json").read_text())["lanes"]
        self.assertEqual([(row["name"], row["state"]) for row in rows], [("A-1", "replaced"), ("A-2", "live")])
        self.assertEqual(
            (rows[1]["owns"], rows[1]["track"], rows[1]["continued_from"]), (["/work/a"], "official", "A-1")
        )
        self.assertEqual(rows[0]["reason"], "continued by A-2")
        brief = (self.root / "A-2/brief.md").read_text()
        self.assertIn("../A-1/HANDOFF.md", brief)
        self.assertEqual(brief.count("LANE RULES"), 1)
        self.assertEqual(
            self.active.read_text().splitlines(),
            ["# NAME=PATH", "OTHER=/x/transcript.jsonl", f"A-2={self.root / 'A-2/transcript.jsonl'}"],
        )

    def test_old_launched_lane_reads_its_unit_and_needs_owns_and_track(self):
        old = self.lane("E-12", "TASK e\n\n" + RULES)
        self.active.write_text(f"E-12={old / 'transcript.jsonl'}\n")
        self.unit_files["lane-e-12"] = (
            f'ExecStart="/usr/bin/python3" "/x/lane.py" "--model" "gpt-6-astra" "--effort" "high" "--wall" "5400" '
            f'"--continues" "5" "--done-when" "result" "--fanout" "disabled" "--max-agents" "0" "--dir" "{old}" '
            f'"--cwd" "{old}" "--name" "e-12" "--kind" "tooling" "--owner-override" "owner: stays on Astra"\n'
        )
        code, envelope = run([*self.seat, "lane", "continue", "--from", str(old), "--name", "E-13"])
        self.assertEqual(code, 1)
        self.assertIn("--owns, --track", envelope["findings"][0]["message"])
        self.units.add("lane-e-12")
        code, envelope = run([*self.seat, "lane", "continue", "--from", str(old), "--name", "E-13",
                              "--owns", "/work/e", "--track", "official"])  # fmt: skip
        self.assertEqual(envelope["findings"][0]["key"], "lanes.live")
        self.units.clear()
        code, envelope = run([*self.seat, "lane", "continue", "--from", str(old), "--name", "E-13",
                              "--owns", "/work/e", "--track", "official"])  # fmt: skip
        self.assertEqual(code, 0, envelope)
        command = self.started[-1]
        self.assertEqual((self.flag(command, "--model"), self.flag(command, "--continues")), ("gpt-6-astra", "5"))
        self.assertEqual(self.flag(command, "--owner-override"), "owner: stays on Astra")
        self.assertEqual(envelope["result"]["continued_from"]["unit"], "lane-e-12")
        self.assertEqual(self.active.read_text().splitlines(), [f"E-13={self.root / 'E-13/transcript.jsonl'}"])

    def test_registry_row_without_the_full_spec_is_filled_from_its_unit(self):
        old = self.launch_a()
        self.units.clear()
        document = json.loads((self.root / "lanes.json").read_text())
        for key in ("effort", "wall", "continues"):
            del document["lanes"][0][key]
        (self.root / "lanes.json").write_text(json.dumps(document))
        self.unit_files["lane-A-1"] = f'"--effort" "low" "--wall" "99" "--continues" "1" "--dir" "{old}"'
        self.unit_files["lane-A-1"] = "ExecStart=" + self.unit_files["lane-A-1"]
        code, envelope = run([*self.seat, "lane", "continue", "--from", str(old), "--name", "A-2"])
        self.assertEqual(code, 0, envelope)
        self.assertEqual(
            (self.flag(self.started[-1], "--effort"), self.flag(self.started[-1], "--wall")), ("low", "99")
        )

    def test_a_nonempty_new_directory_is_refused(self):
        old = self.launch_a()
        self.units.clear()
        self.lane("A-2")
        code, envelope = run([*self.seat, "lane", "continue", "--from", str(old), "--name", "A-2"])
        self.assertEqual((code, envelope["findings"][0]["key"]), (1, "lanes.arguments"))

    def launch_json(self, directory, **values):
        record = {"schema": 1, "argv": ["--model", "sonnet"], "model": "sonnet", "effort": "medium", "kind": "tooling",
                  "wall": "1800", "continues": 3, "done_when": "result", "fanout": "disabled", "max_agents": 0,
                  "cwd": str(directory), "dir": str(directory), "socket": "/s.sock", "name": directory.name.lower(),
                  "owns": ["/work/l"], "track": "official", "owner_override": None, "resume_thread": None}  # fmt: skip
        record.update(values)
        (directory / "LAUNCH.json").write_text(json.dumps(record))

    def test_launch_json_alone_carries_every_setting_after_a_collected_unit(self):
        old = self.lane("L-1")
        self.launch_json(old)
        code, envelope = run([*self.seat, "lane", "continue", "--from", str(old), "--name", "L-2"])
        self.assertEqual(code, 0, envelope)
        command = self.started[-1]
        for flag, value in (("--model", "sonnet"), ("--effort", "medium"), ("--wall", "1800"), ("--continues", "3"),
                            ("--owns", "/work/l"), ("--track", "official"), ("--kind", "tooling")):  # fmt: skip
            self.assertEqual(self.flag(command, flag), value, flag)
        self.assertEqual(envelope["result"]["continued_from"]["sources"], ["LAUNCH.json"])
        self.assertEqual(envelope["result"]["continued_from"]["unit"], "lane-l-1")

    def test_launch_json_unit_still_active_is_refused(self):
        old = self.lane("L-1")
        self.launch_json(old)
        self.units.add("lane-l-1")
        code, envelope = run([*self.seat, "lane", "continue", "--from", str(old), "--name", "L-2"])
        self.assertEqual((code, envelope["findings"][0]["key"]), (1, "lanes.live"))

    def test_sources_are_read_registry_then_launch_json_then_exec_start(self):
        old = self.launch_a()
        self.units.clear()
        document = json.loads((self.root / "lanes.json").read_text())
        del document["lanes"][0]["wall"], document["lanes"][0]["continues"]
        (self.root / "lanes.json").write_text(json.dumps(document))
        self.launch_json(old, effort="low", wall="1200", continues=None)
        self.unit_files["lane-A-1"] = f'ExecStart="--continues" "7" "--wall" "99" "--dir" "{old}"'
        code, envelope = run([*self.seat, "lane", "continue", "--from", str(old), "--name", "A-2"])
        self.assertEqual(code, 0, envelope)
        command = self.started[-1]
        # effort from the row, wall from LAUNCH.json, continues only from the unit
        self.assertEqual([self.flag(command, f) for f in ("--effort", "--wall", "--continues")], ["high", "1200", "7"])
        self.assertEqual(envelope["result"]["continued_from"]["sources"], ["registry", "LAUNCH.json", "ExecStart"])

    def test_a_legacy_lane_with_no_record_needs_every_setting_as_a_flag(self):
        old = self.lane("OLD-9")
        code, envelope = run([*self.seat, "lane", "continue", "--from", str(old), "--name", "OLD-10"])
        self.assertEqual((code, envelope["findings"][0]["key"]), (1, "lanes.unknown"))
        partial = ["--model", "sonnet", "--owns", "/work/o", "--track", "official"]
        code, envelope = run([*self.seat, "lane", "continue", "--from", str(old), "--name", "OLD-10", *partial])
        self.assertEqual(envelope["findings"][0]["key"], "lanes.inherit")
        for flag in ("--kind", "--effort", "--wall", "--continues", "--done-when", "--fanout", "--max-agents"):
            self.assertIn(flag, envelope["findings"][0]["message"])
        full = [*partial, "--kind", "tooling", "--effort", "high", "--wall", "600", "--continues", "2",
                "--done-when", "result", "--fanout", "disabled", "--max-agents", "0"]  # fmt: skip
        code, envelope = run([*self.seat, "lane", "continue", "--from", str(old), "--name", "OLD-10", *full])
        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["result"]["continued_from"]["sources"], [])


class RunnerLaunchRecordTests(unittest.TestCase):
    def test_runner_writes_launch_json_with_every_argument_before_it_starts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "models.toml").write_text(LADDER)
            for name in ("codex", "brief.md", "socket"):
                (root / name).write_text("")  # an existing socket path stops the runner right after the record
            lane = root / "lane"
            argv = ["--codex", str(root / "codex"), "--model", "sonnet", "--effort", "high", "--cwd", str(root),
                    "--brief", str(root / "brief.md"), "--dir", str(lane), "--socket", str(root / "socket"),
                    "--name", "r-1", "--wall", "900", "--fanout", "disabled", "--max-agents", "0", "--continues", "2",
                    "--done-when", "result", "--kind", "tooling", "--ladder", str(root / "models.toml"),
                    "--owns", "/work/a", "--owns", "/work/b", "--track", "experiment"]  # fmt: skip
            runner = Path(continuation.__file__).with_name("runner.py")
            called = subprocess.run([sys.executable, str(runner), *argv], capture_output=True, text=True, timeout=60)
            self.assertIn("SocketInUse", called.stderr)
            record = json.loads((lane / "LAUNCH.json").read_text())
            self.assertEqual(record["argv"], argv)
            self.assertEqual((record["owns"], record["track"], record["wall"], record["max_agents"]),
                             (["/work/a", "/work/b"], "experiment", "900", 0))  # fmt: skip
            spec = continuation.launch_record(lane)
            self.assertEqual((spec["wall"], spec["continues"], spec["done_when"]), (900, 2, "result"))


if __name__ == "__main__":
    unittest.main()
