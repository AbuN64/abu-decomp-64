import json
import os
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

WHEEL = "WHEEL=https://github.com/AbuN64/cake-unbake-64/releases/download/v0.5.11/unbake-0.5.11-py3-none-any.whl\n"


class RelaunchRuleTests(unittest.TestCase):
    def test_successor_name_increments_the_trailing_number_keeping_padding(self):
        self.assertEqual(relaunch.next_name("RW-SOL-X3"), "RW-SOL-X4")
        self.assertEqual(relaunch.next_name("lane-09"), "lane-10")
        self.assertEqual(relaunch.next_name("lane-007"), "lane-008")
        self.assertEqual(relaunch.next_name("lane"), "lane2")

    def test_successor_name_skips_names_a_sibling_lane_already_uses(self):
        self.assertEqual(relaunch.next_name("TOP-7", {"TOP-8", "TOP-9"}), "TOP-10")
        self.assertEqual(relaunch.next_name("lane", {"lane2"}), "lane3")

    def test_taken_names_are_sibling_lane_dirs_and_registry_rows(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "TOP-7").mkdir()
            (root / "TOP-7" / "brief.md").write_text("x")
            (root / "TOP-8").mkdir()
            (root / "TOP-8" / "brief.md").write_text("x")
            (root / "EMPTY-9").mkdir()
            taken = relaunch.taken_names(root / "TOP-7", ["TOP-9"])
            self.assertEqual(relaunch.next_name("TOP-7", taken), "TOP-10")

    def test_context_handoff_with_budget_left_continues_even_without_progress(self):
        action, _ = relaunch.plan("result", 3, "", True, True, False, "")
        self.assertEqual(action, "continue")

    def test_plan_continues_unfinished_results_and_handoffs_within_the_budget(self):
        plan = relaunch.plan
        self.assertEqual(plan("result", 2, "partial: next step X", False, False, False)[0], "continue")
        self.assertEqual(plan("result", 2, "", True, True, False)[0], "continue")
        self.assertEqual(plan("identical", 2, "still 3 words off", False, False, False)[0], "continue")
        self.assertEqual(plan("result", 0, "partial", False, False, False)[0], "blocked")

    def test_prose_without_the_first_line_marker_continues(self):
        plan = relaunch.plan
        for said in ("Status: COMPLETE\nall finished", "RESULT: DONE", "summary\nDONE", "All work is done", "DONEx"):
            self.assertEqual(plan("result", 5, said, False, False, False)[0], "continue", said)

    def test_plan_ends_on_explicit_done_and_ignores_other_closings(self):
        plan = relaunch.plan
        self.assertEqual(plan("result", 5, "DONE\nlanded abc", False, False, False)[0], "end")
        self.assertEqual(plan("result", 5, "\nDONE: landed abc", True, True, False)[0], "end")
        self.assertEqual(plan("identical", 5, "function is identical to base", False, False, False)[0], "end")
        self.assertEqual(plan("result", 5, "UNDONE", False, False, False)[0], "continue")
        self.assertEqual(plan("result", 5, "", False, False, False)[0], "none")
        self.assertEqual(plan("result", 5, "", False, True, False)[0], "none")  # cap requested, no HANDOFF.md
        self.assertEqual(plan("result", 5, "partial", True, True, True)[0], "none")  # cancelled

    def test_progress_keeps_the_budget_and_no_progress_spends_it(self):
        plan = relaunch.plan
        action, why = plan("result", 0, "partial", False, False, False, "RESULT.md reports a commit")
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

    def test_a_changed_export_keeps_the_budget_an_unchanged_or_missing_one_spends_it(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            export = Path(tmp) / "out.patch"
            export.write_text("v1")
            before = relaunch.export_hashes([str(export)])
            start = time.time() - 10
            self.assertEqual(relaunch.progress("partial", [], start, [str(export)], before), "")  # unchanged
            export.write_text("v2")
            self.assertEqual(
                relaunch.progress("partial", [], start, [str(export)], before), "a declared export file changed"
            )
            self.assertEqual(relaunch.progress("partial", [], time.time() + 10, [str(export)], before), "")  # not newer
            export.unlink()
            self.assertEqual(relaunch.progress("partial", [], start, [str(export)], before), "")  # missing
            self.assertEqual(relaunch.progress("partial", [], start), "")  # none declared
            export.write_text("v1")  # created by this link
            self.assertTrue(relaunch.progress("partial", [], start, [str(export)], {}))

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
            self.assertEqual(relaunch.progress("pushed 3a6b1752d99e", [], now), "RESULT.md reports a commit")

    def test_result_md_is_the_canonical_result_file(self):
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
        unbake = Distribution("https://github.com/AbuN64/cake-unbake-64", "a" * 40, version="0.5.11")
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

    def test_a_tree_that_still_names_another_tool_version_is_not_refused(self):
        """A game project pins none of our tools: an old wheel line left in a tree's files is inert text, and the
        installed tool runs on the tree (a tool release never asks a project to move)."""
        envelope = {"ok": True, "result": {}, "findings": [], "stages": [], "invocation": "x"}
        done = subprocess.CompletedProcess([], 0, json.dumps(envelope).encode(), b"")
        old = Distribution("https://github.com/AbuN64/cake-unbake-64", "a" * 40, version="0.5.5")
        with installed(unbake=old), mock.patch("abu_decomp.tools.subprocess.run", return_value=done) as call:
            self.assertTrue(tools.invoke("unbake", self.profile, ["status"], project=self.root / "tree").ok)
            tools.ready(self.profile, self.root / "tree")
        call.assert_called_once()
        self.assertFalse(hasattr(tools, "pin_findings") or hasattr(tools, "pin_file_of"))


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

    def test_launch_never_compares_the_installed_tool_with_a_file_of_the_project(self):
        old = Distribution("https://github.com/AbuN64/cake-unbake-64", "a" * 40, version="0.5.5")
        with installed(unbake=old):
            control.launch(self.profile, self.spec)
        self.assertEqual(len(self.started), 1)

    def test_launch_passes_when_the_pins_agree_and_hands_the_runner_the_profile(self):
        ok = Distribution("https://github.com/AbuN64/cake-unbake-64", "a" * 40, version="0.5.11")
        with installed(unbake=ok):
            control.launch(self.profile, self.spec)
        command = self.started[0]
        self.assertEqual(command[command.index("--profile") + 1], str(self.root / "seat.json"))
