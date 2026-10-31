"""Human flags on the Runner site (module site_feedback, lanes/RUNNER-REDO/PLAN.md): validation, record, events, CLI."""

import json
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from abu_decomp import DecompError, site_core, site_feedback

ROW = {
    "id": "r1",
    "movie": "boot-idle",
    "status": "running",
    "poll": 90,
    "polls": 600,
    "started": 5.0,
    "input": "human",
}


def raw(count: int) -> str:
    return "".join(f"{i:04X} {i % 7} {-i % 5}\n" for i in range(count))


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runs = Path(tmp.name)
        run = self.runs / "plays" / "r1"
        run.mkdir(parents=True)
        (run / "run.json").write_text(json.dumps(ROW))
        (run / "polls.raw").write_text(raw(300))
        self.run = run
        self.server = SimpleNamespace(
            cfg={"runs": self.runs},
            feed=site_core.Feed(),
            run_dir=lambda run_id: run if run_id == "r1" else None,
            capture=lambda path, kind="PNG": (0, b"JPEG:" + path.name.encode(), 0),
        )

    def shot(self, side: str, poll: int, folder: str | None = None) -> Path:
        where = self.run / "shots" / (folder or side)
        where.mkdir(parents=True, exist_ok=True)
        path = where / f"poll-{poll:06d}.ppm"
        path.write_bytes(b"P6")
        return path


class CheckTest(unittest.TestCase):
    def test_a_good_request_gives_run_poll_category_and_trimmed_text(self):
        got = site_feedback.check({"run": "r1", "poll": 0, "category": "audio", "text": "  pops \n"})
        self.assertEqual(got, ("r1", 0, "audio", "pops"))

    def test_every_category_of_the_contract_is_accepted_and_text_is_optional(self):
        for category in site_core.FEEDBACK_CATEGORIES:
            self.assertEqual(site_feedback.check({"run": "r", "poll": 3, "category": category})[2:], (category, ""))

    def test_refusals_name_the_field(self):
        good = {"run": "r1", "poll": 4, "category": "ui", "text": ""}
        cases = {
            "body": [None, [], "x"],
            "run": [{**good, "run": ""}, {**good, "run": 3}, {k: v for k, v in good.items() if k != "run"}],
            "poll": [{**good, "poll": -1}, {**good, "poll": "4"}, {**good, "poll": True}, {**good, "poll": 1.5}],
            "category": [{**good, "category": "lag"}, {**good, "category": None}],
            "text": [{**good, "text": "x" * (site_feedback.TEXT_MAX + 1)}, {**good, "text": 5}],
        }
        for field, bodies in cases.items():
            for body in bodies:
                with self.assertRaisesRegex(ValueError, f"^{field}:", msg=repr(body)):
                    site_feedback.check(body)


class InputsAndFramesTest(Fixture):
    def test_inputs_are_the_window_of_pad_zero_ending_at_the_poll(self):
        got = site_feedback.inputs_at(self.run, 200)
        self.assertEqual(len(got), site_feedback.INPUT_WINDOW)
        self.assertEqual(got[-1], {"poll": 200, "buttons": 200, "x": 200 % 7, "y": -200 % 5})
        self.assertEqual(got[0]["poll"], 200 - site_feedback.INPUT_WINDOW + 1)

    def test_inputs_near_the_start_and_past_the_end_are_clipped(self):
        self.assertEqual([r["poll"] for r in site_feedback.inputs_at(self.run, 2)], [0, 1, 2])
        self.assertEqual(site_feedback.inputs_at(self.run, 310)[-1]["poll"], 299)

    def test_a_run_without_recorded_inputs_has_none(self):
        (self.run / "polls.raw").unlink()
        self.assertEqual(site_feedback.inputs_at(self.run, 10), [])

    def test_newest_at_takes_the_highest_poll_not_after_the_flag(self):
        for poll in (0, 25, 50, 75):
            self.shot("port", poll)
        self.assertEqual(
            site_feedback.newest_at(self.run / "shots" / "port", 60, ".ppm", "poll-").name, "poll-000050.ppm"
        )
        self.assertIsNone(site_feedback.newest_at(self.run / "shots" / "none", 60, ".ppm", "poll-"))

    def test_frame_path_prefers_the_packed_stream_then_shots_then_the_live_view(self):
        self.assertIsNone(site_feedback.frame_path(self.run, "port", 40))
        (self.run / "view.bmp").write_bytes(b"BM")
        self.assertEqual(site_feedback.frame_path(self.run, "port", 40).name, "view.bmp")
        self.assertIsNone(site_feedback.frame_path(self.run, "original", 40))
        self.shot("port", 25)
        self.shot("original", 25, "oracle")
        self.assertEqual(site_feedback.frame_path(self.run, "port", 40).name, "poll-000025.ppm")
        self.assertEqual(site_feedback.frame_path(self.run, "original", 40).parent.name, "oracle")
        packed = self.run / "stream" / "port"
        packed.mkdir(parents=True)
        (packed / "30.jpg").write_bytes(b"x")
        self.assertEqual(site_feedback.frame_path(self.run, "port", 40).name, "30.jpg")


