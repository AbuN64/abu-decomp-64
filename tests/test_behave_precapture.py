"""`decomp behave --precapture` (behave/precapture.py): the list, the plan from behave's own describe runs, one batch
call to the oracle tool and the report, with the tools mocked."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from abu_decomp import DecompError
from abu_decomp.behave import precapture, run


class PrecaptureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.options = run.Options("", None, "v1", None, 1, None, None)
        self.calls = []

    def plans(self, table):
        def plan_one(profile, options, name):
            return table[name]

        return mock.patch.object(precapture, "plan_one", plan_one)

    def invoke(self, captures):
        def fake(tool, profile, args, **kw):
            plan = Path(args[args.index("--plan") + 1])
            self.calls.append((tool, args, plan.read_text()))
            return SimpleNamespace(
                ok=True,
                returncode=0,
                document={"result": {"captures": captures, "counts": {}, "oracle_runs": 1, "wall_seconds": 1.5}},
            )

        return mock.patch.object(precapture.tools, "invoke", fake)

    def go(self, spec, table, captures):
        profile = mock.Mock()
        with (
            self.plans(table),
            self.invoke(captures),
            mock.patch.object(precapture.behave_run, "work_dir", return_value=self.root),
            mock.patch.object(precapture.behave_run, "configuration", return_value={}),
            mock.patch.object(precapture.behave_run, "capture_movie", return_value=(None, None)),
            mock.patch.object(precapture.tools, "project_dir", return_value=self.root),
        ):
            return precapture.run(profile, self.options, spec)

    def test_names_come_from_a_comma_list_or_a_file(self):
        listing = self.root / "names.txt"
        listing.write_text("f\n# not this\ng  # trailing\n\n")
        self.assertEqual(precapture.names_of(mock.Mock(), "a, b", None), ["a", "b"])
        self.assertEqual(precapture.names_of(mock.Mock(), str(listing), None), ["f", "g"])

    def test_open_nonmatching_is_every_nonmatching_body_of_the_project(self):
        found = {"f": {"kind": "nm"}, "g": {"kind": "asm"}, "h": {"kind": "nm"}, "MACRO": {"kind": "nm"}}
        table = {"f": [0x80001000], "h": [0x80002000], "g": [0x80003000], "alias": [None]}
        with (
            mock.patch.object(precapture.owes, "functions", return_value=found),
            mock.patch.object(precapture.owes, "settings", return_value={}),
            mock.patch.object(precapture.tools, "project_dir", return_value=self.root),
            mock.patch.object(precapture.evidence, "NM", "nm"),
            mock.patch.object(precapture.build_candidate, "symbol_table", return_value=table),
        ):
            self.assertEqual(precapture.names_of(mock.Mock(), precapture.OPEN, None, "v1"), ["f", "h"])

    def test_one_batch_call_carries_every_planned_stub_set_and_the_report_counts_them(self):
        table = {
            "f": {"function": "f", "status": "planned", "sets": [(1, ["g"]), (2, ["g", "h"])]},
            "g": {"function": "g", "status": "planned", "sets": [(1, [])]},
            "bad": {"function": "bad", "status": "unplanned", "message": "not a symbol"},
        }
        captures = [
            {"function": "f", "status": "captured", "cached": False},
            {"function": "f", "status": "captured", "cached": True},
            {"function": "g", "status": "repro.capture.unreached", "message": "never entered"},
        ]
        result, code, findings = self.go("f,g,bad", table, captures)
        self.assertEqual(len(self.calls), 1)
        tool, args, plan = self.calls[0]
        self.assertEqual((tool, args[:3]), ("taste", ["repro", "capture-batch", "--version"]))
        self.assertIn('"stubs": ["g", "h"]', plan)
        self.assertEqual((code, result["functions"], result["planned"], result["captures"]), (0, 3, 2, 3))
        self.assertEqual((result["newly_captured"], result["already_cached"]), (1, 1))
        self.assertEqual([f["key"] for f in findings], ["behave.repro.capture.unreached", "behave.unplanned"])

    def test_planning_stops_at_the_cpu_budget_and_says_to_run_again(self):
        table = {"f": {"function": "f", "status": "planned", "sets": [(1, [])]}}
        profile = mock.Mock()
        with (
            self.plans(table),
            self.invoke([{"function": "f", "status": "captured", "cached": False}]),
            mock.patch.object(precapture.behave_run, "work_dir", return_value=self.root),
            mock.patch.object(precapture.behave_run, "configuration", return_value={}),
            mock.patch.object(precapture.behave_run, "capture_movie", return_value=(None, None)),
            mock.patch.object(precapture.tools, "project_dir", return_value=self.root),
            mock.patch.object(precapture.meter, "cpu", side_effect=[0.0, 1.0, 999.0, 999.0, 999.0, 999.0]),
        ):
            result, _, findings = precapture.run(profile, self.options, "f,g", cpu_budget=100.0)
        self.assertEqual((result["planned"], result["deferred"]), (1, ["g"]))
        self.assertIn("behave.deferred", [f["key"] for f in findings])

    def plan_with(self, runner):
        profile = mock.Mock()
        pins = {"taste": mock.Mock(commit="abc")}
        with (
            mock.patch.object(precapture.behave_run, "run", runner),
            mock.patch.object(precapture.behave_run, "work_dir", return_value=self.root),
            mock.patch.object(precapture.behave_run, "configuration", return_value={"cases": 48, "depth": 1}),
            mock.patch.object(precapture.tools, "pins", return_value=pins),
        ):
            return precapture.plan_one(profile, self.options, "f")

    def test_a_describe_that_fails_after_noting_its_stubs_still_plans_them_and_the_plan_is_remembered(self):
        runs = []

        def runner(profile, options):
            runs.append(options.function)
            options.plan.append((1, ["g"]))
            raise IndexError("list index out of range")

        first = self.plan_with(runner)
        self.assertEqual(
            (first["status"], first["sets"][0][1], "IndexError" in first["failure"]), ("planned", ["g"], True)
        )
        again = self.plan_with(runner)
        self.assertEqual(
            (again["status"], again["memo"], runs), ("planned", True, ["f"])
        )  # the second never ran behave

    def test_a_function_behave_never_reaches_a_capture_for_is_unplanned_with_the_reason(self):
        def runner(profile, options):
            raise DecompError("behave.function", "f is not a symbol of version v1")

        row = self.plan_with(runner)
        self.assertEqual(row["status"], "unplanned")
        self.assertIn("not a symbol", row["message"])

    def test_nothing_planned_never_calls_the_oracle_tool(self):
        table = {"bad": {"function": "bad", "status": "unplanned", "message": "x"}}
        result, _, _ = self.go("bad", table, [])
        self.assertEqual((self.calls, result["captures"], result["oracle"]["oracle_runs"]), ([], 0, 0))

    def test_an_empty_list_is_refused(self):
        with (
            mock.patch.object(precapture, "names_of", return_value=[]),
            self.assertRaises(DecompError),
        ):
            precapture.run(mock.Mock(), self.options, "")


if __name__ == "__main__":
    unittest.main()

    def test_a_planned_function_carries_its_movie_and_one_nothing_reaches_is_left_out(self):
        table = {
            "f": {"function": "f", "status": "planned", "sets": [(1, ["g"])]},
            "h": {"function": "h", "status": "planned", "sets": [(1, [])]},
        }
        own = {"env": {"N64R_TAS_PLAY": "/m/a.raw"}, "seconds": 90, "movie": "a", "poll": 4}
        verdict = {"key": "behave.no_movie", "blocking": False, "message": "none reaches it"}
        profile = mock.Mock()
        with (
            self.plans(table),
            self.invoke([{"function": "f", "status": "captured", "cached": False}]),
            mock.patch.object(precapture.behave_run, "work_dir", return_value=self.root),
            mock.patch.object(precapture.behave_run, "configuration", return_value={}),
            mock.patch.object(
                precapture.behave_run,
                "capture_movie",
                lambda p, c, name, v: (own, None) if name == "f" else (None, verdict),
            ),
            mock.patch.object(precapture.tools, "project_dir", return_value=self.root),
        ):
            _, _, findings = precapture.run(profile, self.options, "f,h")
        rows = json.loads(self.calls[0][2])
        self.assertEqual(rows, [{"function": "f", "stubs": ["g"], "env": own["env"], "seconds": 90}])
        self.assertIn("h: none reaches it", [f["message"] for f in findings if f["key"] == "behave.no_movie"])
