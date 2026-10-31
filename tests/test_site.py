"""The Runner site's contract, feed and shell (module site_core, lanes/RUNNER-REDO/PLAN.md)."""

import json
import re
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from abu_decomp import site_core, tas_serve

ROOT = Path(__file__).resolve().parents[1] / "src" / "abu_decomp"
RUNNING = {
    "id": "r1",
    "movie": "boot-idle",
    "status": "running",
    "poll": 1200,
    "polls": 3000,
    "started": 10.0,
    "label": "LANE-A",
    "input": "movie",
    "oracle": {"every": 25},
}
DONE = {
    "id": "r2",
    "movie": "menu",
    "status": "done",
    "poll": 1401,
    "polls": 1101,
    "outcome": "ok",
    "started": 1.0,
    "ended": 2.0,
    "input": "human",
}


class ContractTest(unittest.TestCase):
    def test_every_template_names_only_fields_and_every_family_has_an_icon_in_the_page(self):
        page = (ROOT / "runner.html").read_text()
        for kind, template in site_core.TEMPLATES.items():
            self.assertTrue(re.fullmatch(r"[a-z]+\.[a-z]+(:[a-z-]+)?", kind), kind)
            self.assertIn(f'"{kind.split(":")[0]}"', page, kind)
            self.assertNotIn("{}", template)

    def test_filters_have_distinct_single_bits_in_contract_order(self):
        filters = site_core.contract()["filters"]
        bits = [f["bit"] for f in filters]
        self.assertEqual(bits, [1 << i for i in range(len(filters))])
        self.assertEqual([f["key"] for f in filters][:3], ["named", "exact", "asm"])

    def test_contract_names_the_three_run_kinds_and_the_card_of_every_region(self):
        got = site_core.contract()
        self.assertEqual(got["run_kinds"], ["unit", "set", "tas"])
        self.assertEqual(set(got["regions"]), {"map", "fn", "stream", "feedback"})
        self.assertIn("crashed", got["feedback_categories"])


class RecordTest(unittest.TestCase):
    def test_run_record_reads_lane_sides_and_kind(self):
        run = site_core.run_record(RUNNING)
        self.assertEqual((run["kind"], run["lane"], run["sides"]), ("tas", "LANE-A", ["port", "original"]))
        self.assertEqual(site_core.run_record(DONE)["lane"], "owner")
        self.assertEqual(site_core.run_record({**DONE, "input": "movie"})["lane"], "runner")

    def test_unknown_run_kind_is_refused(self):
        with self.assertRaisesRegex(ValueError, "kind 'bake'"):
            site_core.run_record({**DONE, "kind": "bake"})

    def test_each_run_state_renders_its_template(self):
        def render(row: dict) -> list[str]:
            return [site_core.render(e) for e in site_core.run_events(site_core.run_record(row))]

        self.assertEqual(render(RUNNING), ["LANE-A is playing boot-idle (poll 1,200 of 3,000)"])
        self.assertEqual(render({**RUNNING, "target": "Fn_A"}), ["LANE-A is playing toward Fn_A (poll 1,200 of 3,000)"])
        self.assertEqual(
            render({**RUNNING, "kind": "unit", "target": "Fn_A", "equal": 41, "cases": 50}),
            ["LANE-A is matching function Fn_A: 41/50 cases equal"],
        )
        self.assertEqual(render(DONE), ["owner finished menu: ok at poll 1,401 of 1,101"])
        self.assertEqual(render({**DONE, "error": "stale binary"}), ["owner was refused menu: stale binary"])
        self.assertEqual(render({**RUNNING, "status": "queued"}), ["LANE-A queued boot-idle"])

    def test_an_event_missing_a_template_field_is_refused(self):
        with self.assertRaisesRegex(ValueError, "missing equal, cases"):
            site_core.run_events(site_core.run_record({**RUNNING, "kind": "unit", "target": "Fn_A"}))
        with self.assertRaisesRegex(ValueError, "no template"):
            site_core.event("run.danced", 0.0, "x", "y")

    def test_a_run_keeps_one_event_id_from_start_to_end(self):
        start = site_core.run_events(site_core.run_record(RUNNING))[0]
        ended = {**RUNNING, "status": "done", "outcome": "ok", "ended": 20.0}
        end = site_core.run_events(site_core.run_record(ended))[0]
        self.assertNotEqual(start["kind"], end["kind"])
        self.assertEqual(site_core.run_events(site_core.run_record({**RUNNING, "poll": 1300}))[0]["id"], start["id"])


