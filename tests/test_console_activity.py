import json
import tempfile
import unittest
from pathlib import Path

from abu_decomp.console.dashboard_sources import Sources
from abu_decomp.console.feed import append_event
from abu_decomp.console.human import block, collapse_probes, sentence

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
                "func_802BD430: 59.3% match",
                "func_802BD430: 93.4% match",
                "func_802BD430: same (3/3)",
                "func_802BD430: CRACKED!",
            ],
        )
        self.assertEqual([r.tag for r in model.rows][:2], ["RW-SOL-A", "RW-SOL-B"])  # a tag, not a pane
        self.assertTrue(all(r.when for r in model.rows))
        details = model.rows[0].details
        self.assertIn("trail      attempt 2: 93.4% match", details)  # later trials are in the row's trail
        self.assertIn("built addiu $v1 ≠ original addiu $v0", details)
        self.assertIn("refused    a.CPP:12 score.compile: bad", details)
        self.assertIn("expected   337 words", details)
        self.assertEqual(model.rows[3].state, "cracked")
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
        self.assertEqual(row.title, "landed 2 fn → src/GFX/CODE_802AB940.CPP")
        self.assertEqual(row.tag, "RW-SOL-B")
        self.assertIn("module: src/GFX/A.CPP -> code_802AB940", row.details)

    def test_old_failures_and_unrelated_events_are_not_rows(self):
        self.add("git", "info", "rage-ports abc1234 Select a thing", "LAND")
        self.add("cycle", "failed", "cycle build failed", "RUN-FAIL")
        rows = self.model().rows
        self.assertEqual([r.state for r in rows], ["stop"])
        self.assertIn("cycle: cycle build failed", rows[0].title)


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
                "landed 10 fn → ragewars-decomp e341f61",
            ),
            (
                {
                    "kind": "PROBE",
                    "actor": "repro",
                    "status": "passed",
                    "message": "m",
                    "payload": {"function": "func_8044ADC0", "checks": 111, "differences": 0},
                },
                "func_8044ADC0: same (111/111)",
            ),
            (
                {"kind": "LAND", "actor": "git", "message": "rage-ports abc1234 Select original background"},
                "rage-ports abc1234: Select original background",
            ),
            ({"kind": "RUN-FAIL", "actor": "cycle", "message": "build failed"}, "cycle: build failed"),
            (
                {
                    "kind": "PROBE",
                    "actor": "repro",
                    "status": "started",
                    "message": "Observe the original behavior of func_9",
                    "payload": {"function": "func_9"},
                },
                "func_9: probing",
            ),
        ]
        for event, expected in cases:
            self.assertEqual(sentence(event), expected)

    def test_probe_without_counts_is_not_read_as_none_checks(self):
        event = {
            "kind": "PROBE",
            "status": "passed",
            "message": "answered",
            "payload": {"function": "f", "checks": None},
        }
        self.assertEqual(sentence(event), "answered")

    def test_probe_start_rows_fold_into_the_probes_result(self):
        def probe(status, name, **payload):
            return {"kind": "PROBE", "status": status, "message": name, "payload": {"function": name, **payload}}

        events = [
            probe("started", "f1"),
            probe("started", "f2"),
            probe("passed", "f1", checks=2, differences=0),
            probe("started", "f1"),
            probe("started", "f1"),
        ]
        kept = collapse_probes(events)
        self.assertEqual(len(kept), 3)
        self.assertEqual(kept[-1]["status"], "started")
        self.assertEqual(kept[0]["message"], "f2")

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
            self.assertEqual(sentence(event), "build ✓ 150/150")
            self.assertIn("ctest=ok", block(event))

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
        for label in ("expected", "actual", "first diff", "variants"):
            self.assertIn(label, text)
        self.assertNotIn("Ran", text)  # values only, no prose
