import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp import DecompError, narrate


def variant(name, matched, target=337, exact=False, findings=None):
    result = {
        "score": matched / target,
        "exact": exact,
        "members": ["func_802BD430_de"],
        "version": "de",
        "unit": "src/SDK/PRINTF/802C2520.CPP",
        "words": {"built": target, "matched": matched, "target": target},
        "shift": {"first_difference": {"built": "addiu $v1, $v0, 0x4", "target": "addiu $v0, $v0, 0x4", "index": 7}},
    }
    return {"file": f"/c/{name}/func_802BD430_de.c", "result": result, "findings": findings}


class AttemptTests(unittest.TestCase):
    def test_attempt_names_function_size_counter_words_and_first_difference(self):
        with tempfile.TemporaryDirectory() as directory:
            feed = Path(directory) / "events.jsonl"
            refusal = {"path": "a.CPP", "line": 12, "key": "score.compile", "message": "bad"}
            body = {"variants": [variant("a", 200), variant("b", 315, findings=[refusal])]}
            first = narrate.attempt_payload(body, feed, "RW-SOL-A")
            second = narrate.attempt_payload(body, feed, "RW-SOL-A")
            self.assertEqual((first["attempt"], second["attempt"]), (1, 2))  # counted per function
            self.assertEqual((first["size_bytes"], first["words"]["matched"], first["variants"]), (1348, 315, 2))
            self.assertEqual(first["first_difference"]["built"], "addiu $v1, $v0, 0x4")
            self.assertEqual(first["refusals"], ["a.CPP:12 score.compile: bad"])
            self.assertEqual(
                narrate.attempt_message(second), "Tried func_802BD430 (1,348 B), attempt 2: 315 of 337 words match"
            )
            exact = narrate.attempt_payload({"variants": [variant("c", 337, exact=True)]}, feed, "x")
            self.assertTrue(narrate.attempt_message(exact).endswith("exact match"))
            self.assertIsNone(narrate.attempt_payload({"variants": []}, feed, "x"))

    def test_submissions_carry_the_stored_files_and_refusals(self):
        body = {
            "submissions": [
                {"member": "func_1", "exact": True, "score": 1.0, "findings": [], "refused": False},
                {"member": "func_2", "exact": False, "refused": True, "findings": [{"key": "k", "message": "no"}]},
            ],
            "drain": {"landed": [{"member": "func_1", "files": ["src/a.CPP"]}]},
        }
        one, two = narrate.submit_payloads(body, "L")
        self.assertEqual(narrate.submit_message(one), "Submitted func_1: exact, stored in src/a.CPP")
        self.assertEqual(narrate.submit_message(two), "Submitted func_2: refused (k: no)")


class EmitTests(unittest.TestCase):
    def emit(self, tool, args, document, directory):
        written = []
        feed = Path(directory) / "events.jsonl"
        with (
            mock.patch("abu_decomp.events.events_path", return_value=feed),
            mock.patch("abu_decomp.events.append", side_effect=lambda path, record: written.append(record)),
        ):
            narrate.emit(object(), tool, args, document, Path("/lanes/RW-SOL-A/rw"))
        return written

    def test_score_and_submit_emit_one_event_each_and_other_calls_none(self):
        with tempfile.TemporaryDirectory() as directory:
            score = self.emit("unbake", ["score", "x.c"], {"result": {"variants": [variant("a", 5)]}}, directory)
            self.assertEqual(
                [(e["kind"], e["actor"], e["payload"]["op"]) for e in score], [("MATCH", "RW-SOL-A", "attempt")]
            )
            sub = self.emit(
                "unbake",
                ["--x", "submit", "f.c"],
                {"result": {"submissions": [{"member": "f", "exact": True}]}},
                directory,
            )
            self.assertEqual((sub[0]["status"], sub[0]["payload"]["op"]), ("passed", "submit"))
            self.assertEqual(self.emit("unbake", ["report"], {"result": {}}, directory), [])
            self.assertEqual(self.emit("recipe", ["score"], {"result": {}}, directory), [])

    def test_a_failing_writer_never_breaks_the_call(self):
        with mock.patch("abu_decomp.events.events_path", side_effect=DecompError("config.schema", "no events")):
            narrate.emit(object(), "unbake", ["score"], {"result": {"variants": [variant("a", 1)]}}, None)


class BehaveTests(unittest.TestCase):
    def test_behave_events_use_the_stable_schema(self):
        written = []
        with (
            mock.patch("abu_decomp.events.events_path", return_value=Path("e")),
            mock.patch("abu_decomp.events.append", side_effect=lambda path, record: written.append(record)),
        ):
            narrate.behave_event(
                object(), function="func_1", verdict="same", checks=4, differences=0, inputs={"a0": 1}, lane="L"
            )
            with self.assertRaises(DecompError):
                narrate.behave_event(object(), function="func_1", verdict="maybe")
        record = written[0]
        self.assertEqual((record["kind"], record["status"], record["payload"]["op"]), ("PROBE", "passed", "behave"))
        self.assertIn("same behaviour", record["message"])
        json.dumps(record)


class LaneOfTests(unittest.TestCase):
    def test_behave_without_a_project_names_the_lane_directory_it_runs_in(self):
        with tempfile.TemporaryDirectory() as directory:
            deep = Path(directory) / "lanes" / "PORT-SOL" / "cache"
            deep.mkdir(parents=True)
            with mock.patch.dict("os.environ", {}, clear=False) as env:
                env.pop("DECOMP_LANE", None)
                with mock.patch("pathlib.Path.cwd", return_value=deep):
                    self.assertEqual(narrate.lane_of(None), "PORT-SOL")
                with mock.patch("pathlib.Path.cwd", return_value=Path(directory)):
                    self.assertEqual(narrate.lane_of(None), "unknown")
