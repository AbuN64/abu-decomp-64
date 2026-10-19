import json
import unittest
from dataclasses import replace
from pathlib import Path

from textual.widgets import Collapsible

from abu_decomp.console.app import ConsoleApp, EvidenceRow, FollowScroll
from abu_decomp.console.dashboard_model import Dashboard, Row, ago, boot_model, mission_model

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

    def test_boot_status_is_three_plain_lines_and_rows_carry_an_age(self):
        runtime = dict(
            sample("runtime"), started_at="2026-10-18T17:00:00+00:00", updated_at="2026-10-18T17:02:00+00:00"
        )
        model = boot(runtime=runtime)
        passing, stopped, nxt = model.pinned
        self.assertRegex(passing, r"^Passing: \d+ of \d+ boot steps ✓$")
        self.assertRegex(stopped, r"^Stopped at: .+ — the port has no native code for this original function yet$")
        self.assertTrue(nxt.startswith("Next: ") and "nothing planned" not in nxt)
        dated = [row for row in model.rows if row.state in ("passed", "stop")]
        self.assertTrue(dated and all(row.when for row in dated))
        self.assertGreater(next(r.when for r in model.rows if r.state == "stop"), dated[0].when)

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
        self.assertEqual(model.rows[0].details, "raw")  # context first; the newest event is last, where the tail is

    def test_mission_row_identity_survives_new_events_and_bounded_history(self):
        events = sample("events")
        options = dict(title="Events", frames="0", graphics="0", run_id="r", raw_log="")
        before = mission_model(events, **options)
        after = mission_model(events[1:], **options)
        self.assertEqual(before.rows[-1].key, after.rows[-1].key)


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
            self.assertEqual(app.query_one("#heading").region.height, 1)  # one compact title line
            self.assertEqual(len(app.query("Footer")), 0)
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


class ConsoleFollowTests(unittest.IsolatedAsyncioTestCase):
    def rows(self, count, now=None):
        import time

        now = now or time.time()
        return [Row(f"r{i}", f"line {i}", "d", when=now - (count - i) * 5) for i in range(count)]

    async def test_new_rows_are_followed_until_the_user_scrolls_up_and_latest_resumes(self):
        class Source:
            def __init__(self):
                self.count = 60

            def read(self):
                return Dashboard("T", "", self.rows(self.count), None)

            def directories(self):
                return set()

        source = Source()
        source.rows = lambda count: ConsoleFollowTests.rows(self, count)
        app = ConsoleApp(source)
        async with app.run_test(size=(80, 20)) as pilot:
            box = app.query_one(FollowScroll)
            await pilot.pause()
            self.assertTrue(box.following)
            self.assertGreater(box.max_scroll_y, 0)
            self.assertEqual(box.scroll_y, box.max_scroll_y)
            source.count = 70
            await app.refresh_sources()
            await pilot.pause()
            self.assertEqual(box.scroll_y, box.max_scroll_y)  # a new line at the bottom is followed
            box.scroll_to(y=0, animate=False)
            await pilot.pause()
            self.assertFalse(box.following)
            self.assertTrue(app.query_one("#latest").display)
            source.count = 80
            await app.refresh_sources()
            await pilot.pause()
            self.assertEqual(box.scroll_y, 0)  # scrolled up: no yanking
            await pilot.press("end")
            await pilot.pause()
            self.assertTrue(box.following)
            self.assertEqual(box.scroll_y, box.max_scroll_y)
            self.assertFalse(app.query_one("#latest").display)

    async def test_pinned_status_and_the_checked_heartbeat_show_in_the_title(self):
        class Source:
            def read(self):
                return Dashboard("T", "", [Row("a", "x", "d")], None, pinned=["Passing: 1 of 2 boot steps ✓", "b", "c"])

            def directories(self):
                return set()

        app = ConsoleApp(Source())
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.pause()
            self.assertEqual(app.query_one("#pinned").region.height, 3)
            app.checked -= 12
            app.show_heading()
            self.assertIn("checked 12s ago", str(app.query_one("#heading").render()))

    async def test_every_row_has_a_live_dim_age_column(self):
        rows = [Row("a", "first", "d", when=1000.0), Row("b", "undated", "d")]

        class Source:
            def read(self):
                return Dashboard("T", "", rows, None)

            def directories(self):
                return set()

        app = ConsoleApp(Source())
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.pause()
            first = app.widgets["a"]
            self.assertIn("[dim]", first.title)
            first.tick(1000.0 + 125)
            self.assertIn("2m", first.title)
            first.tick(1000.0 + 7300)
            self.assertIn("2h", first.title)
            self.assertNotIn("0s", app.widgets["b"].title)

    def test_ago_is_compact(self):
        self.assertEqual([ago(s) for s in (3, 59, 60, 240, 7300, 90000)], ["3s", "59s", "1m", "4m", "2h", "1d"])
