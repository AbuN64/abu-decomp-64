import io
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from abu_decomp import narrate
from abu_decomp.config import load_profile
from abu_decomp.console import hud, ops
from abu_decomp.console.dashboard_sources import Sources
from abu_decomp.console.feed import append_event


class Profile:
    def __init__(self, root):
        self.root = root

    def section(self, name):
        return {"path": "events.jsonl"}

    def resolve_path(self, value):
        return self.root / value


class LanePaneTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.profile = Profile(self.root)

    def add(self, actor, status, message, **payload):
        append_event(self.profile, actor, status, message, kind="LANE", payload=payload)

    def sources(self, config=None):
        return Sources(self.root / "events.jsonl", title="PORT", config=config or {"panes": {"lane": {"actor": "L1"}}})

    def test_rows_are_terse_one_liners_per_op_and_other_actors_are_ignored(self):
        self.add("L1", "info", "trace past order 10", op="step")
        self.add("L1", "info", "build look-past", op="build", name="rp", ok=True, passed=154, total=166)
        self.add("L1", "failed", "link error", op="test", name="ctest", ok=False)
        self.add("L1", "info", "x", op="stop", name="0x80291BE8", reason="no native code")
        self.add("L1", "passed", "Use matched C", op="commit", sha="b9556c0123", subject="Use matched C")
        self.add("other", "info", "not mine", op="step")
        model = self.sources().read()
        self.assertEqual(
            [r.title for r in model.rows],
            [
                "step: trace past order 10",
                "build rp ✓ 154/166",
                "test ctest ✗: link error",
                "stop 0x80291BE8: no native code",
                "commit b9556c0 Use matched C",
            ],
        )
        self.assertEqual([r.state for r in model.rows], ["event", "passed", "stop", "stop", "passed"])
        self.assertEqual(model.pinned[:2], ["step: trace past order 10", "stop 0x80291BE8: no native code"])
        self.assertIn("build rp ✓ 154/166 · test ctest ✗", model.pinned[2])

    def test_newest_step_replaces_older_pin(self):
        self.add("L1", "info", "one", op="step")
        self.add("L1", "info", "two", op="step")
        self.assertEqual(self.sources().read().pinned, ["step: two"])