class FileTest(Fixture):
    def file(self, **over):
        body = {"run": "r1", "poll": 60, "category": "glitch", "text": "floor flickers", **over}
        return site_feedback.file(self.server, body, at=1000.5)

    def test_filing_writes_the_record_with_run_poll_category_text_inputs_and_both_frames(self):
        self.shot("port", 50)
        self.shot("original", 50, "oracle")
        record = self.file()
        where = self.runs / "feedback" / record["id"]
        self.assertEqual(json.loads((where / "feedback.json").read_text()), record)
        self.assertEqual(
            {k: record[k] for k in ("run", "poll", "category", "text", "at", "lane", "movie")},
            {
                "run": "r1",
                "poll": 60,
                "category": "glitch",
                "text": "floor flickers",
                "at": 1000.5,
                "lane": "owner",
                "movie": "boot-idle",
            },
        )
        self.assertEqual(record["frames"], {"port": "port.jpg", "original": "original.jpg"})
        self.assertEqual((where / "port.jpg").read_bytes(), b"JPEG:poll-000050.ppm")
        self.assertEqual(record["inputs"][-1]["poll"], 60)
        self.assertEqual(sorted(p.name for p in where.iterdir()), ["feedback.json", "original.jpg", "port.jpg"])

    def test_a_side_without_a_picture_is_null_and_writes_no_file(self):
        (self.run / "view.bmp").write_bytes(b"BM")
        record = self.file()
        self.assertEqual(record["frames"], {"port": "port.jpg", "original": None})
        self.assertFalse((self.runs / "feedback" / record["id"] / "original.jpg").exists())

    def test_the_id_is_stable_for_the_same_filing_and_differs_by_poll_and_time(self):
        self.assertEqual(self.file()["id"], self.file()["id"])
        self.assertNotEqual(self.file()["id"], self.file(poll=61)["id"])
        self.assertNotEqual(
            self.file()["id"],
            site_feedback.file(
                self.server, {"run": "r1", "poll": 60, "category": "glitch", "text": "floor flickers"}, at=2.0
            )["id"],
        )

    def test_an_unknown_run_is_refused_and_nothing_is_written(self):
        with self.assertRaisesRegex(ValueError, "^run: no run nope"):
            self.file(run="nope")
        self.assertFalse((self.runs / "feedback").exists())

    def test_filing_publishes_one_feedback_filed_event_that_wakes_the_stream(self):
        version, _ = self.server.feed.snapshot()
        record = self.file()
        after, changed = self.server.feed.since(version, 0)
        self.assertEqual(after, version + 1)
        self.assertEqual([e["kind"] for e in changed], ["feedback.filed"])
        self.assertEqual(changed[0]["subject"], record["id"])
        self.assertEqual(site_core.render(changed[0]), "owner flagged boot-idle at poll 60: glitch")
        self.assertEqual(changed[0]["fields"]["run"], "r1")

    def test_the_event_derives_from_the_record_alone(self):
        record = self.file()
        self.assertEqual(site_feedback.event_of(record), site_feedback.event_of(json.loads(json.dumps(record))))


