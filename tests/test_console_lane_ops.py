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

    def test_stale_boot_gate_is_dropped_and_a_current_one_kept(self):
        commands = [{"name": "boot", "expect_all": ["owner 0x80000001 x"]}]
        profile = {"build": {"workspace": str(self.root), "commands": commands}}
        (self.root / "profile.json").write_text(json.dumps(profile))
        self.add("L1", "info", "go", op="step")
        for age, expect in ((7200, False), (10, True)):
            started = (datetime.now(UTC) - timedelta(seconds=age)).isoformat()
            (self.root / "status.json").write_text(json.dumps({"started_at": started}))
            config = {"build_profile": str(self.root / "profile.json"), "panes": {"lane": {"actor": "L1"}}}
            model = self.sources(config).read()
            self.assertEqual(any(r.key.startswith("gate-") or r.key == "raw-log" for r in model.rows), expect, age)
            self.assertEqual(any(line.startswith("pass ") for line in model.pinned), expect, age)


class OpsPaneTests(unittest.TestCase):
    def test_host_lines_become_rsp_vi_tlb_rows_and_chatty_cadence_is_dropped(self):
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
        self.assertEqual(
            [t for t, _ in ops.host_rows(text)],
            [
                "RSP task #75000 dl 0x806D2000 4928 B",
                "VI swap #152400 · 74984 gfx tasks",
                "TLB fault va 0x00800004 thread 0x8011F880",
            ],
        )

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

    def test_repro_run_events_become_one_row_per_case(self):
        text = "case c0\ncall func_8042_de 0 1 2\nret 5 0\nsaved\nend ok\ncase c1\ncall func_8042_de 3\nend ok\n"
        self.assertEqual(
            [t for t, _ in ops.case_rows(text, "033222")],
            ["033222 c0: call func_8042_de(0, 1, 2) -> ret 5 0 ok", "033222 c1: call func_8042_de(3) ok"],
        )

    def test_tile_shows_only_oracle_operations_never_the_decomp_feed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            append_event(Profile(root), "git", "info", "rage-ports abc Fix a thing", kind="LAND")
            (root / "host.log").write_text("x\nn64host: screen updates=60 elapsed_ms=1 graphics_tasks=3\n")
            runs = root / "runs" / "20261019T033222441622Z"
            runs.mkdir(parents=True)
            (runs / "events.txt").write_text("case c0\ncall f 1\nend ok\n")
            config = {"host_log": str(root / "host.log"), "panes": {"ops": {"runs": str(root / "runs")}}}
            model = Sources(root / "events.jsonl", title="ORACLE", config=config, host_log=root / "host.log").read()
            titles = [r.title for r in model.rows]
            self.assertEqual(titles, ["VI swap #60 · 3 gfx tasks", "033222 c0: call f(1) ok"])

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