class OpsPaneTests(unittest.TestCase):
    def test_only_host_faults_and_errors_are_rows_counters_go_to_the_title(self):
        text = "\n".join(
            [
                "n64host: cut line",
                "n64host: cpu_cadence vi=60 count_ticks=1 entries=2 native_ns=3",
                "n64host: graphics tasks=75000 elapsed_ms=2542697 dl=0x806D2000 bytes=4928",
                "n64host: screen updates=152400 elapsed_ms=2542068 graphics_tasks=74984",
                "n64host: TLB fault va=00800004 thread=8011f880",
                "n64host: audio queued_samples=736 nonzero_samples=0",
            ]
        )
        self.assertEqual([t for t, _ in ops.host_rows(text)], ["TLB fault va 0x00800004 thread 0x8011F880"])

    def test_trace_entries_are_classified_and_repeats_fold(self):
        text = "cut\n5 t osPiStartDma\n5 t osPiStartDma\n6 t osViSwapBuffer\n6 t osCreateThread\n"
        text += "7 t func_8000_de\n8 t osSpTaskStart\n"
        self.assertEqual(
            [t for t, _ in ops.trace_rows(text)],
            [
                "PI DMA osPiStartDma x2 · vi 5",
                "VI osViSwapBuffer · vi 6",
                "OS osCreateThread · vi 6",
                "enter func_8000_de · vi 7",
                "RSP/RDP osSpTaskStart · vi 8",
            ],
        )

    def test_a_repro_run_is_one_function_params_output_row_and_nested_calls_never_rows(self):
        text = "".join(
            f"case c{i}\ncall behave_func_8044_de 80A00000 0 0 0\ncall func_8029_de 1\ncall func_8029_de 2\n"
            f"st 80A0 4 1\nrd 80A4 4 0\nret 0 80A03660 0 0\nend ok\n"
            for i in range(3)
        )
        cases = ops.parse_cases(text)
        title, details, state = ops.run_row("20261019T033222441622Z", cases, {"function": "func_8044_de"})
        self.assertEqual(title, "func_8044_de(80A00000): ret 0 · 3/3 ok")
        self.assertEqual(state, "passed")
        self.assertIn("input      a0 80A00000  (c0)", details)
        self.assertIn("output     v0 0  v1 80A03660", details)
        self.assertIn("calls      func_8029_de x6", details)
        self.assertIn("memory     3 stores, 3 reads", details)
        self.assertIn("each case  c0  (80A00000) -> 0, 80A03660  ok  · 2 calls", details)
        self.assertIn("(root behave_func_8044_de)", details)

    def test_a_run_with_a_failed_case_is_a_stop_row_and_says_which_returns_differ(self):
        text = "case c0\ncall f 1\nret 1\nend ok\ncase c1\ncall f 2\nret 2\nend fault\n"
        title, details, state = ops.run_row("run", ops.parse_cases(text), {})
        self.assertEqual(title, "f(1): ret 1 … · 1/2 ok")
        self.assertEqual(state, "stop")
        self.assertIn("cases      1 ok, 1 fault", details)

    def test_tile_shows_only_cake_taste_runs_never_the_decomp_feed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            append_event(Profile(root), "git", "info", "rage-ports abc Fix a thing", kind="LAND")
            (root / "host.log").write_text("x\nn64host: screen updates=60 elapsed_ms=1 graphics_tasks=3\n")
            runs = root / "runs" / "20261019T033222441622Z"
            runs.mkdir(parents=True)
            (runs / "events.txt").write_text("case c0\ncall f 1\nend ok\n")
            config = {"host_log": str(root / "host.log"), "panes": {"ops": {"runs": str(root / "runs")}}}
            model = Sources(root / "events.jsonl", title="CAKE TASTE", config=config, host_log=root / "host.log").read()
            self.assertEqual([r.title for r in model.rows], ["f(1): no return · 1/1 ok"])
            self.assertEqual(model.heading, "CAKE TASTE")
            self.assertEqual(model.facts, ["1 run", "vi - · frames 60 · gfx tasks 3"])
            self.assertTrue(model.rows[0].when)

    def test_hud_naming_a_console_shows_only_its_lines_not_the_decomp_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            console = {"title": "C", "panes": {"lane": {"actor": "L1"}}}
            roles = {"v": {"hud": {"console": "c"}}, "c": {"console": console}}
            seat = {"schema_version": 1, "name": "t", "events": {"path": "events.jsonl"}}
            seat["tiles"] = {"layout": [["v", "c"]], "roles": roles}
            (root / "seat.json").write_text(json.dumps(seat))
            profile = load_profile(root / "seat.json")
            append_event(Profile(root), "L1", "info", "go", kind="LANE", payload={"op": "step"})
            attempt = {"op": "attempt", "function": "f_de", "version": "de", "words": {"matched": 1, "target": 2}}
            append_event(Profile(root), "x", "info", "a", kind="MATCH", payload=attempt)
            out = io.StringIO()
            hud.run_hud(profile, "v", out=out, iterations=1)
            self.assertIn("step: go", out.getvalue())
            self.assertNotIn("match", out.getvalue())


class LaneAttributionTests(unittest.TestCase):
    def test_lane_found_from_the_environment_or_a_parent_process_when_cwd_is_elsewhere(self):
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory) / "lanes" / "RW-X" / "scratch"
            scratch.mkdir(parents=True)
            elsewhere = Path("/nonexistent/place")
            with (
                mock.patch.dict(os.environ, {"TMPDIR": str(scratch)}, clear=True),
                mock.patch("pathlib.Path.cwd", return_value=elsewhere),
                mock.patch("os.getppid", return_value=1),
            ):
                self.assertEqual(narrate.lane_of(None), "RW-X")  # from the environment
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch("pathlib.Path.cwd", return_value=elsewhere),
                mock.patch("os.getppid", return_value=4242),
                mock.patch("os.readlink", return_value=str(scratch)),
                mock.patch("pathlib.Path.read_text", return_value="4242 (zsh) S 1 1 1"),
            ):
                self.assertEqual(narrate.lane_of(None), "RW-X")  # from the parent process's cwd


class ActivityAgingTests(unittest.TestCase):
    def test_refused_landing_ages_out_and_is_retired_by_a_later_landing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(UTC)

            def landing(status, age):
                event = {
                    "schema": 2,
                    "time": (now - timedelta(seconds=age)).isoformat(),
                    "actor": "decomp",
                    "kind": "LAND",
                    "status": status,
                    "message": "land: x: refused",
                    "payload": {"op": "landed", "functions": []},
                }
                return json.dumps(event) + "\n"

            def states(*lines):
                (root / "events.jsonl").write_text("".join(lines))
                config = {"panes": {"activity": {"fail_seconds": 1800}}}
                return [r.state for r in Sources(root / "events.jsonl", title="D", config=config).read().rows]

            self.assertEqual(states(landing("failed", 600)), ["stop"])
            self.assertEqual(states(landing("failed", 2700)), ["planned"])
            self.assertEqual(states(landing("failed", 600), landing("passed", 300)), ["passed"])
