import json
import unittest
from dataclasses import replace
from pathlib import Path

from textual.widgets import Collapsible

from abu_decomp.console.app import ConsoleApp, EvidenceRow, FollowScroll
from abu_decomp.console.dashboard_model import Dashboard, Row, ago, mission_model

FIXTURE = Path(__file__).parent / "fixtures" / "console"


def sample(name):
    return json.loads((FIXTURE / (name + ".json")).read_text())


class ConsoleModelTests(unittest.TestCase):
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
        stop = Row("stop", "func_80256E6C_de  stopped here", "frame  #7", "stop", when=1000.0)
        full = Dashboard("PORT", "", [stop, Row("raw", "log", "raw", "raw")], stop.key, facts=["boot 60/932"])

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
            for _ in range(40):
                await pilot.pause(0.05)
                if box.scroll_y == box.max_scroll_y > 0:
                    break
            self.assertTrue(box.following)
            self.assertGreater(box.max_scroll_y, 0)
            self.assertEqual(box.scroll_y, box.max_scroll_y)
            source.count = 70
            await app.refresh_sources()
            for _ in range(40):  # layout settles later on a slow machine
                await pilot.pause(0.05)
                if box.scroll_y == box.max_scroll_y > 40:
                    break
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
            for _ in range(40):
                await pilot.pause(0.05)
                if box.scroll_y == box.max_scroll_y:
                    break
            self.assertTrue(box.following)
            self.assertEqual(box.scroll_y, box.max_scroll_y)
            self.assertFalse(app.query_one("#latest").display)

    async def test_title_line_carries_the_facts_and_the_age_of_the_newest_row(self):
        import time

        class Source:
            def read(self):
                rows = [Row("a", "x", "d", when=time.time() - 12)]
                return Dashboard("PORT", "", rows, None, pinned=["boot 1/2"], facts=["boot 1/2", "stop f"])

            def directories(self):
                return set()

        app = ConsoleApp(Source())
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.pause()
            self.assertEqual(len(app.query("#pinned")), 0)  # no pinned block: the title line carries the values
            heading = str(app.query_one("#heading").render())
            self.assertTrue(heading.startswith("PORT · boot 1/2 · stop f · 1"), heading)
            self.assertIn("12s", heading)

    async def test_every_row_shows_its_clock_time_and_the_delta_since_the_previous_row(self):
        import time

        base = time.mktime((2026, 10, 19, 21, 42, 50, 0, 0, -1))
        rows = [Row("a", "first", "d", when=base), Row("b", "undated", ""), Row("c", "third", "d", when=base + 125)]

        class Source:
            def read(self):
                return Dashboard("T", "", rows, None)

            def directories(self):
                return set()

        app = ConsoleApp(Source())
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.pause()
            self.assertIn("[dim]21:42:50     [/dim]", app.widgets["a"].title)  # the first row has no delta
            self.assertIn("21:44:55  +2m", app.widgets["c"].title)  # counts from the last row that has a time
            self.assertTrue(app.widgets["b"].title.startswith("[dim]" + " " * 13 + "[/dim]"))
            self.assertIn("no further detail", str(app.widgets["b"].body.render()))

    def test_ago_is_compact(self):
        self.assertEqual([ago(s) for s in (3, 59, 60, 240, 7300, 90000)], ["3s", "59s", "1m", "4m", "2h", "1d"])
