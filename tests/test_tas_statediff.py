"""Game state, port against the original, per region (tas_statediff and shim/tas_state.h). Runs no game."""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from abu_decomp import statetypes
from abu_decomp import tas_statediff as sd

HARNESS = Path(__file__).parent / "fixtures" / "tas_state_harness.c"
BASE = 0x80000000


def word_swizzle(image: bytes) -> bytes:
    """A big-endian image as the original's RDRAM holds it: every 32-bit word in host (little-endian) order."""
    out = bytearray(image)
    for i in range(0, len(out), 4):
        out[i : i + 4] = out[i : i + 4][::-1]
    return bytes(out)


class HeaderTests(unittest.TestCase):
    """Both sides must read one logical state from their own byte order, and agree on hashes and dump."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("gcc"):
            raise unittest.SkipTest("no gcc")
        cls.tmp = Path(tempfile.mkdtemp())
        for name, flags in (("port", []), ("original", ["-DTS_ORACLE"])):
            built = subprocess.run(
                ["gcc", "-O1", "-Wall", "-Werror", *flags, "-o", str(cls.tmp / name), str(HARNESS)],
                capture_output=True,
                text=True,
            )
            if built.returncode:
                raise AssertionError(built.stderr)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    SPEC = (
        "ram 1000\ndynamic 80100000 80800000\nregion A\nsymbol a1\nrun 80000010 b 3\nrun 80000014 h 2\n"
        "symbol a2\nrun 80000020 w 2\nregion B\nsymbol b1\nrun 80000030 d 1\nrun 80000040 p 3\n"
        "symbol b2\nrun 80000050 b 1\n"
    )
    #  (address, kind, logical value)
    LOGICAL: ClassVar[list] = [
        (0x10, "b", 0x11),
        (0x11, "b", 0xEE),
        (0x12, "b", 0x33),
        (0x14, "h", 0xBEEF),
        (0x16, "h", 0x1234),
        (0x20, "w", 0xDEADBEEF),
        (0x24, "w", 0x01020304),
        (0x30, "d", 0x0102030405060708),
        (0x40, "p", 0),
        (0x44, "p", 0x80200000),
        (0x48, "p", 0x80010000),
        (0x50, "b", 0x7F),
    ]
    EXPECTED_DUMP = bytes.fromhex("11ee33beef1234deadbeef0102030401020304050607080000000080200000000000017f")

    def images(self, logical):
        port, big = bytearray(0x1000), bytearray(0x1000)
        for off, kind, value in logical:
            if kind == "b":
                port[off] = big[off] = value
            elif kind in "hwp":
                n = {"h": 2, "w": 4, "p": 4}[kind]
                port[off : off + n] = value.to_bytes(n, "little")
                big[off : off + n] = value.to_bytes(n, "big")
            else:
                port[off : off + 8] = value.to_bytes(8, "little")
                big[off : off + 8] = value.to_bytes(8, "big")
        return bytes(port), word_swizzle(bytes(big))

    def run_side(self, side: str, image: bytes, after: bytes | None = None):
        (self.tmp / "spec").write_text(self.SPEC)
        (self.tmp / "ram").write_bytes(image)
        argv = [str(self.tmp / side), str(self.tmp / "spec"), str(self.tmp / "ram"), str(self.tmp / "dump")]
        if after is not None:
            (self.tmp / "ram2").write_bytes(after)
            argv.append(str(self.tmp / "ram2"))
        out = subprocess.run(argv, capture_output=True, text=True, check=True)
        return out.stdout.splitlines(), (self.tmp / "dump").read_bytes()

    def test_both_byte_orders_give_one_trace_and_one_dump(self):
        port_image, original_image = self.images(self.LOGICAL)
        self.assertNotEqual(port_image, original_image)
        mine, mine_dump = self.run_side("port", port_image)
        theirs, theirs_dump = self.run_side("original", original_image)
        self.assertEqual(mine, theirs)
        self.assertEqual(len(mine[0].split()), 1 + 4)  # the poll and all four symbols at the first poll
        self.assertEqual(mine_dump, self.EXPECTED_DUMP)
        self.assertEqual(theirs_dump, self.EXPECTED_DUMP)

    def test_the_next_poll_names_only_the_symbols_that_changed(self):
        changed = [(o, k, 0x99 if (o, k) == (0x12, "b") else v) for o, k, v in self.LOGICAL]
        for side, which in (("port", 0), ("original", 1)):
            lines, _ = self.run_side(side, self.images(self.LOGICAL)[which], self.images(changed)[which])
            first, second = lines[0].split(), lines[1].split()
            self.assertEqual(second[0], "2")
            self.assertEqual([t.split(":")[0] for t in second[1:]], ["0"])  # symbol a1 only
            self.assertNotEqual(second[1], first[1])

    def test_pointers_into_the_heap_compare_by_value_and_others_as_one(self):
        moved = [(o, k, 0x80300000 if o == 0x44 else v) for o, k, v in self.LOGICAL]
        static = [(o, k, 0x80020000 if o == 0x48 else v) for o, k, v in self.LOGICAL]
        base = self.run_side("original", self.images(self.LOGICAL)[1])[0]
        self.assertNotEqual(base, self.run_side("original", self.images(moved)[1])[0])
        self.assertEqual(base, self.run_side("original", self.images(static)[1])[0])

    def test_a_bad_spec_is_refused(self):
        (self.tmp / "bad").write_text("region A\nsymbol s\nrun zz q 1\n")
        (self.tmp / "ram").write_bytes(bytes(0x1000))
        run = subprocess.run(
            [str(self.tmp / "port"), str(self.tmp / "bad"), str(self.tmp / "ram"), str(self.tmp / "d")]
        )
        self.assertEqual(run.returncode, 2)


PRELUDE = """
typedef unsigned char u8; typedef unsigned short u16; typedef unsigned int u32; typedef double f64;
typedef struct Item { u8 kind; u16 value; u32 count; } Item;
struct Wide { u8 tag; f64 weight; };
union Raw { u32 word; u8 bytes[4]; };
extern Item gItems[2];
extern u32 gOpen[];
extern const u32 gFixed;
extern struct Wide gWide;
extern union Raw gRaw;
extern u8 gBytes[8];
extern u32 gUnused;
"""


def types() -> statetypes.TypeDb:
    db = statetypes.TypeDb()
    for statement in statetypes.statements(PRELUDE):
        db.add(statement)
    return db


class SpecTests(unittest.TestCase):
    def setUp(self):
        self.symbols = {
            "gItems": BASE + 0x1000,
            "gOpen": BASE + 0x1100,
            "gFixed": BASE + 0x1200,
            "gWide": BASE + 0x1300,
            "gRaw": BASE + 0x1400,
            "gBytes": BASE + 0x1410,
            "gUnused": BASE + 0x1500,
            "gGone": BASE + 0x1600,
        }
        self.owner = {
            "gItems": "ACTORS",
            "gOpen": "ACTORS",
            "gFixed": "ACTORS",
            "gWide": "CAMERA",
            "gRaw": "CAMERA",
            "gBytes": "CAMERA",
            "gGone": "CAMERA",
        }

    def test_entries_are_laid_out_per_region_and_the_rest_is_explained(self):
        regions, why = sd.build_entries(types(), self.symbols, self.owner, None)
        self.assertEqual(sorted(regions), ["ACTORS", "CAMERA"])
        items = next(e for e in regions["ACTORS"] if e.name == "gItems")
        self.assertEqual(items.elements, 6)
        self.assertEqual(items.port[0], (BASE + 0x1000, "b", 1))
        opened = next(e for e in regions["ACTORS"] if e.name == "gOpen")
        self.assertEqual(opened.elements, 0x100 // 4)  # the gap to the next symbol
        wide = next(e for e in regions["CAMERA"] if e.name == "gWide")
        self.assertEqual(wide.original[1][0] - wide.original[0][0], 8)  # the original aligns a double to 8
        self.assertEqual(wide.port[1][0] - wide.port[0][0], 4)  # the port to 4
        self.assertEqual(wide.elements, 2)
        self.assertEqual(why["const"], 1)
        self.assertEqual(why["no unit uses it"], 1)  # gUnused
        self.assertEqual(why["no variable declaration"], 1)  # gGone
        self.assertEqual(why["no host-independent elements"], 1)  # gRaw is a union

    def test_spec_text_has_the_same_elements_for_both_sides(self):
        regions, _ = sd.build_entries(types(), self.symbols, self.owner, None)
        port = sd.spec_text(regions, "port", (BASE + 0x2000, BASE + 0x800000))
        original = sd.spec_text(regions, "oracle", (BASE + 0x2000, BASE + 0x800000))
        self.assertIn("region ACTORS\nsymbol gItems\nrun 80001000 b 1", port)
        self.assertIn("dynamic 80002000 80800000", port)

        def elements(text):
            out = []
            for line in text.splitlines():
                m = re.match(r"run \w+ (\w) (\d+)", line)
                if m:
                    out += [m[1]] * int(m[2])
                elif line.startswith("symbol"):
                    out.append(line)
            return out

        self.assertEqual(elements(port), elements(original))
        self.assertNotEqual(port, original)  # the double sits at another address

    def test_skip_pattern(self):
        regions, why = sd.build_entries(types(), self.symbols, self.owner, re.compile("^gBytes$"))
        self.assertEqual(why["skipped by name"], 1)
        self.assertFalse(any(e.name == "gBytes" for e in regions["CAMERA"]))

    def test_area_comes_from_the_source_layout(self):
        self.assertEqual(sd.area_of(Path("game/ACTORS/x.CPP.o")), "ACTORS")
        self.assertEqual(sd.area_of(Path("game/DATA/AMMO/x.CPP.o")), "DATA/AMMO")
        self.assertEqual(sd.area_of(Path("sdk/libultra/os/x.o")), "sdk/libultra")
        self.assertIsNone(sd.area_of(Path("x.o")))


TABLE = {
    "regions": {
        "ACTORS": {
            "symbols": [
                {
                    "index": 0,
                    "name": "gItems",
                    "address": BASE + 0x1000,
                    "kinds": [["b", 1], ["h", 1], ["w", 1], ["b", 1], ["h", 1], ["w", 1]],
                    "shape": ["a", 2, ["s", [["kind", ["k", "b"]], ["value", ["k", "h"]], ["count", ["k", "w"]]]]],
                },
                {"index": 1, "name": "gFlag", "address": BASE + 0x1100, "kinds": [["w", 1]], "shape": ["k", "w"]},
            ]
        },
        "CAMERA": {
            "symbols": [
                {"index": 2, "name": "gPos", "address": BASE + 0x1200, "kinds": [["d", 1]], "shape": ["k", "d"]}
            ]
        },
    }
}


class CompareTests(unittest.TestCase):
    def test_every_symbol_keeps_its_own_first_differing_poll(self):
        port = {1: [(0, 1), (1, 2), (2, 3)], 2: [(0, 9)], 3: [(2, 7)]}
        original = {1: [(0, 1), (1, 5), (2, 3)], 2: [], 3: [(2, 3)], 4: [(0, 0)]}
        out = sd.diverged(port, original, [0, 0, 1])
        self.assertEqual((out["compared"], out["first_compared"], out["last_poll"]), (3, 1, 3))
        self.assertEqual(out["first"], [2, 1, 3])
        self.assertEqual(out["polls_differing"], {0: 3, 1: 1})

    def test_a_symbol_that_matches_again_stops_counting(self):
        port = {1: [(0, 1)], 2: [(0, 2)], 3: [(0, 1)]}
        original = {1: [(0, 1)], 2: [], 3: []}
        out = sd.diverged(port, original, [0])
        self.assertEqual(out["first"], [2])
        self.assertEqual(out["polls_differing"], {0: 1})

    def test_traces_without_a_common_poll_compare_nothing(self):
        out = sd.diverged({}, {1: [(0, 1)]}, [0])
        self.assertEqual((out["compared"], out["first_compared"]), (0, None))

    def test_trace_reader_drops_a_half_written_line_and_bad_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.txt"
            path.write_text("1 0:aa 1:bb\n2\n3 5:cc\n4 x\n5 0:dd\n6 0:ee 1")
            self.assertEqual(sd.read_trace(path, 2), {1: [(0, 0xAA), (1, 0xBB)], 2: [], 5: [(0, 0xDD)]})
            self.assertEqual(sd.read_trace(Path(tmp) / "missing", 2), {})

    def test_symbol_compare_names_the_field(self):
        syms = sd.symbol_table(TABLE)
        items = syms[0]
        self.assertEqual((items.region, items.offset, items.size), ("ACTORS", 0, 14))
        self.assertEqual((syms[1].offset, syms[2].offset), (14, 18))
        mine, theirs = bytearray(14), bytearray(14)
        theirs[8:10] = (9).to_bytes(2, "big")  # gItems[1].value
        theirs[10:14] = (3).to_bytes(4, "big")  # gItems[1].count
        diff = sd.compare_symbol(items, bytes(mine), bytes(theirs))
        self.assertEqual(diff["differing"], 2)
        first = diff["fields"][0]
        self.assertEqual((first["symbol"], first["field"], first["kind"]), ("gItems", "[1].value", "h"))
        self.assertEqual((first["port"], first["oracle"]), (0, 9))
        self.assertEqual(diff["fields"][1]["field"], "[1].count")
        self.assertEqual(sd.compare_symbol(items, bytes(mine), bytes(mine)), {"differing": 0, "fields": []})


class FinishTests(unittest.TestCase):
    """finish(): two traces and the dumps in, state-diff.json and findings.jsonl out."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.run = self.tmp / "runs" / "plays" / "r1"
        self.run.mkdir(parents=True)
        self.cache = self.tmp / "runs" / "oracle-state" / "k"
        self.cache.mkdir(parents=True)
        self.folder = self.tmp / "spec"
        self.folder.mkdir()
        (self.run / sd.PORT_TRACE).write_text("1 1\n2 3\n3 5\n")
        (self.cache / "oracle-trace.txt").write_text("1 1\n2 3\n3 7\n")
        self.session = sd.Session(self.folder, self.run, {"runs": self.tmp / "runs"}, "k", TABLE, self.cache, True)
        self.dump_args = []

    def traces(self, port: str, original: str) -> None:
        (self.run / sd.PORT_HASHES).write_text(port)
        (self.cache / sd.ORACLE_HASHES).write_text(original)

    def dumps(self, theirs_at: dict[int, dict[int, bytes]]):
        def fake(session, polls, profile):
            self.dump_args.append(list(polls))
            out = {}
            for p in polls:
                theirs = bytearray(26)
                for at, data in theirs_at.get(p, {}).items():
                    theirs[at : at + len(data)] = data
                out[p] = (bytes(26), bytes(theirs))
            return out

        return fake

    def findings(self) -> list[dict]:
        path = self.tmp / "runs" / sd.FINDINGS
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        if rows:  # each run first clears the movie's earlier state findings
            self.assertEqual((rows[0]["kind"], rows[0]["clears"]), ("clear", ["state-diverge"]))
        return [r for r in rows if r["kind"] != "clear"]

    def test_one_run_reports_every_divergent_region_and_keeps_going(self):
        self.traces("1 0:1 1:2 2:3\n2 2:9\n3 0:5\n", "1 0:1 1:2 2:3\n2\n3\n")
        fake = self.dumps({2: {18: (1).to_bytes(8, "big")}, 3: {8: (9).to_bytes(2, "big")}})
        with mock.patch.object(sd, "_dump_pass", side_effect=fake):
            summary = sd.finish(self.session, None, "movie-a", {})
        self.assertEqual(self.dump_args, [[2, 3]])
        self.assertEqual((summary["divergent"], summary["findings"]), (2, 2))
        report = json.loads((self.run / sd.DIFF).read_text())
        self.assertEqual((report["polls_compared"], report["first_compared"]), (3, 1))
        actors, camera = report["regions"]["ACTORS"], report["regions"]["CAMERA"]
        self.assertEqual(
            (actors["status"], actors["first"]["poll"], actors["first"]["symbol"], actors["first"]["field"]),
            ("diverges", 3, "gItems", "[1].value"),
        )
        self.assertFalse(actors["first"]["static"])
        self.assertIsNone(actors["runtime"])
        self.assertEqual((camera["first"]["poll"], camera["first"]["symbol"]), (2, "gPos"))
        self.assertEqual((camera["first"]["port"], camera["first"]["oracle"]), (0, 1))
        rows = self.findings()
        self.assertEqual(sorted(r["region"] for r in rows), ["ACTORS", "CAMERA"])
        row = next(r for r in rows if r["region"] == "ACTORS")
        self.assertEqual(
            (row["kind"], row["movie"], row["poll"], row["symbol"], row["field"]),
            ("state-diverge", "movie-a", 3, "gItems", "gItems[1].value"),
        )
        self.assertEqual(row["evidence"]["vi"], {"port": 5, "oracle": 7})
        self.assertTrue(row["evidence"]["timing"])
        self.assertEqual(row["evidence"]["phase"], "first")
        self.assertFalse(next(r for r in rows if r["region"] == "CAMERA")["evidence"]["timing"])

    def test_a_static_double_in_the_other_word_order_is_no_divergence(self):
        # gPos (a double) differs from the first poll only by its two words' order: data, not a divergence
        self.traces("1 2:1\n", "1 2:2\n")

        def swapped(session, polls, profile):
            mine, theirs = bytearray(26), bytearray(26)
            mine[18:26] = bytes.fromhex("0000000041f00000")
            theirs[18:26] = bytes.fromhex("41f0000000000000")
            return {p: (bytes(mine), bytes(theirs)) for p in polls}

        with mock.patch.object(sd, "_dump_pass", side_effect=swapped):
            summary = sd.finish(self.session, None, "movie-a", {})
        self.assertEqual((summary["divergent"], summary["findings"]), (0, 0))
        report = json.loads((self.run / sd.DIFF).read_text())
        camera = report["regions"]["CAMERA"]
        self.assertEqual((camera["status"], camera["byte_order_symbols"], camera["polls_differing"]), ("same", 1, 0))
        self.assertEqual(report["byte_order_symbols"], {"CAMERA": ["gPos"]})
        self.assertEqual(self.findings(), [])

    def test_a_byte_order_symbol_does_not_hide_a_real_static_difference(self):
        # gFlag (a word) really differs and gPos only in word order: the region's first difference is gFlag
        self.traces("1 1:1 2:1\n", "1 1:2 2:2\n")

        def both(session, polls, profile):
            mine, theirs = bytearray(26), bytearray(26)
            theirs[14:18] = (4).to_bytes(4, "big")
            mine[18:26] = bytes.fromhex("0000000041f00000")
            theirs[18:26] = bytes.fromhex("41f0000000000000")
            return {p: (bytes(mine), bytes(theirs)) for p in polls}

        with mock.patch.object(sd, "_dump_pass", side_effect=both):
            sd.finish(self.session, None, "movie-a", {})
        report = json.loads((self.run / sd.DIFF).read_text())
        self.assertEqual(report["byte_order_symbols"], {"CAMERA": ["gPos"]})
        self.assertEqual(report["regions"]["CAMERA"]["status"], "same")
        actors = report["regions"]["ACTORS"]
        self.assertEqual((actors["status"], actors["first"]["symbol"]), ("diverges", "gFlag"))

    def test_byte_order_only_names_the_units_it_swaps(self):
        syms = sd.symbol_table(TABLE)
        gpos = syms[2]
        word_swapped = bytes.fromhex("0000000041f00000")
        self.assertTrue(sd.byte_order_only(gpos, word_swapped, bytes.fromhex("41f0000000000000")))
        self.assertFalse(sd.byte_order_only(gpos, word_swapped, word_swapped))
        self.assertFalse(sd.byte_order_only(gpos, word_swapped, bytes.fromhex("41f0000000000001")))
        four = sd.Sym(0, "gBytes", "R", BASE + 0x2000, 0, 4, [["b", 4]], ["a", 4, ["k", "b"]])
        self.assertTrue(sd.byte_order_only(four, bytes([104, 86, 13, 128]), bytes([128, 13, 86, 104])))
        self.assertFalse(sd.byte_order_only(four, bytes([104, 86, 13, 128]), bytes([128, 13, 86, 105])))
        halves = sd.Sym(0, "gHalves", "R", BASE + 0x2000, 0, 4, [["h", 2]], ["a", 2, ["k", "h"]])
        self.assertTrue(sd.byte_order_only(halves, bytes([0, 1, 0, 2]), bytes([0, 2, 0, 1])))
        straddle = sd.Sym(0, "gOne", "R", BASE + 0x2001, 0, 2, [["b", 2]], ["a", 2, ["k", "b"]])
        self.assertFalse(sd.byte_order_only(straddle, bytes([1, 2]), bytes([2, 1])))  # the word is not held whole
        word = sd.Sym(0, "gW", "R", BASE + 0x2000, 0, 4, [["w", 1]], ["k", "w"])
        self.assertFalse(sd.byte_order_only(word, bytes([1, 2, 3, 4]), bytes([4, 3, 2, 1])))

    def test_a_static_difference_and_a_later_one_are_two_findings(self):
        # gFlag differs from the first poll; gItems only from poll 3
        self.traces("1 0:1 1:2 2:3\n3 0:5\n", "1 0:1 1:8 2:3\n3\n")
        fake = self.dumps({1: {14: (4).to_bytes(4, "big")}, 3: {8: (9).to_bytes(2, "big")}})
        with mock.patch.object(sd, "_dump_pass", side_effect=fake):
            summary = sd.finish(self.session, None, "movie-a", {})
        self.assertEqual(self.dump_args, [[1, 3]])
        self.assertEqual((summary["divergent"], summary["findings"]), (1, 2))
        actors = json.loads((self.run / sd.DIFF).read_text())["regions"]["ACTORS"]
        self.assertEqual((actors["static_symbols"], actors["runtime_symbols"]), (1, 1))
        self.assertEqual(
            (actors["first"]["symbol"], actors["first"]["static"], actors["first"]["poll"]), ("gFlag", True, 1)
        )
        self.assertEqual(
            (actors["runtime"]["symbol"], actors["runtime"]["field"], actors["runtime"]["poll"]),
            ("gItems", "[1].value", 3),
        )
        phases = sorted((r["evidence"]["phase"], r["symbol"]) for r in self.findings())
        self.assertEqual(phases, [("first", "gFlag"), ("runtime", "gItems")])

    def test_identical_traces_leave_no_finding(self):
        self.traces("1 0:1 1:2 2:3\n2 0:4\n", "1 0:1 1:2 2:3\n2 0:4\n")
        with mock.patch.object(sd, "_dump_pass") as dump:
            summary = sd.finish(self.session, None, "movie-a", {})
        dump.assert_not_called()
        self.assertEqual(summary["divergent"], 0)
        self.assertEqual(self.findings(), [])  # only the clear: an earlier divergence stops standing

    def test_a_failed_dump_pass_still_reports_the_regions(self):
        self.traces("1 0:1 1:2 2:3\n2 2:9\n", "1 0:1 1:2 2:3\n2\n")
        with mock.patch.object(sd, "_dump_pass", side_effect=OSError("no port")):
            summary = sd.finish(self.session, None, "movie-a", {})
        self.assertEqual(summary["divergent"], 1)
        report = json.loads((self.run / sd.DIFF).read_text())
        self.assertIn("dump pass failed", report["notes"][0])
        self.assertIsNone(report["regions"]["CAMERA"]["first"].get("field"))
        self.assertEqual(report["regions"]["CAMERA"]["first"]["symbol"], "gPos")

    def test_a_port_without_the_feature_says_so(self):
        self.traces("", "1 0:1 1:2 2:3\n")
        with mock.patch.object(sd, "_dump_pass") as dump:
            summary = sd.finish(self.session, None, "movie-a", {})
        dump.assert_not_called()
        self.assertEqual((summary["divergent"], summary["polls_compared"]), (0, 0))
        report = json.loads((self.run / sd.DIFF).read_text())
        self.assertEqual(report["regions"]["ACTORS"]["status"], "unreached")
        self.assertIn("wrote no state trace", report["notes"][0])


