import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from abu_decomp.console.dashboard_sources import Sources
from abu_decomp.console.feed import append_event
from abu_decomp.console.matching import document_of, envelope_of, finding_text, score_words

FIXTURES = Path(__file__).parent / "fixtures" / "panes"


def variant(name, matched, target=337, exact=False, findings=None):
    result = {
        "score": matched / target,
        "exact": exact,
        "words": {"built": target, "matched": matched, "target": target},
    }
    return {"file": f"/lane/cand/{name}/func_802BD430_de.c", "ok": not findings, "result": result, "findings": findings}


def envelope(variants, wrapped=True, findings=()):
    document = {"findings": list(findings), "ok": True, "result": {"variants": variants, "proofs": [], "missing": []}}
    return {"findings": [], "result": {"argv": [], "document": document}} if wrapped else document


def write_log(path, document, noise="score started\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(noise + json.dumps(document) + "\nslowest:\n")


class FeedProfile:
    def __init__(self, root):
        self.root = root

    def section(self, name):
        return {"path": "events.jsonl"}

    def resolve_path(self, value):
        return self.root / value


class ParsingTests(unittest.TestCase):
    def test_a_wrapped_and_a_raw_envelope_give_the_same_document(self):
        variants = [variant("a", 294)]
        wrapped, raw = envelope(variants), envelope(variants, wrapped=False)
        self.assertEqual(document_of(wrapped)["result"]["variants"], document_of(raw)["result"]["variants"])
        self.assertIsNone(envelope_of("setup done\n" + json.dumps({"findings": [], "result": {"steps": 1}}) + "\n"))

    def test_score_is_the_best_variants_words_matched_out_of_target(self):
        self.assertEqual(score_words([variant("a", 200), variant("b", 294)]), "294 of 337 words matched, best of 2")
        self.assertEqual(score_words([variant("a", 337, exact=True)]), "exact, all 337 words matched")
        self.assertEqual(score_words([{"file": "x.c", "findings": []}]), "1 variants, none scored")

    def test_finding_text_is_short_with_path_and_line(self):
        found = {"path": "src/a/b.c", "line": 41, "key": "native.exit", "message": "compilation terminated " * 8}
        text = finding_text(found)
        self.assertTrue(text.startswith("src/a/b.c:41 native.exit: compilation terminated"))
        self.assertLessEqual(len(text), 78)


class PaneTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.lanes = self.root / "lanes"
        data = (FIXTURES / "progress.json").read_bytes()
        (self.root / "progress.json").write_bytes(data)

    def lane(self, name, *, score=None, done=(), age=0):
        lane = self.lanes / name
        (lane / "logs").mkdir(parents=True)
        (lane / "done.jsonl").write_text("".join(json.dumps(row) + "\n" for row in done))
        if score is not None:
            write_log(lane / "logs" / "run1-score.log", score)
        stamp = time.time() - age
        for path in lane.rglob("*"):
            if path.is_file():
                os.utime(path, (stamp, stamp))
        return lane

    def read(self, **extra):
        pane = {
            "lanes": str(self.lanes / "RW-*"),
            "progress": {"path": str(self.root / "progress.json"), "version": "us", "label": "RW"},
        }
        sources = Sources(self.root / "events.jsonl", title="DECOMP", config={"panes": {"matching": {**pane, **extra}}})
        return sources.read()

    def test_one_block_per_active_lane_names_function_score_refusal_and_outcome(self):
        refusal = {
            "path": "src/SDK/PRINTF/802C2520.CPP",
            "line": 12,
            "key": "native.exit",
            "message": "compilation terminated",
            "blocking": True,
        }
        self.lane(
            "RW-A",
            score=envelope([variant("v1", 294, findings=[refusal]), variant("v2", 270)]),
            done=[
                {"name": "func_1_de", "outcome": "nonexact", "bytes": 12},
                {"name": "func_2_de", "outcome": "exact", "bytes": 8},
            ],
        )
        append_event(
            FeedProfile(self.root),
            "git",
            "info",
            "ragewars-decomp abc1234 Fix 2 INCLUDE_ASM stand-ins by matching func_2_de as C",
            kind="LAND",
        )
        model = self.read()
        row = next(r for r in model.rows if r.key == "match-RW-A")
        self.assertTrue(row.open)
        self.assertIn("RW-A func_802BD430_de 294 of 337 words matched, best of 2", row.title)
        self.assertIn("refusal  src/SDK/PRINTF/802C2520.CPP:12 native.exit: compilation terminated", row.details)
        self.assertIn("outcome  func_2_de exact landed 8 B", row.details)
        self.assertIn("trying   func_802BD430_de", row.details)
        self.assertEqual(model.heading, "DECOMP · 1 active lanes")
        self.assertTrue(model.footer.startswith("landed today 2 · RW us functions "), model.footer)
        self.assertIn("code ", model.footer)

    def test_a_quiet_lane_is_listed_as_idle_and_no_lane_is_hardcoded(self):
        self.lane("RW-A", score=envelope([variant("v", 5)]), done=[])
        self.lane("RW-B", score=envelope([variant("v", 5)]), done=[], age=7200)
        self.lane("OTHER", score=envelope([variant("v", 5)]), done=[])
        rows = {r.key: r for r in self.read(active_seconds=600).rows}
        self.assertIn("match-RW-A", rows)
        self.assertNotIn("match-OTHER", rows)
        self.assertEqual(rows["match-idle"].title, "idle: RW-B")

    def test_a_lane_without_a_score_log_still_shows_its_outcome(self):
        self.lane("RW-A", done=[{"name": "func_9_de", "outcome": "blocked-tool"}])
        row = next(r for r in self.read().rows if r.key == "match-RW-A")
        self.assertIn("no score yet", row.title)
        self.assertIn("outcome  func_9_de blocked-tool", row.details)
        self.assertIn("refusal  none", row.details)

    def test_no_lane_directories_and_a_missing_progress_file_say_so(self):
        model = self.read(progress={"path": str(self.root / "gone.json"), "version": "us"})
        self.assertEqual([r.key for r in model.rows], ["match-none"])
        self.assertIn("progress unavailable", model.footer)
