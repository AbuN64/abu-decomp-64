import io
import json
import tempfile
import unittest
from pathlib import Path

from abu_decomp import DecompError
from abu_decomp.config import load_profile
from abu_decomp.console import hud


def attempt(time, matched, exact=False, function="func_8000_de"):
    return {
        "schema": 2,
        "time": time,
        "actor": "lane",
        "kind": "MATCH",
        "status": "passed",
        "message": "attempt",
        "payload": {
            "op": "attempt",
            "function": function,
            "version": "de",
            "exact": exact,
            "words": {"matched": matched, "target": 200, "built": 200},
        },
    }


class HudTests(unittest.TestCase):
    def profile(self, hud_object):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        document = {
            "schema_version": 1,
            "name": "t",
            "events": {"path": "events.jsonl"},
            "tiles": {
                "layout": [["a", "b"], ["c", "d"]],
                "roles": {"a": {"hud": hud_object}, "c": {"console": {"title": "C"}}},
            },
        }
        (root / "seat.json").write_text(json.dumps(document))
        return load_profile(root / "seat.json"), root

    def feed(self, root, *events):
        (root / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))

    def lines(self, hud_object, *events):
        profile, root = self.profile(hud_object)
        self.feed(root, *events)
        out = io.StringIO()
        hud.run_hud(profile, "a", out=out, iterations=1)
        return out.getvalue()

    def test_newest_attempt_as_a_value(self):
        text = self.lines({}, attempt("2026-10-18T10:00:00+00:00", 100), attempt("2026-10-18T10:05:00+00:00", 150))
        self.assertIn("func_8000: 75.0% match", text)
        self.assertNotIn("50.0", text)

    def test_cracked_is_bold_green(self):
        text = self.lines({}, attempt("2026-10-18T10:00:00+00:00", 200, exact=True))
        self.assertIn(f"func_8000: {hud.BOLD_GREEN}CRACKED!{hud.RESET}", text)

    def test_quiet_feed_prints_an_empty_strip(self):
        self.assertNotIn("match", self.lines({}))

    def test_unknown_hud_is_refused(self):
        profile, _ = self.profile({})
        with self.assertRaises(DecompError):
            hud.run_hud(profile, "c", out=io.StringIO(), iterations=1)

    def test_line_prints_on_the_last_row_of_the_tile_terminal(self):
        text = self.lines({}, attempt("2026-10-18T10:00:00+00:00", 100))
        self.assertIn("\033[24;1Hfunc_8000: 50.0% match", text)

    def test_escape_sequences_are_not_cut(self):
        text = "x" * 200 + hud.BOLD_GREEN + "y" + hud.RESET
        self.assertTrue(hud.visible(text).endswith(hud.RESET))
