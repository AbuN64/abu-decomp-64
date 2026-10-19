import json
import tempfile
import unittest
from pathlib import Path

from abu_decomp.console.dashboard_sources import Sources
from abu_decomp.console.feed import append_event
from abu_decomp.console.human import block, sentence

FIXTURES = Path(__file__).parent / "fixtures" / "panes"
SUGGEST = [
    json.dumps({"op": "module", "subject": "src/GFX/A.CPP", "target": "code_802AB940"}),
    json.dumps({"op": "source", "subject": "code_802AB940", "target": "src/GFX/CODE_802AB940.CPP"}),
]


class Profile:
    def __init__(self, root):
        self.root = root

    def section(self, name):
        return {"path": "events.jsonl"}

    def resolve_path(self, value):
        return self.root / value


def attempt(n, matched, exact=False, lane="RW-SOL-A"):
    return {
        "op": "attempt",
        "function": "func_802BD430_de",
        "version": "de",
        "size_bytes": 1348,
        "attempt": n,
        "words": {"matched": matched, "target": 337, "built": 337},
        "exact": exact,
        "variants": 3,
        "first_difference": {"built": "addiu $v1", "target": "addiu $v0"},
        "refusals": ["a.CPP:12 score.compile: bad"] if n == 1 else [],
        "lane": lane,
    }


class ActivityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.profile = Profile(self.root)
        (self.root / "progress.json").write_bytes((FIXTURES / "progress.json").read_bytes())

    def add(self, actor, status, message, kind, **payload):
        append_event(self.profile, actor, status, message, kind=kind, payload=payload)

    def model(self):
        pane = {"progress": {"path": str(self.root / "progress.json"), "version": "us", "label": "RW"}}
        return Sources(self.root / "events.jsonl", title="DECOMP", config={"panes": {"activity": pane}}).read()

    def test_one_chronological_feed_with_lane_tags_and_the_functions_whole_history(self):
        self.add("RW-SOL-A", "info", "x", "MATCH", **attempt(1, 200))
        self.add("RW-SOL-B", "info", "x", "MATCH", **attempt(2, 315, lane="RW-SOL-B"))
        self.add(
            "repro",
            "passed",
            "probe",
            "PROBE",
            function="func_802BD430_de",
            checks=3,
            differences=0,
            result="3 checks, 0 differences",
        )
        self.add(
            "RW-SOL-A",
            "passed",
            "x",
            "MATCH",
            **{**attempt(3, 337, exact=True), "words": {"matched": 337, "target": 337}},
        )
        model = self.model()
        titles = [r.title for r in model.rows]
        self.assertEqual(
            titles,
            [
                "func_802BD430 (1,348 B) · attempt 1 · 200/337 words",
                "func_802BD430 (1,348 B) · attempt 2 · 315/337 words",
                "func_802BD430 · probed the original: 3 checks, 0 differences",
                "func_802BD430 (1,348 B) · attempt 3 · exact ✓",
            ],
        )
        self.assertEqual([r.tag for r in model.rows][:2], ["RW-SOL-A", "RW-SOL-B"])  # a tag, not a pane
        self.assertTrue(all(r.when for r in model.rows))
        details = model.rows[0].details
        self.assertIn("Trial history of func_802BD430", details)
        self.assertIn("attempt 2: 315 of 337 words matched", details)  # later trials are in every row's history
        self.assertIn("original-game probe: 3 checks, 0 differences", details)
        self.assertIn("first difference addiu $v1 vs original addiu $v0", details)
        self.assertIn("refused: a.CPP:12 score.compile: bad", details)
        self.assertIn("Expected: original: 337 words", details.replace("  ", " ").replace("Expected:  ", "Expected: "))
        self.assertTrue(model.footer.startswith("landed today"))

    def test_a_landing_says_it_went_through_recipe_maker_with_the_suggested_name(self):
        recipe = {"structure": {"ok": True, "moves": 5, "head": SUGGEST}}
        self.add(
            "land",
            "passed",
            "land 1 lane(s) at abc: 2 converted",
            "LAND",
            op="landed",
            functions=["func_1", "func_2"],
            gates={"check": True},
            lanes={"RW-SOL-B": {"status": "applied"}, "RW-SOL-C": {"status": "excluded"}},
            recipe=recipe,
        )
        row = self.model().rows[0]
        self.assertEqual(
            row.title, "Landed 2 functions → Recipe suggests name code_802AB940 in src/GFX/CODE_802AB940.CPP"
        )
        self.assertEqual(row.tag, "RW-SOL-B")
        self.assertIn("Went through Recipe Maker", row.details)
        self.assertIn("module: src/GFX/A.CPP -> code_802AB940", row.details)

    def test_old_failures_and_unrelated_events_are_not_rows(self):
        self.add("git", "info", "rage-ports abc1234 Select a thing", "LAND")
        self.add("cycle", "failed", "cycle build failed", "RUN-FAIL")
        rows = self.model().rows
        self.assertEqual([r.state for r in rows], ["stop"])
        self.assertIn("cycle stopped: cycle build failed", rows[0].title)


STAMP = "2026-10-18T12:00:00+00:00"


class HumanTests(unittest.TestCase):
    def test_sentences_name_the_operation_and_never_a_raw_kind(self):
        cases = [
            (
                {
                    "kind": "LAND",
                    "actor": "git",
                    "message": "ragewars-decomp e341f61 Fix 10 INCLUDE_ASM stand-ins in A by matching x as C",
                },
                "Landed 10 matched functions into ragewars-decomp (commit e341f61)",
            ),
            (
                {
                    "kind": "PROBE",
                    "actor": "repro",
                    "status": "passed",
                    "message": "m",
                    "payload": {"function": "func_8044ADC0", "checks": 111, "differences": 0},
                },
                "Probed func_8044ADC0 on the original game: 111 checks, 0 differences",
            ),
            (
                {"kind": "LAND", "actor": "git", "message": "rage-ports abc1234 Select original background"},
                "Committed to rage-ports (abc1234): Select original background",
            ),
            ({"kind": "RUN-FAIL", "actor": "cycle", "message": "build failed"}, "cycle stopped: build failed"),
            (
                {
                    "kind": "PROBE",
                    "actor": "repro",
                    "status": "started",
                    "message": "Observe the original behavior of func_9",
                    "payload": {"function": "func_9"},
                },
                "Probing func_9 on the original game",
            ),
        ]
        for event, expected in cases:
            self.assertEqual(sentence(event), expected)

    def test_build_reads_the_receipts_test_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "ctest.log"
            log.write_text("100% tests passed, 0 tests failed out of 150\n")
            receipt = root / "r.json"
            receipt.write_text(json.dumps({"steps": [{"name": "ctest", "log": str(log), "returncode": 0}]}))
            event = {
                "time": STAMP,
                "schema": 2,
                "actor": "build",
                "status": "passed",
                "message": "Committed build promoted",
                "evidence": str(receipt),
            }
            self.assertEqual(sentence(event), "Build passed: 150 of 150 tests")
            self.assertIn("Result:   PASS", block(event))
            self.assertIn("ctest ok", block(event))

    def test_blocks_read_like_a_test_with_expected_and_actual(self):
        event = {
            "time": STAMP,
            "schema": 2,
            "kind": "MATCH",
            "actor": "L",
            "status": "info",
            "message": "m",
            "payload": attempt(2, 315),
        }
        text = block(event)
        for label in ("Ran:", "Input:", "Expected:", "Actual:", "Result:", "Raw event:"):
            self.assertIn(label, text)
        self.assertIn("FAIL, not exact yet", text)