if __name__ == "__main__":
    unittest.main()


class DumpPassPadsTests(unittest.TestCase):
    """_dump_pass(): a movie of more than one pad replays with that many controllers on both sides."""

    def test_both_sides_see_the_movie_pad_count(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        run, cache = tmp / "run", tmp / "oracle-state" / "k"
        run.mkdir()
        cache.mkdir(parents=True)
        (run / "movie.raw").write_text("0000 0 0 0000 0 0\n" * 5)
        cfg = {"env": {}, "timeout_seconds": 1}
        session = sd.Session(tmp, run, cfg, "k", TABLE, cache, True, oracle_cfg={"timeout_seconds": 1}, pads=2)
        from abu_decomp import tas

        with (
            mock.patch.object(tas, "_argv", return_value=["port"]),
            mock.patch.object(tas, "_wait", return_value=(0, 0.0)),
            mock.patch.object(sd.tas_oracle, "start") as oracle,
            mock.patch.object(sd.subprocess, "Popen") as port,
        ):
            sd._dump_pass(session, [3], None)
        self.assertEqual(port.call_args.kwargs["env"]["N64C_PADS"], "2")
        self.assertEqual(oracle.call_args.kwargs["pads"], 2)
        self.assertEqual(len((run / sd.DUMPS / "movie.raw").read_text().splitlines()), 3)


class HostTraceTests(unittest.TestCase):
    """TAS-COVER-3: an original host that never reads N64R_TAS_STATE replayed every movie for nothing."""

    def test_a_host_without_the_state_trace_turns_the_state_diff_off_with_its_reason(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        bare, traced = tmp / "bare", tmp / "traced"
        bare.write_bytes(b"\x7fELF host without it")
        traced.write_bytes(b"\x7fELF ... N64R_TAS_STATE ...")
        self.assertFalse(sd.host_traces(bare))
        self.assertTrue(sd.host_traces(traced))
        with mock.patch.object(sd.tas_oracle, "settings", return_value={"host": bare}):
            self.assertIn("no state trace", sd.available(object()))
        with (
            mock.patch.object(sd.tas_oracle, "settings", return_value={"host": traced}),
            mock.patch.object(sd, "_units", return_value=tmp),
        ):
            self.assertIsNone(sd.available(object()))


class StartupFaultTests(unittest.TestCase):
    def test_a_signal_before_poll_1_is_a_named_host_fault(self):
        from abu_decomp import tas_oracle

        self.assertIn("SIGSEGV before poll 1", tas_oracle.startup_note(-11, 0))
        self.assertEqual(tas_oracle.startup_note(-11, 5), "")
        self.assertEqual(tas_oracle.startup_note(0, 0), "")
        self.assertEqual(tas_oracle.startup_note(None, 0), "")


class OracleEventTests(unittest.TestCase):
    def test_the_original_reads_guest_arguments_from_the_recomp_context(self):
        from abu_decomp import tas_oracle

        self.assertEqual(
            tas_oracle.event_commands("Gfx_DrawRect/2"),
            ['dprintf *Gfx_DrawRect,"EV|Gfx_DrawRect|%d|%d\\n", *(int*)($rsi+32), *(int*)($rsi+40)'],
        )

    def test_split_events_moves_the_event_lines_out_of_the_output(self):
        from abu_decomp import tas_oracle

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / tas_oracle.LOG).write_text("host up\nEV|F|1\nnoise\nEV|F|2\n")
        self.assertEqual(tas_oracle.split_events(tmp), 2)
        self.assertEqual((tmp / tas_oracle.ORACLE_EVENTS).read_text(), "EV|F|1\nEV|F|2\n")
