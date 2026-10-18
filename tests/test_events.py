import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from abu_decomp.cli import main

BASE = {"time": "2026-10-18T00:00:00+00:00", "actor": "x", "status": "info", "message": "m", "evidence": None}


def run(argv):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = main(argv)
    return code, json.loads(output.getvalue())


class EventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.feed = self.root / "narration.jsonl"
        (self.root / "seat.json").write_text(
            json.dumps({"schema_version": 1, "name": "seat", "events": {"path": str(self.feed)}})
        )
        self.seat = ["--profile", str(self.root / "seat.json")]

    def lines(self, *values):
        self.feed.write_text("".join(json.dumps(value) + "\n" for value in values))

    def test_append_goes_through_the_cake_taste_writer_and_validates(self):
        lane = json.dumps({"name": "T1", "kind": "tooling", "model": "m", "track": "official"})
        code, envelope = run(
            [
                *self.seat,
                "event",
                "append",
                "--actor",
                "T1",
                "--status",
                "started",
                "--message",
                "launched",
                "--kind",
                "LANE",
                "--lane",
                lane,
            ]
        )
        self.assertEqual(code, 0, envelope)
        written = json.loads(self.feed.read_text())
        self.assertEqual((written["schema"], written["kind"], written["lane"]["name"]), (2, "LANE", "T1"))
        code, envelope = run([*self.seat, "event", "validate"])
        self.assertEqual((code, envelope["result"]["events"], envelope["findings"]), (0, 1, []))

    def test_append_refuses_retired_and_schema_1_kinds(self):
        for kind in ("RUNG", "DECOMP"):
            with self.subTest(kind=kind):
                code, envelope = run(
                    [
                        *self.seat,
                        "event",
                        "append",
                        "--actor",
                        "a",
                        "--status",
                        "info",
                        "--message",
                        "m",
                        "--kind",
                        kind,
                    ]
                )
                self.assertEqual(code, 1)
                self.assertIn(envelope["findings"][0]["key"], {"events.kind", "events.invalid"})
        self.assertFalse(self.feed.exists())

    def test_append_records_evidence_hash(self):
        proof = self.root / "proof.txt"
        proof.write_text("x")
        code, _ = run(
            [
                *self.seat,
                "event",
                "append",
                "--actor",
                "a",
                "--status",
                "passed",
                "--message",
                "m",
                "--kind",
                "DECISION",
                "--evidence",
                str(proof),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(self.feed.read_text())["evidence"]["sha256"]), 64)

    def test_tail_maps_writer_schema_1_kinds_and_leaves_rung_unmapped(self):
        self.lines(
            {**BASE, "schema": 1, "kind": "RUNG"},
            {**BASE, "schema": 1, "kind": "PORT-FAIL"},
            {**BASE, "schema": 1, "kind": "COMMIT"},
            {**BASE, "schema": 2, "kind": "DECISION"},
        )
        _, envelope = run([*self.seat, "event", "tail"])
        self.assertEqual(
            [row["canonical_kind"] for row in envelope["result"]["events"]],
            ["RUNG", "RUN-FAIL", "LAND", "DECISION"],
        )
        code, envelope = run([*self.seat, "event", "tail", "--kinds", "DECISION"])
        self.assertEqual((code, len(envelope["result"]["events"])), (0, 1))
        code, envelope = run([*self.seat, "event", "tail", "--kinds", "RUNG"])
        self.assertEqual((code, envelope["findings"][0]["key"]), (1, "events.kind"))

    def test_validate_refuses_rung_in_any_schema_and_reports_bad_lines(self):
        self.lines({**BASE, "schema": 1, "kind": "PORT-FAIL"}, {**BASE, "schema": 1, "kind": "RUNG"})
        with self.feed.open("a") as stream:
            stream.write("{bad\n")
        code, envelope = run([*self.seat, "event", "validate"])
        self.assertEqual(code, 1)
        self.assertEqual([row["line"] for row in envelope["findings"]], [2, 3])
        self.assertIn("unknown event kind 'RUNG'", envelope["findings"][0]["message"])

    def test_validate_path_option_needs_no_profile(self):
        self.lines({**BASE, "schema": 2})
        code, envelope = run(["event", "validate", "--path", str(self.feed)])
        self.assertEqual((code, envelope["result"]["events"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
