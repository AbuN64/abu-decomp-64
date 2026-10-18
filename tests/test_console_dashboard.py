import json
import unittest
from dataclasses import replace
from pathlib import Path

from textual.widgets import Collapsible

from abu_decomp.console.app import ConsoleApp, EvidenceRow
from abu_decomp.console.dashboard_model import Dashboard, boot_model, mission_model

FIXTURE = Path(__file__).parent / "fixtures" / "console"


def sample(name):
    return json.loads((FIXTURE / (name + ".json")).read_text())


def boot(**overrides):
    values = dict(
        profile=sample("profile"),
        status=sample("status"),
        receipt=sample("receipt"),
        gate_log=(FIXTURE / "boot.txt").read_text(),
        runtime=sample("runtime"),
        events=sample("events"),
        test_log=(FIXTURE / "ctest.txt").read_text(),
        plan=(FIXTURE / "plan.md").read_text(),
        plan_path="BOOT_WINDOW.md",
        plan_after=["Next:", "Then setup calls"],
        plan_before="Evidence:",
        release={"revision": "1f8d01c"},
    )
    values.update(overrides)
    return boot_model(**values)


class ConsoleModelTests(unittest.TestCase):
    def test_real_gate_order_and_expected_failure_are_distinct_from_boot_completion(self):
        model = boot()
        expected = [needle for command in sample("profile")["build"]["commands"] for needle in command["expect_all"]]
        rows = [row for row in model.rows if row.key.startswith("gate-")]
        self.assertEqual(len(rows), len(expected))
        self.assertTrue(rows[0].title.startswith("0x80265964"))
        self.assertEqual(rows[-1].state, "stop")
        self.assertIn("0x8040F9E0 frontend string bank", rows[-1].title)
        self.assertTrue(all(row.state == "passed" for row in rows[:-1]))
        self.assertIn("tests 86/86", model.heading)
        self.assertIn(f"gate {len(expected)}/{len(expected)}", model.heading)
        self.assertIn(f"{len(expected) - 1} passed", model.summary)
        self.assertEqual(model.rows[-1].key, "raw-log")
        planned = [row for row in model.rows if row.key.startswith("plan-")]
        self.assertEqual(planned[0].title, "0x8041B5E0 planned owner")
        self.assertTrue(all(row.state == "planned" for row in planned))
        self.assertIn("Build commit:", rows[-1].details)
        self.assertIn("[unknown]", rows[-1].details)
        self.assertTrue(any("MATH/80297CD0_3.CPP" in row.details for row in rows))

    def test_new_failed_attempt_is_not_replaced_with_last_success(self):
        status = dict(sample("status"), phase="failed", started_at="2026-10-18T17:00:00+00:00")
        receipt = dict(sample("receipt"), steps=[{"name": "build", "returncode": 2, "expected_exit_codes": [0]}])
        model = boot(status=status, receipt=receipt, gate_log="", test_log="", runtime={})
        self.assertIn("17:00:00 UTC", model.heading)
        self.assertIn("build FAIL (2)", model.heading)
        self.assertIn("gate 0/", model.heading)
        self.assertFalse(any(row.state == "passed" for row in model.rows))

    def test_real_mission_payload_preserves_question_source_and_evidence(self):
        events = sample("events")
        model = mission_model(events, title="ORACLE", frames="123", graphics="45", run_id="run-7", raw_log="raw")
        self.assertIn("run run-7 · frames 123 · graphics 45", model.heading)
        qualified = [event for event in events if (event.get("payload") or {}).get("decomp_path")]
        self.assertTrue(qualified)
        event = qualified[-1]
        matching = [row for row in model.rows if event["evidence"] in row.details]
        self.assertTrue(matching)
        self.assertIn(event["payload"]["decomp_path"], matching[0].details)
        self.assertEqual(model.rows[-1].details, "raw")

    def test_mission_row_identity_survives_new_events_and_bounded_history(self):
        events = sample("events")
        options = dict(title="Events", frames="0", graphics="0", run_id="r", raw_log="")
        before = mission_model(events, **options)
        after = mission_model(events[1:], **options)
        self.assertEqual(before.rows[0].key, after.rows[0].key)


class ConsoleInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_click_and_keyboard_expand_and_refresh_preserves_open_row(self):
        full = boot()
        stop = next(row for row in full.rows if row.state == "stop")

        class Source:
            current = stop

            def read(self):
                return Dashboard(full.heading, full.summary, [self.current, full.rows[-1]], stop.key)

            def directories(self):
                return set()

        app = ConsoleApp(Source())
        async with app.run_test(size=(106, 29)) as pilot:
            row = app.query_one(EvidenceRow)
            self.assertGreater(app.query_one("#summary").region.y, app.query_one("#heading").region.y)
            self.assertTrue(row.collapsed)
            app.sources.current = replace(stop, details="New evidence", state="passed")
            await app.refresh_sources()
            await pilot.pause()
            self.assertTrue(row.collapsed)
            self.assertFalse(row.query_one(Collapsible.Contents).display)
            self.assertTrue(row.has_class("passed"))
            self.assertFalse(row.has_class("stop"))
            await pilot.press("enter")
            self.assertFalse(row.collapsed)
            app.sources.current = replace(stop, details="Updated open evidence")
            await app.refresh_sources()
            await pilot.pause()
            self.assertFalse(row.collapsed)
            self.assertTrue(row.query_one(Collapsible.Contents).display)
            await pilot.click(f"#{row.id} CollapsibleTitle")
            self.assertTrue(row.collapsed)
            await pilot.press("space")
            self.assertFalse(row.collapsed)
