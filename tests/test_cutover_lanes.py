import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from test_tools import Distribution, installed

from abu_decomp import DecompError, tools
from abu_decomp.config import load_profile
from abu_decomp.lanes import control, registry, relaunch

WHEEL = "WHEEL=https://github.com/AbuN64/cake-unbake-64/releases/download/v0.5.8/unbake-0.5.8-py3-none-any.whl\n"


class RelaunchRuleTests(unittest.TestCase):
    def test_successor_name_increments_the_trailing_number_keeping_padding(self):
        self.assertEqual(relaunch.next_name("RW-SOL-X3"), "RW-SOL-X4")
        self.assertEqual(relaunch.next_name("lane-09"), "lane-10")
        self.assertEqual(relaunch.next_name("lane-007"), "lane-008")
        self.assertEqual(relaunch.next_name("lane"), "lane2")

    def test_plan_continues_unfinished_results_and_handoffs_within_the_budget(self):
        plan = relaunch.plan
        self.assertEqual(plan("result", 2, "partial: next step X", False, False, False)[0], "continue")
        self.assertEqual(plan("result", 2, "", True, True, False)[0], "continue")
        self.assertEqual(plan("identical", 2, "still 3 words off", False, False, False)[0], "continue")
        self.assertEqual(plan("result", 0, "partial", False, False, False)[0], "blocked")

    def test_plan_ends_on_explicit_done_and_ignores_other_closings(self):
        plan = relaunch.plan
        self.assertEqual(plan("result", 5, "DONE\nlanded abc", False, False, False)[0], "end")
        self.assertEqual(plan("result", 5, "RESULT: DONE", True, True, False)[0], "end")
        self.assertEqual(plan("identical", 5, "function is identical to base", False, False, False)[0], "end")
        self.assertEqual(plan("result", 5, "UNDONE", False, False, False)[0], "continue")
        self.assertEqual(plan("result", 5, "", False, False, False)[0], "none")
        self.assertEqual(plan("result", 5, "", False, True, False)[0], "none")  # cap requested, no HANDOFF.md
        self.assertEqual(plan("result", 5, "partial", True, True, True)[0], "none")  # cancelled

    def test_progress_keeps_the_budget_and_no_progress_spends_it(self):
        plan = relaunch.plan
        action, why = plan("result", 0, "partial", False, False, False, "RESULT reports a commit")
        self.assertEqual(action, "continue")
        self.assertIn("progressed", why)
        action, why = plan("result", 2, "partial", False, False, False)
        self.assertEqual(action, "continue")
        self.assertIn("spent one", why)
        action, why = plan("result", 0, "partial", False, False, False)
        self.assertEqual(action, "blocked")
        self.assertIn("budget spent", why)
        self.assertEqual(relaunch.next_budget(3, "x"), 3)
        self.assertEqual(relaunch.next_budget(3, ""), 2)

    def test_progress_evidence_is_a_reported_commit_or_a_new_commit_in_an_owned_tree(self):
        self.assertTrue(relaunch.reported_commit("done: 3a6b1752d99e8b33de36634040bf614e543a5c3d pushed"))
        self.assertTrue(relaunch.reported_commit("x\ncommitted abc1234 on main"))
        self.assertFalse(relaunch.reported_commit("pushed nothing yet"))
        self.assertFalse(relaunch.reported_commit("see 3a6b1752 for context"))
        with tempfile.TemporaryDirectory() as temp:
            tree = Path(temp) / "tree"
            tree.mkdir()
            git = ["git", "-C", str(tree), "-c", "user.name=t", "-c", "user.email=t@t"]
            subprocess.run([*git, "init", "-q"], check=True)
            (tree / "f").write_text("1")
            subprocess.run([*git, "add", "f"], check=True)
            subprocess.run([*git, "commit", "-q", "-m", "m"], check=True)
            now = time.time()
            self.assertEqual(
                relaunch.progress("partial", [str(tree), "/no/such"], now - 60), "an owned git tree gained a commit"
            )
            self.assertEqual(relaunch.progress("partial", [str(tree)], now + 3600), "")
            self.assertEqual(relaunch.progress("partial", [temp], now + 3600), "")
            self.assertEqual(relaunch.progress("pushed 3a6b1752d99e", [], now), "RESULT reports a commit")

    def test_result_md_is_read_when_there_is_no_result_file(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.assertEqual(relaunch.result_text(directory), "")
            (directory / "RESULT.md").write_text("DONE\n")
            self.assertTrue(relaunch.explicit_done("result", relaunch.result_text(directory)))

    def test_spawn_runs_decomp_lane_continue_in_its_own_unit(self):
        done = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch("abu_decomp.lanes.relaunch.subprocess.run", return_value=done) as call:
            unit = relaunch.spawn(Path("/s/seat.json"), Path("/l/A-1"), "A-2", 3)
        command = call.call_args.args[0]
        self.assertEqual(unit, "relaunch-A-2")
        self.assertEqual(command[command.index("lane") :], ["lane", "continue", "--from", "/l/A-1", "--name", "A-2",
                                                            "--continues", "3", "--wait", "600"])  # fmt: skip
        self.assertIn("--unit=relaunch-A-2", command)
        failed = subprocess.CompletedProcess([], 1, "", "no bus")
        with (
            mock.patch("abu_decomp.lanes.relaunch.subprocess.run", return_value=failed),
            self.assertRaises(DecompError),
        ):
            relaunch.spawn(Path("/s/seat.json"), Path("/l/A-1"), "A-2", 3)


class ProjectTargetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("project", "tree/.git", "tree/ci", "home"):
            (self.root / name).mkdir(parents=True)
        (self.root / "host.toml").write_text("")
        (self.root / "tree/host.toml").write_text("")
        (self.root / "tree/ci/run").write_text(WHEEL)
        section = {"project": "project", "host": "host.toml", "timeout_seconds": 5}
        (self.root / "seat.json").write_text(json.dumps({"schema_version": 1, "name": "s", "tools": section}))
        self.profile = load_profile(self.root / "seat.json")

    def test_a_lane_tree_and_host_replace_the_profiles_for_one_call(self):
        envelope = {"ok": True, "result": {}, "findings": [], "stages": [], "invocation": "x"}
        done = subprocess.CompletedProcess([], 0, json.dumps(envelope).encode(), b"")
        unbake = Distribution("https://github.com/AbuN64/cake-unbake-64", "a" * 40, version="0.5.8")
        with installed(unbake=unbake), mock.patch("abu_decomp.tools.subprocess.run", return_value=done) as call:
            result = tools.invoke(
                "unbake", self.profile, ["status"], project=self.root / "tree", host=self.root / "tree/host.toml"
            )
        self.assertEqual(call.call_args.args[0][4:], ["--project", str(self.root / "tree"), "status"])
        self.assertEqual(call.call_args.kwargs["cwd"], self.root / "tree")
        self.assertEqual(call.call_args.kwargs["env"]["UNBAKE_HOST"], str(self.root / "tree/host.toml"))
        self.assertEqual(result.context["project"], str(self.root / "tree"))
        with installed(), self.assertRaises(tools.DependencyError) as caught:
            tools.invoke("taste", self.profile, ["profile"], project=self.root / "tree")
        self.assertEqual(caught.exception.key, "dependency.arguments")

    def test_a_tree_whose_own_pin_differs_from_the_installed_tool_is_refused(self):
        old = Distribution("https://github.com/AbuN64/cake-unbake-64", "a" * 40, version="0.5.5")
        with installed(unbake=old), mock.patch("abu_decomp.tools.subprocess.run") as call:
            with self.assertRaises(tools.DependencyError) as caught:
                tools.invoke("unbake", self.profile, ["status"], project=self.root / "tree")
            self.assertEqual(caught.exception.key, "dependency.project_pin")
            self.assertIn("pins 0.5.8", caught.exception.detail)
            call.assert_not_called()

    def test_pin_file_lookup_stops_at_the_project_root(self):
        (self.root / "ci").mkdir()
        (self.root / "ci/run").write_text(WHEEL)
        (self.root / "tree/sub").mkdir()
        self.assertEqual(tools.pin_file_of(self.profile, self.root / "tree/sub"), self.root / "tree/ci/run")
        (self.root / "nogit").mkdir()
        (self.root / "bare/.git").mkdir(parents=True)
        self.assertIsNone(tools.pin_file_of(self.profile, self.root / "bare"))


class LaunchPinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = self.root = Path(self.temp.name)
        for name in ("tree/.git", "tree/ci", "lane", "project"):
            (root / name).mkdir(parents=True)
        (root / "tree/ci/run").write_text(WHEEL)
        (root / "lane/brief.md").write_text("TASK\n")
        (root / "host.toml").write_text("")
        (root / "rules.md").write_text("LANE RULES\n")
        (root / "models.toml").write_text(
            'ladder = ["sonnet", "opus"]\n[models]\nsonnet = "sonnet"\n'
            '[kind.tooling]\nmin = "sonnet"\nmax = "sonnet"\n[kind.impl]\nmin = "sonnet"\nmax = "sonnet"\n'
        )
        lanes = {
            "registry": "lanes.json",
            "ladder": "models.toml",
            "codex": "codex",
            "socket_dir": "s",
            "rules": "rules.md",
        }
        section = {"project": "project", "host": "host.toml", "timeout_seconds": 5}
        (root / "seat.json").write_text(
            json.dumps({"schema_version": 1, "name": "s", "lanes": lanes, "tools": section})
        )
        self.profile = load_profile(root / "seat.json")
        self.started = []
        for patch in (
            mock.patch.object(registry, "active_units", lambda: set()),
            mock.patch.object(control, "start_unit", lambda command, directory: self.started.append(command)),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.spec = {
            "name": "T1", "kind": "tooling", "model": "sonnet", "effort": "high", "track": "official",
            "owns": [str(root / "lane")], "dir": str(root / "lane"), "cwd": str(root / "tree"), "wall": 60,
            "continues": 2, "done_when": "result", "fanout": "disabled", "max_agents": 0,
        }  # fmt: skip

    def test_launch_refuses_a_tool_that_differs_from_the_project_pin_and_names_the_finding(self):
        old = Distribution("https://github.com/AbuN64/cake-unbake-64", "a" * 40, version="0.5.5")
        with installed(unbake=old), self.assertRaises(DecompError) as caught:
            control.launch(self.profile, self.spec)
        self.assertEqual(caught.exception.key, "lanes.tool_pin")
        self.assertIn("unbake 0.5.5 is installed with decomp but", caught.exception.detail)
        self.assertEqual(self.started, [])
        self.assertFalse((self.root / "lanes.json").exists())

    def test_launch_passes_when_the_pins_agree_and_hands_the_runner_the_profile(self):
        ok = Distribution("https://github.com/AbuN64/cake-unbake-64", "a" * 40, version="0.5.8")
        with installed(unbake=ok):
            control.launch(self.profile, self.spec)
        command = self.started[0]
        self.assertEqual(command[command.index("--profile") + 1], str(self.root / "seat.json"))
