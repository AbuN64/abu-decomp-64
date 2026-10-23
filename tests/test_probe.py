import contextlib
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from abu_decomp import DecompError, events, probe
from abu_decomp.cli import main
from abu_decomp.config import load_profile

T0 = datetime(2026, 10, 18, 18, 0, tzinfo=UTC)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.seat = self.root / "seat.json"
        self.seat.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "t",
                    "events": {"path": "narration.jsonl"},
                    "probe": {"queue": "oracle-queue"},
                }
            )
        )
        self.profile = load_profile(self.seat)
        self.feed = self.root / "narration.jsonl"
        self.queue = self.root / "oracle-queue" / "requests"

    def request(self, minute=0, **extra):
        values = {
            "requester": "PORT-ASTRA-17",
            "address": "0x8025E1EC",
            "function": "func_8025E1EC_de",
            "question": "Which voices does the masked audio stop release?",
            "decomp_path": "src/AUDIO_2/8025E1EC.CPP",
            "match_state": "stand-in",
            "now": T0 + timedelta(minutes=minute),
            **extra,
        }
        return probe.make_request(self.profile, **values)

    def oracle_probe(self, story, status, message="5 checks, 0 differences"):
        """What Cake Taste's repro narration appends: the story becomes the PROBE payload."""
        events.append(self.feed, {"actor": "repro", "status": status, "kind": "PROBE", "message": message,
                                  "payload": story})  # fmt: skip

    def test_a_request_is_one_file_and_a_handoff_with_the_address(self):
        made = self.request()
        path = Path(made["path"])
        self.assertEqual(path.parent, self.queue)
        stored = json.loads(path.read_text())
        self.assertEqual(stored["id"], path.stem)
        self.assertEqual((stored["address"], stored["requester"]), ("0x8025e1ec", "PORT-ASTRA-17"))
        handoff = events.tail(self.feed, 5)[-1]
        self.assertEqual(
            (handoff["kind"], handoff["status"], handoff["actor"]), ("HANDOFF", "handoff", "PORT-ASTRA-17")
        )
        self.assertEqual(handoff["subject"], {"address": "0x8025e1ec", "function": "func_8025E1EC_de"})
        self.assertEqual(handoff["payload"]["request"], stored["id"])

    def test_round_trip_request_serve_probe_status(self):
        first = self.request(0)["request"]
        second = self.request(5, address="0x80253838", function="func_80253838_de")["request"]
        served = probe.serve(self.profile)["open"]
        self.assertEqual([row["id"] for row in served], [first["id"], second["id"]])  # oldest first
        story = served[0]["story"]
        self.assertEqual(story["request"], first["id"])
        self.assertEqual((story["owner_address"], story["match_state"]), ("0x8025e1ec", "stand-in"))
        self.oracle_probe(story, "started", story["question"])
        self.assertEqual(probe.status(self.profile, first["id"])["requests"][0]["state"], "running")
        self.oracle_probe(story, "info")
        row = probe.status(self.profile, first["id"])["requests"][0]
        self.assertEqual((row["state"], row["probes"]), ("answered", 2))
        self.assertEqual(row["answer"]["message"], "5 checks, 0 differences")
        self.assertEqual([item["id"] for item in probe.serve(self.profile)["open"]], [second["id"]])
        self.assertEqual(probe.status(self.profile)["counts"], {"answered": 1, "open": 1})

    def test_a_later_request_supersedes_an_earlier_one(self):
        old = self.request(0)["request"]
        new = self.request(1, question="Which voices, with the mask from scene 2?", supersedes=old["id"])["request"]
        rows = {row["id"]: row for row in probe.status(self.profile)["requests"]}
        self.assertEqual((rows[old["id"]]["state"], rows[old["id"]]["superseded_by"]), ("superseded", new["id"]))
        self.assertEqual(rows[new["id"]]["state"], "open")

    def test_bad_requests_are_refused(self):
        evidence = self.root / "receipt.json"
        evidence.write_text("{}")
        self.assertEqual(self.request(evidence=[evidence])["request"]["evidence"][0]["path"], str(evidence))
        for extra in (
            {"address": "8025E1EC"},
            {"question": "two\nlines"},
            {"supersedes": "20261018T180000Z-00000000"},
            {"evidence": [self.root / "missing.json"]},
        ):
            with self.assertRaises(DecompError):
                self.request(1, **extra)
        self.request(2)
        with self.assertRaises(DecompError):
            self.request(2)  # the same request at the same second already exists

    def test_public_commands_print_one_envelope(self):
        output = io.StringIO()
        argv = ["--profile", str(self.seat), "probe", "request", "--requester", "PORT-ASTRA-17", "--address",
                "0x8025E1EC", "--function", "f", "--question", "q?"]  # fmt: skip
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(argv), 0)
        request_id = json.loads(output.getvalue())["result"]["request"]["id"]
        for action in (["serve"], ["status", "--id", request_id]):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--profile", str(self.seat), "probe", *action]), 0)
            self.assertIn(request_id, output.getvalue())