class FeedTest(unittest.TestCase):
    def test_publish_counts_only_new_or_changed_events_and_moves_the_version(self):
        feed = site_core.Feed()
        self.assertEqual(feed.publish_runs([RUNNING, DONE]), 2)
        self.assertEqual(feed.publish_runs([RUNNING, DONE]), 0)
        self.assertEqual(feed.snapshot()[0], 1)
        self.assertEqual(feed.publish_runs([{**RUNNING, "poll": 1300}]), 1)
        version, items = feed.since(1, 0)
        self.assertEqual((version, [e["fields"]["poll"] for e in items]), (2, [1300]))
        self.assertEqual([e["subject"] for e in feed.snapshot()[1]], ["r1", "r2"])  # newest first

    def test_a_malformed_run_is_skipped_not_fatal(self):
        feed = site_core.Feed()
        self.assertEqual(feed.publish_runs([{**DONE, "kind": "bake"}, RUNNING]), 1)

    def test_the_feed_keeps_the_newest_events(self):
        feed = site_core.Feed()
        feed.publish_runs([{**DONE, "id": f"r{i}", "ended": float(i)} for i in range(site_core.FEED_KEPT + 5)])
        items = feed.snapshot()[1]
        self.assertEqual(len(items), site_core.FEED_KEPT)
        self.assertEqual(items[-1]["at"], 5.0)

    def test_since_wakes_on_publish_from_another_thread(self):
        feed = site_core.Feed()
        threading.Thread(target=feed.publish_runs, args=([RUNNING],)).start()
        version, items = feed.since(0, 60)  # returns on the signal; the bound is never reached
        self.assertEqual((version, len(items)), (1, 1))


class FakeServer:
    def __init__(self) -> None:
        self.feed = site_core.Feed()


class EndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FakeServer()
        self.server.feed.publish_runs([RUNNING, DONE])
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), tas_serve.handler(self.server))  # type: ignore[arg-type]
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"

    def tearDown(self) -> None:
        self.http.shutdown()
        self.http.server_close()

    def get(self, path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(self.base + path) as reply:
                return reply.status, json.loads(reply.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_contract_and_feed_answer_json(self):
        status, body = self.get("/api/contract")
        self.assertEqual((status, body["templates"]["run.ended"]), (200, site_core.TEMPLATES["run.ended"]))
        status, body = self.get("/api/feed")
        self.assertEqual((status, [e["subject"] for e in body["events"]]), (200, ["r1", "r2"]))

    def test_unbuilt_regions_answer_501_with_their_card(self):
        for path, card in (
            ("/api/stream/r1", "RUNNER-STREAM"),
            ("/api/feedback", "RUNNER-PLAY"),
        ):
            self.assertEqual(self.get(path), (501, {"error": self.get(path)[1]["error"], "card": card}), path)
        request = urllib.request.Request(self.base + "/api/feedback", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 501)

    def test_the_stream_says_hello_replays_what_it_holds_then_pushes_a_changed_event(self):
        with urllib.request.urlopen(self.base + "/api/feed/stream") as reply:
            self.assertEqual(reply.headers["Content-Type"], "text/event-stream")
            self.assertEqual(reply.readline(), b"event: hello\n")
            self.assertEqual(json.loads(reply.readline().removeprefix(b"data: ")), {"version": 1})
            reply.readline()
            for subject in ("r1", "r2"):
                self.assertEqual(reply.readline(), b"event: feed\n")
                self.assertEqual(json.loads(reply.readline().removeprefix(b"data: "))["subject"], subject)
                reply.readline()
            self.server.feed.publish_runs([{**RUNNING, "poll": 1500}])
            self.assertEqual(reply.readline(), b"event: feed\n")
            self.assertEqual(json.loads(reply.readline().removeprefix(b"data: "))["fields"]["poll"], 1500)


class PageTest(unittest.TestCase):
    def test_the_page_loads_every_region_module_that_ships(self):
        page = (ROOT / "runner.html").read_text()
        for path in ("/api/contract", "/api/feed", "/api/feed/stream", "/static/site/map.js", "/static/icons.svg"):
            self.assertIn(path, page)
        shipped = {p.stem for p in (ROOT / "static" / "site").glob("*.js")}
        self.assertEqual(shipped, {"map", "fn", "stream", "play"})
        for name in shipped:
            self.assertRegex((ROOT / "static" / "site" / f"{name}.js").read_text(), r"export function mount\(el, ctx\)")