class AnswerTest(Fixture):
    def test_post_creates_get_lists_newest_first_and_get_one_returns_the_record(self):
        first = site_feedback.answer(
            self.server, "POST", "", "", json.dumps({"run": "r1", "poll": 5, "category": "ui"}).encode()
        )
        self.assertEqual(first[0], HTTPStatus.CREATED)
        later = site_feedback.file(
            self.server, {"run": "r1", "poll": 9, "category": "other", "text": "t"}, at=first[1]["at"] + 5
        )
        status, body = site_feedback.answer(self.server, "GET", "", "", b"")
        self.assertEqual((status, [r["id"] for r in body["feedback"]]), (HTTPStatus.OK, [later["id"], first[1]["id"]]))
        self.assertEqual(site_feedback.answer(self.server, "GET", first[1]["id"], "", b"")[1], first[1])

    def test_bad_requests_and_missing_records_get_typed_errors(self):
        self.assertEqual(site_feedback.answer(self.server, "POST", "", "", b"{not json")[0], HTTPStatus.BAD_REQUEST)
        status, body = site_feedback.answer(self.server, "POST", "", "", b'{"run":"r1","poll":1,"category":"lag"}')
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertTrue(body["error"].startswith("category:"))
        self.assertEqual(site_feedback.answer(self.server, "GET", "abcdef012345", "", b"")[0], HTTPStatus.NOT_FOUND)
        self.assertEqual(site_feedback.answer(self.server, "GET", "../../etc", "", b"")[0], HTTPStatus.NOT_FOUND)
        self.assertEqual(site_feedback.answer(self.server, "DELETE", "x", "", b"")[0], HTTPStatus.METHOD_NOT_ALLOWED)

    def test_listing_republishes_held_flags_so_a_restarted_feed_shows_them(self):
        record = self.file_record()
        self.server.feed = site_core.Feed()
        site_feedback.answer(self.server, "GET", "", "", b"")
        self.assertEqual([e["subject"] for e in self.server.feed.snapshot()[1]], [record["id"]])

    def file_record(self):
        return site_feedback.file(self.server, {"run": "r1", "poll": 3, "category": "input"}, at=7.0)


class CommandTest(Fixture):
    def test_list_gives_typed_rows_without_inputs_and_show_gives_the_whole_record(self):
        record = site_feedback.file(self.server, {"run": "r1", "poll": 3, "category": "crashed", "text": "x"}, at=7.0)
        listed = site_feedback.command(self.runs, "list", None)
        self.assertEqual(listed["total"], 1)
        self.assertEqual(
            listed["feedback"][0],
            {
                "id": record["id"],
                "at": 7.0,
                "run": "r1",
                "poll": 3,
                "category": "crashed",
                "lane": "owner",
                "movie": "boot-idle",
            },
        )
        shown = site_feedback.command(self.runs, "show", record["id"])
        self.assertEqual({k: v for k, v in shown.items() if k != "folder"}, record)
        self.assertEqual(shown["folder"], str(self.runs / "feedback" / record["id"]))

    def test_show_refuses_a_missing_id_and_an_unknown_record(self):
        for ident in (None, "abcdef012345"):
            with self.assertRaises(DecompError):
                site_feedback.command(self.runs, "show", ident)

    def test_list_of_nothing_is_empty(self):
        self.assertEqual(site_feedback.command(self.runs, "list", None), {"total": 0, "feedback": []})


class RegionFilesTest(unittest.TestCase):
    def test_play_module_and_feedback_module_carry_no_game_names(self):
        root = Path(__file__).resolve().parents[1] / "src" / "abu_decomp"
        text = (root / "static" / "site" / "play.js").read_text() + (root / "site_feedback.py").read_text()
        for name in ("mario", "zelda", "ocarina", "banjo", "smash", "kart"):
            self.assertNotIn(name, text.lower())

    def test_the_region_appends_the_dialog_element_not_its_wrapper(self):
        text = (Path(__file__).resolve().parents[1] / "src" / "abu_decomp" / "static" / "site" / "play.js").read_text()
        self.assertIn("list, dialog.el)", text)
        self.assertIn('el.dataset.ready = "play"', text)

    def test_the_dialog_categories_are_the_contracts(self):
        self.assertEqual(
            site_core.contract()["feedback_categories"], ["crashed", "ui", "glitch", "audio", "input", "other"]
        )
