"""Hard cutover: completion, continuation and lifecycle read only RESULT.md."""

import ast
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from abu_decomp.lanes import control, registry, relaunch


def runner_functions():
    # The runner is a script; compile its real functions without starting app-server.
    source = ast.parse(Path(find_spec("abu_decomp.lanes.runner").origin).read_text())
    names = {"completion", "handoff_only_result", "is_done", "lifecycle_text", "continue_lane"}
    functions = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in names]
    module = ast.Module(body=functions, type_ignores=[])
    namespace = {
        "os": os,
        "sys": sys,
        "context_cap": SimpleNamespace(requested=False),
        "resumes": 0,
        "record": mock.Mock(),
        "successor_text": lambda: "successor",
    }
    exec(compile(module, find_spec("abu_decomp.lanes.runner").origin, "exec"), namespace)
    return namespace


class ResultContractTests(unittest.TestCase):
    def setUp(self):
        # Resolve only the module spec: importing runner executes the lane CLI.
        self.namespace = runner_functions()
        self.namespace["a"] = SimpleNamespace(dir="/lane", done_when="result")

    def test_completion_reads_canonical_file_and_ignores_legacy_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.namespace["a"].dir = temporary
            self.assertFalse(self.namespace["completion"]())
            (root / "RESULT").write_text("DONE\nlegacy only")
            self.assertFalse(self.namespace["completion"]())
            (root / "RESULT.md").write_text("")
            self.assertFalse(self.namespace["completion"]())
            (root / "RESULT.md").write_text("DONE\ncanonical")
            self.assertTrue(self.namespace["completion"]())

    def test_continuation_never_falls_back_to_legacy_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy, canonical = root / "RESULT", root / "RESULT.md"
            legacy.write_text("DONE\n")
            self.assertEqual(relaunch.result_text(root), "")
            canonical.write_text("partial")
            self.assertEqual(relaunch.result_text(root), "partial")
            canonical.write_text("")
            self.assertEqual(relaunch.result_text(root), "")
            canonical.unlink()
            legacy.unlink()
            canonical.write_text("DONE\n")
            legacy.symlink_to(canonical.name)
            self.assertEqual(relaunch.result_text(root), "DONE\n")
            canonical.unlink()
            self.assertEqual(relaunch.result_text(root), "")

    def test_identical_rule_reads_only_canonical_result(self):
        self.namespace["a"].done_when = "identical"
        with (
            mock.patch("builtins.open", mock.mock_open(read_data="function identical to reference")) as opened,
            mock.patch("os.path.exists", return_value=True),
        ):
            self.assertTrue(self.namespace["completion"]())
        opened.assert_called_once_with("/lane/RESULT.md")

    def test_handoff_archives_only_canonical_result(self):
        self.namespace["context_cap"].requested = True
        with (
            mock.patch("builtins.open", mock.mock_open(read_data="HANDOFF.md")),
            mock.patch("os.path.exists", return_value=True),
            mock.patch("os.replace") as rename,
        ):
            self.assertFalse(self.namespace["completion"]())
        rename.assert_called_once_with("/lane/RESULT.md", "/lane/HANDOFF-RESULT-1.txt")

    def test_generated_contracts_name_only_result_md(self):
        text = relaunch.DONE_RULE + self.namespace["lifecycle_text"]()
        self.assertIn("writes RESULT.md", text)
        self.assertNotRegex(text, r"RESULT(?!\.md)\b")


class RelaunchPlanTests(unittest.TestCase):
    def test_every_unfinished_exit_uses_the_budget_without_requiring_a_handoff(self):
        for rule in ("result", "identical"):
            for said in ("", " \n", "partial work"):
                for handoff, context in ((False, False), (True, False), (False, True), (True, True)):
                    for budget, expected in ((3, "continue"), (0, "blocked")):
                        with self.subTest(rule=rule, said=said, handoff=handoff, context=context, budget=budget):
                            action, why = relaunch.plan(rule, budget, said, handoff, context, False)
                            self.assertEqual(action, expected)
                            self.assertIn("budget", why)

    def test_progress_preserves_budget_even_without_result_or_context_handoff(self):
        action, why = relaunch.plan("result", 0, "", False, False, False, "a declared export file changed")
        self.assertEqual(action, "continue")
        self.assertIn("budget kept at 0", why)
        self.assertEqual(relaunch.next_budget(0, "a declared export file changed"), 0)

    def test_completion_and_cancellation_take_precedence_over_continuation(self):
        self.assertEqual(relaunch.plan("result", 0, "DONE\nfinished", True, False, False)[0], "end")
        self.assertEqual(
            relaunch.plan("identical", 0, "function identical to reference", False, False, False)[0], "end"
        )
        self.assertEqual(relaunch.plan("result", 3, "", True, False, True, waiting=2)[0], "none")


class RunnerContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "WALL-3"
        self.directory.mkdir()
        self.profile = self.root / "seat.json"
        self.profile.write_text(json.dumps({
            "schema_version": 1,
            "name": "seat",
            "lanes": {
                "registry": "lanes.json", "ladder": "models.toml", "codex": "codex",
                "socket_dir": "sockets", "rules": "rules.md",
            },
            "events": {"path": "events.jsonl"},
        }))  # fmt: skip
        self.registry = self.root / "lanes.json"
        with registry.locked(self.registry) as rows:
            rows.append({"name": "WALL-3", "dir": str(self.directory), "state": "live"})
        self.namespace = runner_functions()
        self.args = SimpleNamespace(
            profile=str(self.profile), dir=str(self.directory), name="WALL-3", done_when="result", continues=3,
            owns=[], export=[], kind="impl", model="model", track="official",
        )  # fmt: skip
        self.namespace.update(a=self.args, STARTED=1, EXPORTS_BEFORE={})
        # The observed WIN-3/4 lifecycle: wall exit, no context request, no jobs, no RESULT.
        self.terminal = {
            "exit": 6, "why": "WallReached: 1800 s", "status": "unfinished", "handoff": True,
            "context_requested": False, "rig_jobs": 0,
        }  # fmt: skip

    def close(self):
        error = io.StringIO()
        with (
            mock.patch.object(relaunch, "progress", return_value=""),
            mock.patch.object(relaunch, "spawn", return_value="relaunch-WALL-4") as spawn,
            contextlib.redirect_stderr(error),
        ):
            self.namespace["continue_lane"](self.terminal)
        row = registry.load(self.registry)[0]
        events = [json.loads(line) for line in (self.root / "events.jsonl").read_text().splitlines()]
        return spawn, row, events, error.getvalue()

    def test_wall_without_result_continues_with_or_without_handoff(self):
        for handoff in (False, True):
            with self.subTest(handoff=handoff):
                self.terminal["handoff"] = handoff
                if handoff:
                    (self.directory / "HANDOFF.md").write_text("unfinished work\n")
                spawn, row, events, error = self.close()
                spawn.assert_called_once_with(self.profile, self.directory, "WALL-4", 2)
                self.assertEqual(row["state"], "ended")
                self.assertEqual(events[-1]["status"], "info")
                self.assertIn("continues as WALL-4", events[-1]["message"])
                self.assertEqual(error, "")

    def test_exhaustion_is_visible_in_registry_event_feed_and_stderr(self):
        self.args.continues = 0
        spawn, row, events, error = self.close()
        spawn.assert_not_called()
        self.assertEqual(row["state"], "continue_failed")
        self.assertIn("budget spent", row["error"])
        self.assertEqual(events[-1]["status"], "blocked")
        self.assertIn("budget spent", events[-1]["message"])
        self.assertIn("ContinuationBlocked: WALL-3: missing RESULT.md", error)

    def test_done_result_does_not_spawn_a_successor(self):
        (self.directory / "RESULT.md").write_text("DONE\nfinished\n")
        spawn, row, events, error = self.close()
        spawn.assert_not_called()
        self.assertEqual(row["state"], "ended")
        self.assertEqual(events[-1]["status"], "passed")
        self.assertEqual(error, "")

    def test_cancelled_lane_does_not_spawn_or_report_budget_failure(self):
        self.terminal["status"] = "cancelled"
        with mock.patch.object(relaunch, "spawn") as spawn, mock.patch.object(control, "announce") as announce:
            self.namespace["continue_lane"](self.terminal)
        spawn.assert_not_called()
        announce.assert_not_called()
        self.assertEqual(registry.load(self.registry)[0]["state"], "ended")
