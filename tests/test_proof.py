import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp import cli, proof
from abu_decomp.config import load_profile


class ProofTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "lanes" / "X" / "tree").mkdir(parents=True)
        (root / "host.toml").write_text("")
        seat = {
            "schema_version": 1,
            "name": "seat",
            "tools": {"project": "lanes/X/tree", "host": "host.toml", "timeout_seconds": 5},
            "land": {"check": ["make", "check"], "jobs": ["echo", "6"]},
        }
        (root / "seat.json").write_text(json.dumps(seat))
        self.profile = load_profile(root / "seat.json")

    def build(self, shell, *args):
        events = []

        def record(profile, actor, status, message, kind, payload):
            events.append((actor, status, kind, message, payload))

        with (
            mock.patch.object(proof, "command", return_value=["sh", "-c", shell]),
            mock.patch("abu_decomp.narrate.append", side_effect=record),
        ):
            return proof.run(self.profile, list(args), project=None), events

    def test_command_adds_the_admit_jobs_unless_the_caller_gave_some(self):
        tree = Path(self.temp.name)
        self.assertEqual(proof.command(self.profile, tree, ["check"])[1:], ["make", "-j6", "check"])
        self.assertEqual(proof.command(self.profile, tree, ["-j2", "check"])[1:], ["make", "-j2", "check"])

    def test_a_passing_build_writes_one_match_event_for_the_lane(self):
        (result, code, findings), events = self.build("echo ok", "check")
        self.assertEqual((code, findings, result["lane"]), (0, [], "X"))
        self.assertEqual([(e[0], e[1], e[2]) for e in events], [("X", "passed", "MATCH")])
        self.assertEqual(events[0][4]["op"], "build")

    def test_a_failing_build_writes_run_fail_with_the_tail(self):
        (result, code, findings), events = self.build("echo boom; exit 2", "check")
        self.assertEqual((code, result["exit"]), (1, 2))
        self.assertEqual(findings[0]["key"], "proof.failed")
        self.assertEqual([(e[1], e[2]) for e in events], [("failed", "RUN-FAIL")])
        self.assertEqual(events[0][4]["tail"], ["boom"])

    def test_make_is_a_command_and_its_arguments_stay_untouched(self):
        self.assertIn("make", cli.COMMANDS)
        parsed = cli._tool_call(["--profile", "p.json", "make", "--project", "t", "-j3", "check"])
        self.assertEqual((parsed.command, str(parsed.project), parsed.args), ("make", "t", ["-j3", "check"]))

    def test_a_separator_before_the_owned_flags_changes_nothing(self):
        plain = cli._tool_call(["--profile", "p.json", "unbake", "--project", "t", "types", "--functions", "f"])
        split = cli._tool_call(["--profile", "p.json", "unbake", "--", "--project", "t", "types", "--functions", "f"])
        self.assertEqual((split.command, split.project, split.args), (plain.command, plain.project, plain.args))
        bare = cli._tool_call(["unbake", "--", "sync"])
        self.assertEqual((bare.project, bare.args), (None, ["sync"]))
