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
        with mock.patch.object(proof, "command", return_value=["sh", "-c", shell]):
            return proof.run(self.profile, list(args), project=None)

    def test_command_adds_the_admit_jobs_unless_the_caller_gave_some(self):
        tree = Path(self.temp.name)
        self.assertEqual(
            proof.command(self.profile, tree, ["check"])[1:], ["--work", "6", "--", "make", "-j{j}", "check"]
        )
        for given, work in ((["-j2", "check"], "2"), (["--jobs=3", "check"], "3"), (["-j", "4", "check"], "4")):
            expected = ["--work", work, "--", "make", "-j{j}", "check"]
            self.assertEqual(proof.command(self.profile, tree, given)[1:], expected)

    def test_a_passing_build_reports_its_exit_and_command(self):
        result, code, findings = self.build("echo ok", "check")
        self.assertEqual((code, findings, result["exit"]), (0, [], 0))

    def test_the_build_holds_the_trees_lock_exclusively_and_marks_its_child(self):
        _, code, _ = self.build('test -n "$UNBAKE_TREE_LOCK"', "check")
        self.assertEqual(code, 0)  # the child ran inside the hold (a ship's write window cannot overlap the build)
        with mock.patch("abu_decomp.proof.treelock.hold", wraps=proof.treelock.hold) as held:
            self.build("true", "check")
        self.assertEqual(held.call_args.kwargs, {"exclusive": True, "who": "make"})

    def test_a_failing_build_reports_its_exit_and_the_last_line(self):
        result, code, findings = self.build("echo boom; exit 2", "check")
        self.assertEqual((code, result["exit"]), (1, 2))
        self.assertEqual((findings[0]["key"], findings[0]["message"]), ("proof.failed", "make exited 2: boom"))

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

    def test_tree_is_the_project_flag_and_make_defaults_to_the_callers_tree(self):
        parsed = cli._tool_call(["--profile", "p.json", "make", "--tree", "t", "check"])
        self.assertEqual((str(parsed.project), parsed.args), ("t", ["check"]))
        lane = Path(self.temp.name) / "lane"
        args = mock.Mock(profile=None, args=["check"], project=None, host=None)
        with (
            mock.patch.object(cli, "_profile", return_value=self.profile),
            mock.patch("abu_decomp.tools.cwd_project", return_value=lane) as picked,
            mock.patch.object(proof, "run", return_value=({}, 0, [])) as ran,
        ):
            cli._run_make(args)
        self.assertEqual(picked.call_args.args[1:], ("make", ["check"], None))
        self.assertEqual(ran.call_args.kwargs["project"], lane)  # not the seat tree
