"""The Runner site map region: flag bitmask, filter selection and colours (site_map, static/site/map.js)."""

import json
import subprocess
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from abu_decomp import site_core, site_map

ROOT = Path(__file__).resolve().parents[1] / "src" / "abu_decomp"
BIT = site_core.FILTER_BITS
COLS = ["name", "rom", "size", "state", "file", "reach", "foundation", "behaviour"]
ROWS = [
    ["fn_a", 0, 64, "named", 0, "both", 0, "behaves"],
    ["fn_b", 0, 32, "exact", 0, "port", 0, "differs-args"],
    ["fn_c", 0, 16, "asm", 0, "oracle", 0, ""],
    ["fn_d", 0, 8, "fuzzy", 0, "none", 0, "missing"],
]


def bits(*keys: str) -> int:
    return sum(BIT[key] for key in keys)


class FlagsTest(unittest.TestCase):
    def test_every_filter_has_its_own_bit(self) -> None:
        self.assertEqual(len(set(BIT.values())), len(BIT))
        self.assertTrue(all(bit & (bit - 1) == 0 for bit in BIT.values()))

    def test_state_reach_and_behaviour_set_their_bits(self) -> None:
        self.assertEqual(site_map.flags_of("named", "both", "behaves"), bits("named", "exercised", "behaves"))
        self.assertEqual(site_map.flags_of("exact", "port", "differs-args"), bits("exact", "exercised", "differs"))
        self.assertEqual(site_map.flags_of("exact", "port", "differs-result"), bits("exact", "exercised", "differs"))
        self.assertEqual(site_map.flags_of("asm", "oracle", ""), bits("asm", "original-only"))

    def test_fuzzy_unreached_and_unjudged_set_nothing(self) -> None:
        self.assertEqual(site_map.flags_of("fuzzy", "none", "missing"), 0)
        self.assertEqual(site_map.flags_of("fuzzy", "none", "port-only"), 0)

    def test_feedback_and_stream_bits(self) -> None:
        self.assertEqual(site_map.flags_of("asm", "none", "", feedback=True), bits("asm", "feedback"))
        self.assertEqual(site_map.flags_of("asm", "none", "", streamed=True), bits("asm", "streamed"))

    def test_reach_bitset_names_its_rows_and_refuses_a_wrong_size(self) -> None:
        self.assertEqual(site_map.reach_rows(bytes([0b00000101, 0b00000010]), 10), {0, 2, 9})
        self.assertEqual(site_map.reach_rows(bytes([0xFF]), 10), set())


class SelectionTest(unittest.TestCase):
    def test_all_unchecked_is_all_dark(self) -> None:
        for flags in (0, bits("named"), (1 << len(BIT)) - 1):
            self.assertFalse(site_map.lit(flags, set()))

    def test_a_box_lights_when_any_checked_filter_selects_it(self) -> None:
        flags = bits("named", "behaves")
        self.assertTrue(site_map.lit(flags, {"named"}))
        self.assertTrue(site_map.lit(flags, {"asm", "behaves"}))
        self.assertFalse(site_map.lit(flags, {"asm", "differs"}))

    def test_an_unknown_filter_is_refused(self) -> None:
        with self.assertRaises(KeyError):
            site_map.lit(1, {"nope"})


class ColourTest(unittest.TestCase):
    def test_progress_colours_by_state(self) -> None:
        for state in ("asm", "fuzzy", "exact", "named"):
            self.assertEqual(site_map.colour_key(state, 0), state)
        self.assertEqual(site_map.colour_key("unknown", 0), "asm")

    def test_a_judged_call_overrides_the_state_and_differs_wins(self) -> None:
        self.assertEqual(site_map.colour_key("named", bits("behaves")), "behaves")
        self.assertEqual(site_map.colour_key("asm", bits("differs")), "differs")
        self.assertEqual(site_map.colour_key("named", bits("behaves", "differs")), "differs")

    def test_every_colour_key_has_a_shell_variable(self) -> None:
        html = (ROOT / "runner.html").read_text()
        for variable in site_map.COLOURS.values():
            self.assertIn(f"{variable}:", html)


class BuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self.tmp.name)
        site = self.runs / "site"
        site.mkdir()
        (site / "fns.abc.json").write_text(json.dumps({"ref": "r1", "cols": COLS, "rows": ROWS}))
        (site / "manifest.json").write_text(json.dumps({"fns": "fns.abc.json"}))
        self.server = SimpleNamespace(cfg={"runs": self.runs}, site=site)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def reach(self, run: str, rows: list[int], *, stream: bool = False) -> None:
        folder = self.runs / "plays" / run
        folder.mkdir(parents=True)
        body = bytearray((len(ROWS) + 7) // 8)
        for i in rows:
            body[i >> 3] |= 1 << (i & 7)
        (folder / "reach.bin").write_bytes(bytes(body))
        if stream:
            (folder / "stream").mkdir()
            (folder / "stream" / "index.json").write_text("{}")

    def feedback(self, run: str) -> None:
        folder = self.runs / "feedback" / f"fb-{run}"
        folder.mkdir(parents=True)
        (folder / "feedback.json").write_text(json.dumps({"run": run}))

    def rows(self) -> list[list]:
        status, body = site_map.answer(self.server, "GET", "", "", b"")
        self.assertEqual(status, HTTPStatus.OK)
        return body["rows"]

    def test_rows_are_name_size_state_flags_in_rom_order(self) -> None:
        self.assertEqual(
            self.rows(),
            [
                ["fn_a", 64, "named", bits("named", "exercised", "behaves")],
                ["fn_b", 32, "exact", bits("exact", "exercised", "differs")],
                ["fn_c", 16, "asm", bits("asm", "original-only")],
                ["fn_d", 8, "fuzzy", 0],
            ],
        )

    def test_feedback_and_streamed_flags_come_from_the_run_reach(self) -> None:
        self.reach("p1", [1, 3])
        self.reach("p2", [0], stream=True)
        self.feedback("p1")
        rows = {row[0]: row[3] for row in self.rows()}
        self.assertTrue(rows["fn_b"] & BIT["feedback"] and rows["fn_d"] & BIT["feedback"])
        self.assertFalse(rows["fn_a"] & BIT["feedback"])
        self.assertTrue(rows["fn_a"] & BIT["streamed"])
        self.assertFalse(rows["fn_b"] & BIT["streamed"])

    def test_the_answer_is_deterministic_and_its_version_moves_with_the_rows(self) -> None:
        first = site_map.answer(self.server, "GET", "", "", b"")[1]
        again = site_map.answer(self.server, "GET", "", "", b"")[1]
        self.assertEqual(json.dumps(first), json.dumps(again))
        self.reach("p1", [0])
        self.feedback("p1")
        self.assertNotEqual(first["version"], site_map.answer(self.server, "GET", "", "", b"")[1]["version"])
        self.assertEqual(first["filters"], [{"key": k, "bit": b} for k, b in BIT.items()])

    def test_no_baked_site_answers_503_and_other_paths_404(self) -> None:
        (self.server.site / "manifest.json").unlink()
        self.assertEqual(site_map.answer(self.server, "GET", "", "", b"")[0], HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(site_map.answer(self.server, "GET", "x", "", b"")[0], HTTPStatus.NOT_FOUND)
        self.assertEqual(site_map.answer(self.server, "POST", "", "", b"")[0], HTTPStatus.NOT_FOUND)


SCRIPT = """
import * as m from %r;
const filters = %s;
const bit = Object.fromEntries(filters.map(f => [f.key, f.bit]));
const out = {
  none: m.isLit(0xffff, m.maskOf(filters, new Set())),
  named: m.isLit(bit.named | bit.behaves, m.maskOf(filters, new Set(["named"]))),
  other: m.isLit(bit.named, m.maskOf(filters, new Set(["asm", "differs"]))),
  colours: [
    m.colourKey("named", 0, bit),
    m.colourKey("exact", bit.behaves, bit),
    m.colourKey("asm", bit.behaves | bit.differs, bit),
    m.colourKey("zzz", 0, bit),
  ],
  layout: m.layout(25, 12 * m.CELL + 2),
  lit: [
    m.litParam(filters, new Set(["differs", "named"])),
    [...m.litFrom("#/?lit=asm,zzz,exact", filters)],
    [...m.litFrom("#/fn/x", filters)],
  ],
  box: m.boxAt(2 * m.CELL + 1, m.CELL + 1, 12),
};
console.log(JSON.stringify(out));
"""


class PageLogicTest(unittest.TestCase):
    """map.js's pure functions run under node against the contract's filters (the same rules as site_map)."""

    def test_filters_and_colours_in_the_page_match_the_server(self) -> None:
        filters = site_core.contract()["filters"]
        script = SCRIPT % ((ROOT / "static" / "site" / "map.js").as_uri(), json.dumps(filters))
        run = subprocess.run(
            ["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True, timeout=60
        )
        got = json.loads(run.stdout)
        self.assertFalse(got["none"])
        self.assertTrue(got["named"])
        self.assertFalse(got["other"])
        self.assertEqual(got["colours"], ["named", "behaves", "differs", "asm"])
        server = [
            site_map.colour_key("named", 0),
            site_map.colour_key("exact", bits("behaves")),
            site_map.colour_key("asm", bits("behaves", "differs")),
            site_map.colour_key("zzz", 0),
        ]
        self.assertEqual(got["colours"], server)
        self.assertEqual(got["layout"], {"cols": 12, "rows": 3, "height": 18})
        self.assertEqual(got["lit"], ["named,differs", ["exact", "asm"], []])
        self.assertEqual(got["box"], 12 + 2)


if __name__ == "__main__":
    unittest.main()
