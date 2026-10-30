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

    def test_area_follows_the_pinned_sources_layout(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        self.assertEqual(sd.areas(None), sd.DEFAULT_AREAS)
        self.assertEqual(sd.areas(root), sd.DEFAULT_AREAS)  # no config.toml: the packaged layout
        (root / "config.toml").write_text(
            '[layout]\nsource_roots = ["src", "sdk"]\nsubsystem_source = "src/{subsystem}"\n'
            'library_source = "sdk/{library}"\n'
        )
        moved = sd.areas(root)
        self.assertEqual(moved, ((), ("sdk",)))
        self.assertEqual(sd.area_of(Path("ACTORS/x.CPP.o"), moved), "ACTORS")
        self.assertEqual(sd.area_of(Path("DATA/AMMO/x.CPP.o"), moved), "DATA/AMMO")
        self.assertEqual(sd.area_of(Path("sdk/libultra/os/x.o"), moved), "sdk/libultra")
        self.assertIsNone(sd.area_of(Path("x.o"), moved))


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

    def test_coverage_names_prefix_interior_and_tail_gaps(self):
        # A slice of the pp-1p-match shape: the original traced polls 1-23368, the port 1-59874 (the tail), and here
        # the port also lacks a prefix and an interior stretch while the original lacks polls 40-41 mid-run.
        port = {n: [] for n in [*range(4, 20), *range(25, 60)]}
        original = {n: [] for n in [*range(1, 40), *range(42, 51)]}
        out = sd.coverage(port, original)
        self.assertFalse(out["complete"])
        self.assertEqual((out["port_first"], out["port_last"], out["oracle_first"], out["oracle_last"]), (4, 59, 1, 50))
        self.assertEqual(out["oracle_only_ranges"], [[1, 3], [20, 24]])
        self.assertEqual(out["port_only_ranges"], [[40, 41], [51, 59]])
        self.assertEqual((out["only_port"], out["only_oracle"]), (11, 8))
        self.assertEqual(out["first_missing"], "polls 1-3 are only in the original trace, missing from the port's")

    def test_coverage_is_complete_only_for_equal_nonempty_traces(self):
        same = {1: [], 2: [(0, 1)]}
        self.assertTrue(sd.coverage(same, dict(same))["complete"])
        self.assertIsNone(sd.coverage(same, dict(same))["first_missing"])
        self.assertFalse(sd.coverage({}, {})["complete"])
        self.assertEqual(sd.coverage({}, {})["first_missing"], "the port's trace is empty")
        self.assertEqual(
            sd.coverage({3: []}, {})["first_missing"], "poll 3 is only in the port trace, missing from the original's"
        )

    def test_coverage_lists_a_capped_number_of_ranges(self):
        out = sd.coverage({n: [] for n in range(0, 100, 2)}, {})
        self.assertEqual(len(out["port_only_ranges"]), sd.RANGES_SHOWN)
        self.assertEqual(out["only_port"], 50)

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

    def ledger(self, text):
        (self.tmp / "lockstep-ignore.tsv").write_text(text)
        self.session.cfg["env"] = {"N64C_PORT_DIR": str(self.tmp)}

    def test_reviewed_symbol_is_excluded_with_its_cause(self):
        self.ledger("gPos\tNative render phase; measured first poll 1.\n")
        self.traces("1 0:1 1:2 2:3\n", "1 0:1 1:2 2:4\n")
        with mock.patch.object(sd, "_dump_pass") as dump:
            summary = sd.finish(self.session, None, "movie-a", {})
        dump.assert_not_called()
        self.assertEqual(
            (summary["raw_symbols"], summary["reviewed_symbols"], summary["unreviewed_symbols"]), (1, 1, 0)
        )
        self.assertEqual((summary["divergent"], summary["reviewed_divergent"]), (0, 1))
        report = json.loads((self.run / sd.DIFF).read_text())
        self.assertEqual(report["reviewed"][0]["cause"], "Native render phase; measured first poll 1.")
        self.assertEqual(self.findings(), [])

    def test_unknown_symbol_still_diverges_in_a_reviewed_region(self):
        self.ledger("gFlag\tReviewed neighboring object.\n")
        self.traces("1 0:1 1:2 2:3\n", "1 0:4 1:5 2:3\n")
        with mock.patch.object(sd, "_dump_pass", return_value={}):
            summary = sd.finish(self.session, None, "movie-a", {})
        self.assertEqual(
            (summary["raw_symbols"], summary["reviewed_symbols"], summary["unreviewed_symbols"]), (2, 1, 1)
        )
        self.assertEqual(summary["divergent"], 1)
        self.assertEqual(self.findings()[0]["symbol"], "gItems")

    def test_malformed_ledger_cannot_accept_a_difference(self):
        self.ledger("gPos\t\n")
        self.traces("1 0:1 1:2 2:3\n", "1 0:1 1:2 2:4\n")
        with self.assertRaisesRegex(ValueError, "lockstep-ignore.tsv:1"):
            sd.finish(self.session, None, "movie-a", {})

    def test_the_record_names_the_checkpoint_events_and_both_coverages(self):
        self.traces("1 0:1 1:2 2:3\n2 2:9\n", "1 0:1 1:2 2:3\n2\n")
        (self.run / sd.PORT_EVENTS).write_text("EV|textBegin@1|1\nEV|textReset@1\n")
        (self.run / "oracle.txt").write_text("host up\nEV|textBegin@1|1\n")
        with mock.patch.object(sd, "_dump_pass", return_value={}):
            summary = sd.finish(self.session, None, "movie-a", {})
        report = json.loads((self.run / sd.DIFF).read_text())
        for out in (summary, report):
            self.assertEqual(out["checkpoint"], "poll-entry")
            self.assertEqual(out["events"], {"port": 2, "oracle": 1})
            self.assertTrue(out["state_polls"]["complete"] and out["trace_polls"]["complete"])
            self.assertEqual((out["state_polls"]["port_sampled"], out["trace_polls"]["compared"]), (2, 3))
        row = report["regions"]["CAMERA"]["first"]
        self.assertEqual(row["poll"], 2)
        self.assertEqual(
            row["events"],
            {
                "checkpoint": "poll-entry",
                "tagged_poll": 1,
                "port": ["textBegin(1)", "textReset()"],
                "oracle": ["textBegin(1)"],
            },
        )

    def test_a_poll_trace_one_side_cut_short_is_named_apart_from_the_state_samples(self):
        self.traces("1 0:1 1:2 2:3\n2\n3\n", "1 0:1 1:2 2:3\n2\n3\n")
        (self.cache / "oracle-trace.txt").write_text("1 1\n2 3\n")
        with mock.patch.object(sd, "_dump_pass", return_value={}):
            sd.finish(self.session, None, "movie-a", {})
        report = json.loads((self.run / sd.DIFF).read_text())
        self.assertTrue(report["state_polls"]["complete"])
        self.assertFalse(report["trace_polls"]["complete"])
        self.assertEqual(report["trace_polls"]["port_only_ranges"], [[3, 3]])
        self.assertNotIn("events", report["regions"]["ACTORS"])

    def test_one_run_reports_every_divergent_region_and_keeps_going(self):
        # both regions differ only after the first compared poll: the oracle run's own trace is the whole detail
        self.traces("1 0:1 1:2 2:3\n2 2:9\n3 0:5\n", "1 0:1 1:2 2:3\n2\n3\n")
        with mock.patch.object(sd, "_dump_pass") as dump:
            summary = sd.finish(self.session, None, "movie-a", {})
        dump.assert_not_called()
        self.assertEqual((summary["detail"]["source"], summary["detail"]["polls"]), ("trace", []))
        self.assertEqual((summary["divergent"], summary["findings"]), (2, 2))
        report = json.loads((self.run / sd.DIFF).read_text())
        self.assertEqual((report["polls_compared"], report["first_compared"]), (3, 1))
        actors, camera = report["regions"]["ACTORS"], report["regions"]["CAMERA"]
        self.assertEqual(
            (actors["status"], actors["first"]["poll"], actors["first"]["symbol"], actors["first"].get("field")),
            ("diverges", 3, "gItems", None),
        )
        self.assertFalse(actors["first"]["static"])
        self.assertIsNone(actors["runtime"])
        self.assertEqual((camera["first"]["poll"], camera["first"]["symbol"]), (2, "gPos"))
        rows = self.findings()
        self.assertEqual(sorted(r["region"] for r in rows), ["ACTORS", "CAMERA"])
        row = next(r for r in rows if r["region"] == "ACTORS")
        self.assertEqual(
            (row["kind"], row["movie"], row["poll"], row["symbol"]), ("state-diverge", "movie-a", 3, "gItems")
        )
        self.assertEqual(row["evidence"]["vi"], {"port": 5, "oracle": 7})
        self.assertTrue(row["evidence"]["timing"])
        self.assertEqual(row["evidence"]["phase"], "first")
        self.assertFalse(next(r for r in rows if r["region"] == "CAMERA")["evidence"]["timing"])

    def test_unreviewed_byte_order_difference_still_diverges(self):
        # Word-order diagnostics do not authorize unreviewed exclusions.
        self.traces("1 2:1\n", "1 2:2\n")

        def swapped(session, polls, profile):
            mine, theirs = bytearray(26), bytearray(26)
            mine[18:26] = bytes.fromhex("0000000041f00000")
            theirs[18:26] = bytes.fromhex("41f0000000000000")
            return {p: (bytes(mine), bytes(theirs)) for p in polls}

        with mock.patch.object(sd, "_dump_pass", side_effect=swapped):
            summary = sd.finish(self.session, None, "movie-a", {})
        self.assertEqual((summary["divergent"], summary["findings"]), (1, 1))
        report = json.loads((self.run / sd.DIFF).read_text())
        camera = report["regions"]["CAMERA"]
        self.assertEqual(
            (camera["status"], camera["byte_order_symbols"], camera["polls_differing"]), ("diverges", 1, 1)
        )
        self.assertEqual(report["byte_order_symbols"], {"CAMERA": ["gPos"]})
        self.assertEqual(len(self.findings()), 1)

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
        self.assertEqual(report["regions"]["CAMERA"]["status"], "diverges")
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
        self.assertTrue(sd.byte_order_only(word, b"triV", b"Virt"))  # a string chunk typed as a word
        self.assertFalse(sd.byte_order_only(word, b"triV", b"Virs"))

    def test_a_static_difference_and_a_later_one_are_two_findings(self):
        # gFlag differs from the first poll; gItems only from poll 3
        self.traces("1 0:1 1:2 2:3\n3 0:5\n", "1 0:1 1:8 2:3\n3\n")
        fake = self.dumps({1: {14: (4).to_bytes(4, "big")}, 3: {8: (9).to_bytes(2, "big")}})
        with mock.patch.object(sd, "_dump_pass", side_effect=fake):
            summary = sd.finish(self.session, None, "movie-a", {})
        self.assertEqual(self.dump_args, [[1, 3]])
        self.assertEqual(summary["detail"]["source"], "replay")
        self.assertIn("static first divergence at polls [1]", summary["detail"]["reason"])  # the run record's reason
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
        self.traces("1 0:1 1:2 2:9\n", "1 0:1 1:2 2:3\n")
        with mock.patch.object(sd, "_dump_pass", side_effect=OSError("no port")):
            summary = sd.finish(self.session, None, "movie-a", {})
        self.assertEqual(summary["divergent"], 1)
        report = json.loads((self.run / sd.DIFF).read_text())
        self.assertIn("dump pass failed", report["notes"][0])
        self.assertIsNone(report["regions"]["CAMERA"]["first"].get("field"))
        self.assertEqual(report["regions"]["CAMERA"]["first"]["symbol"], "gPos")

    def test_a_failed_detail_replay_is_named_in_the_result(self):
        self.traces("1 0:1 1:2 2:9\n", "1 0:1 1:2 2:3\n")

        def killed(session, polls, profile):
            session.detail_runs.append({"side": "original", "exit": None, "outcome": "hang", "cpu_seconds": 300.0})
            return {}

        with mock.patch.object(sd, "_dump_pass", side_effect=killed):
            summary = sd.finish(self.session, None, "movie-a", {})
        self.assertEqual(summary["detail"]["failed"], ["original"])
        self.assertEqual(summary["detail"]["runs"][0]["cpu_seconds"], 300.0)

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


class DumpPassFailureTests(unittest.TestCase):
    """_dump_pass(): a replay killed at its bound or exiting non-zero is reported with exit, CPU and log, never read."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.run, self.cache = self.tmp / "run", self.tmp / "oracle-state" / "k"
        self.run.mkdir()
        self.cache.mkdir(parents=True)
        (self.run / "movie.raw").write_text("0000\n" * 5)
        cfg = {"env": {}, "timeout_seconds": 1}
        self.session = sd.Session(
            self.tmp, self.run, cfg, "k", TABLE, self.cache, True, oracle_cfg={"timeout_seconds": 1}
        )

    def replay(self, port_end, original_end):
        from abu_decomp import tas

        def original(oracle, work, *args, **kwargs):  # the original leaves its output and one dump
            (work / "oracle.txt").write_text("original output")
            (work / "state-3.bin").write_bytes(b"theirs")
            return mock.Mock()

        def port(argv, env, stdout, **kwargs):
            stdout.write("port output")
            (self.run / sd.DUMPS / "state-3.bin").write_bytes(b"mine")

        with (
            mock.patch.object(tas, "_argv", return_value=["port"]),
            mock.patch.object(tas, "_wait", side_effect=[port_end, original_end]),
            mock.patch.object(sd.tas_oracle, "start", side_effect=original),
            mock.patch.object(sd.subprocess, "Popen", side_effect=port),
        ):
            return sd._dump_pass(self.session, [3], None)

    def test_the_original_killed_at_its_bound_is_reported_and_adds_nothing(self):
        dumps = self.replay((0, 40.0), (None, 300.0))
        self.assertEqual(dumps, {})
        self.assertFalse((self.cache / "dump-3.bin").exists())  # a killed replay is not cached
        ran = {r["side"]: r for r in self.session.detail_runs}
        self.assertEqual((ran["port"]["exit"], ran["port"]["cpu_seconds"]), (0, 40.0))
        self.assertEqual((ran["original"]["outcome"], ran["original"]["exit"]), ("hang", None))
        self.assertEqual(ran["original"]["cpu_seconds"], 300.0)
        self.assertEqual(Path(ran["original"]["log"]).read_text(), "original output")
        self.assertIn("detail replay of the original hang, 300.0 CPU s", self.session.notes[0])

    def test_the_port_exiting_non_zero_is_reported_and_its_dumps_are_not_read(self):
        dumps = self.replay((3, 12.5), (0, 30.0))
        self.assertEqual(dumps, {})
        ran = {r["side"]: r for r in self.session.detail_runs}
        self.assertEqual((ran["port"]["outcome"], ran["port"]["subject"]), ("exit", "exit 3"))
        self.assertEqual(Path(ran["port"]["log"]).read_text(), "port output")
        self.assertIn("detail replay of the port exit (exit 3), 12.5 CPU s", self.session.notes[0])

    def test_two_clean_replays_pair_the_dumps(self):
        dumps = self.replay((0, 40.0), (0, 30.0))
        self.assertEqual(dumps, {3: (b"mine", b"theirs")})
        self.assertEqual(self.session.notes, [])
        self.assertEqual([r["exit"] for r in self.session.detail_runs], [0, 0])


class OracleRunTests(unittest.TestCase):
    """_oracle_done(): only a run that exited 0 and reached the movie's end is cached as complete."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.runs = self.tmp / "runs"
        self.run = self.runs / "plays" / "r1"
        self.run.mkdir(parents=True)
        self.final = self.runs / sd.ORACLES / "k"
        self.work = self.runs / sd.ORACLES / "k.tmp1-r1"
        self.work.mkdir(parents=True)

    def session(self, hashes, code, cpu=12.5, attached=False):
        folder = self.run if attached else self.work
        (folder / sd.ORACLE_HASHES).write_text(hashes)
        (folder / "oracle-trace.txt").write_text("1 5\n")
        (folder / "oracle.txt").write_text("host output")
        (folder / "oracle.bmp").write_bytes(b"BM")
        session = sd.Session(
            self.tmp, self.run, {"runs": self.runs}, "k", TABLE, folder, False, attached=attached, movie_polls=3
        )
        session._end.update(code=code, cpu=cpu, wall=9.0)
        return session

    def refused(self, session, outcome, cpu):
        self.assertFalse(sd._oracle_done(session))
        self.assertFalse(self.final.exists())  # never cached
        facts = session.oracle_run
        self.assertEqual((facts["outcome"], facts["cpu_seconds"]), (outcome, cpu))
        self.assertIn("not cached", facts["refused"])
        self.assertIn(facts["refused"], session.notes)
        return facts

    def test_a_run_killed_at_the_bound_is_not_cached(self):
        facts = self.refused(self.session("1\n2\n", None), "hang", 12.5)
        self.assertEqual(facts["exit"], None)
        self.assertEqual((self.run / sd.STATE_LOG).read_text(), "host output")  # the log stays in the run
        self.assertFalse(self.work.exists())

    def test_a_non_zero_exit_is_not_cached_even_with_a_whole_trace(self):
        facts = self.refused(self.session("1\n2\n3\n", 3), "exit", 12.5)
        self.assertEqual((facts["exit"], facts["subject"], facts["polls"]), (3, "exit 3", 3))
        self.assertEqual((self.run / sd.STATE_LOG).read_text(), "host output")

    def test_partial_hashes_are_not_cached_after_a_clean_exit(self):
        facts = self.refused(self.session("1\n2\n3", 0), "ok", 12.5)  # the last line is half written
        self.assertEqual((facts["polls"], facts["wanted_polls"]), (2, 3))
        self.assertIn("2 of 3 polls", facts["refused"])

    def test_a_whole_run_is_cached_with_its_marker_and_without_its_pictures(self):
        session = self.session("1\n2\n3\n", 0)
        self.assertTrue(sd._oracle_done(session))
        self.assertTrue(session.cached)
        self.assertEqual(session.oracle_dir, self.final)
        self.assertEqual(
            sorted(p.name for p in self.final.iterdir()), [sd.COMPLETE, "oracle-trace.txt", sd.ORACLE_HASHES]
        )
        self.assertTrue(sd._complete(self.final, 3))

    def test_the_plays_one_oracle_leaves_the_run_its_files_and_the_cache_copies(self):
        session = self.session("1\n2\n3\n", 0, attached=True)
        self.assertTrue(sd._oracle_done(session))
        self.assertEqual(
            sorted(p.name for p in self.final.iterdir()), [sd.COMPLETE, "oracle-trace.txt", sd.ORACLE_HASHES]
        )
        for kept in ("oracle.txt", "oracle.bmp", "oracle-trace.txt", sd.ORACLE_HASHES):
            self.assertTrue((self.run / kept).is_file(), kept)  # the picture verdict still reads these

    def test_the_plays_one_oracle_killed_at_the_bound_keeps_the_run_and_caches_nothing(self):
        session = self.session("1\n2\n", None, cpu=300.0, attached=True)
        facts = self.refused(session, "hang", 300.0)
        self.assertEqual(facts["log"], str(self.run / "oracle.txt"))
        self.assertEqual((self.run / "oracle.txt").read_text(), "host output")
        self.assertTrue((self.run / sd.ORACLE_HASHES).is_file())

    def test_a_refused_run_is_an_error_in_the_diff_with_its_exit_and_cpu(self):
        session = self.session("1\n", None, cpu=300.0)
        summary = sd.finish(session, None, "movie-a", {})
        self.assertEqual(summary["error"], "the original left no state trace")
        report = json.loads((self.run / sd.DIFF).read_text())
        self.assertEqual((report["oracle_run"]["outcome"], report["oracle_run"]["cpu_seconds"]), ("hang", 300.0))
        self.assertEqual(summary["oracle_run"], report["oracle_run"])

    def test_an_entry_is_complete_only_with_a_clean_marker_and_the_movie_s_whole_trace(self):
        entry = self.runs / sd.ORACLES / "e"
        entry.mkdir()
        (entry / sd.ORACLE_HASHES).write_text("1\n2\n3\n")
        self.assertFalse(sd._complete(entry, 3))  # no marker: an entry an older build left, maybe partial
        (entry / sd.COMPLETE).write_text(json.dumps({"exit": 0}))
        self.assertTrue(sd._complete(entry, 3))
        self.assertFalse(sd._complete(entry, 4))  # the trace stops short of the movie
        (entry / sd.COMPLETE).write_text(json.dumps({"exit": 1}))
        self.assertFalse(sd._complete(entry, 3))
        (entry / sd.COMPLETE).write_text("{")
        self.assertFalse(sd._complete(entry, 3))


class BeginTests(unittest.TestCase):
    """begin(): one oracle process per play, and an incomplete cache entry is never reused."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.runs = self.tmp / "runs"
        self.run = self.runs / "plays" / "r1"
        self.run.mkdir(parents=True)
        (self.run / "movie.raw").write_text("0000\n" * 3)
        self.folder = self.tmp / "spec"
        self.folder.mkdir()
        (self.folder / "oracle.spec").write_text("spec")
        host = self.tmp / "n64host"
        host.write_text("")
        self.oracle = {"host": host, "env": {}, "shot_every": 25, "timeout_seconds": 5}
        self.cfg = {"port": str(self.tmp / "port"), "runs": self.runs, "exit_after": 7}

    def begin(self, attach=False):
        from abu_decomp import tas

        with (
            mock.patch.object(sd, "available", return_value=None),
            mock.patch.object(sd.tas_oracle, "required", return_value=self.oracle),
            mock.patch.object(sd, "build_spec", return_value=self.folder),
            mock.patch.object(sd, "load_map", return_value=TABLE),
            mock.patch.object(sd.tas_oracle, "start", return_value=mock.Mock()) as start,
            mock.patch.object(tas, "_wait", return_value=(0, 1.0)),
        ):
            session = sd.begin(None, self.cfg, self.run, "on", None, [0, 0, 0], attach=attach)
            if session and session._waiter:
                session._waiter.join()
        return session, start

    def test_a_play_with_its_own_oracle_starts_none_here_and_asks_it_for_the_trace(self):
        session, start = self.begin(attach=True)
        start.assert_not_called()
        self.assertTrue(session.attached)
        self.assertIsNone(session.process)
        self.assertEqual(session.oracle_dir, self.run)
        env = session.oracle_env()
        self.assertEqual(env["N64R_TAS_STATE_HASH"], str(self.run / sd.ORACLE_HASHES))
        self.assertEqual(env["N64R_TAS_STATE"], str(self.folder / "oracle.spec"))

    def test_a_state_only_play_still_starts_the_one_oracle(self):
        session, start = self.begin()
        start.assert_called_once()
        self.assertFalse(session.attached)

    def test_an_incomplete_cache_entry_is_cleared_and_never_reused(self):
        session, _ = self.begin(attach=True)
        entry = self.runs / sd.ORACLES / session.key
        entry.mkdir(parents=True)
        (entry / sd.ORACLE_HASHES).write_text("1\n2\n")  # what the killed run left: no marker, short trace
        again, start = self.begin()
        self.assertFalse(again.cached)
        start.assert_called_once()
        self.assertFalse(entry.exists())

    def test_a_complete_cache_entry_is_reused(self):
        session, _ = self.begin(attach=True)
        entry = self.runs / sd.ORACLES / session.key
        entry.mkdir(parents=True)
        (entry / sd.ORACLE_HASHES).write_text("1\n2\n3\n")
        (entry / sd.COMPLETE).write_text(json.dumps({"exit": 0}))
        again, start = self.begin(attach=True)
        self.assertTrue(again.cached)
        start.assert_not_called()


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
            [
                'dprintf *Gfx_DrawRect,"EV|Gfx_DrawRect@%ld|%d|%d\\n", (long)n64host_tas_poll, '
                "*(int*)($rsi+32), *(int*)($rsi+40)"
            ],
        )

    def test_split_events_moves_the_event_lines_out_of_the_output(self):
        from abu_decomp import tas_oracle

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / tas_oracle.LOG).write_text("host up\nEV|F@3|1\nnoise\nEV|F@4|2\n")
        self.assertEqual(tas_oracle.split_events(tmp), 2)
        self.assertEqual((tmp / tas_oracle.ORACLE_EVENTS).read_text(), "EV|F@3|1\nEV|F@4|2\n")

    def test_an_event_with_no_poll_is_refused(self):
        from abu_decomp import DecompError, tas_oracle

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / tas_oracle.LOG).write_text("EV|F@3|1\nEV|F|2\n")
        with self.assertRaisesRegex(DecompError, "event 2 carries no poll"):
            tas_oracle.split_events(tmp)
        self.assertFalse((tmp / tas_oracle.ORACLE_EVENTS).exists())

    def test_the_host_must_carry_the_poll_counter(self):
        from abu_decomp import tas_oracle

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / "tagged").write_bytes(b"\0\0" + tas_oracle.POLL_SYMBOL.encode() + b"\0")
        (tmp / "old").write_bytes(b"\0N64R_TAS_STATE\0")
        (tmp / "empty").write_bytes(b"")
        self.assertTrue(tas_oracle.host_tags_polls(tmp / "tagged"))
        self.assertFalse(tas_oracle.host_tags_polls(tmp / "old"))
        self.assertFalse(tas_oracle.host_tags_polls(tmp / "empty"))
        self.assertFalse(tas_oracle.host_tags_polls(tmp / "missing"))


DATA = Path(__file__).parent / "data" / "tas_checkpoint"
CLIP_TOP = 55  # g_textClipTop in the menu movie's 150-symbol spec


class PollTaggedEventTests(unittest.TestCase):
    """The port's and the original's events are one form, `EV|FUNC@POLL|args`, and place a call in a poll."""

    def test_the_ports_real_events_parse_with_their_polls(self):
        from abu_decomp import tas_oracle

        events = tas_oracle.read_events(DATA / "events-port.txt")
        first = tas_oracle.Event("clipRead", 1027, ("target=0x6020b734", "top=0", "cached=0", "caller=0x836a0c4"))
        self.assertEqual(events[0], first)
        self.assertTrue(events and all(e.poll in (1027, 1028, 1029) for e in events))
        self.assertIn("func_802A1870_de(89,60,194,164)", tas_oracle.events_in(events, 1027))

    def test_the_two_sides_events_tag_the_same_poll_in_the_same_form(self):
        from abu_decomp import tas_oracle

        port = tas_oracle.parse_events("EV|textBegin@7|1\nEV|textReset@7\nEV|textBegin@8|2\n")
        oracle = tas_oracle.parse_events("EV|textBegin@7|1\nEV|textBegin@8|2\nEV|textReset@8\n")
        self.assertEqual(tas_oracle.events_in(port, 7), ["textBegin(1)", "textReset()"])
        self.assertEqual(tas_oracle.events_in(oracle, 7), ["textBegin(1)"])  # the poll tag places the difference
        self.assertEqual(tas_oracle.events_in(oracle, 8), ["textBegin(2)", "textReset()"])

    def test_no_file_is_not_an_empty_trace(self):
        from abu_decomp import tas_oracle

        self.assertIsNone(tas_oracle.read_events(DATA / "absent.txt"))


class CheckpointRegressionTests(unittest.TestCase):
    """A slice of the real menu-movie traces (tool-06eb26f, polls 1 and 1026-1032): g_textClipTop reads 0 in the port
    and 60 in the original at poll 1029. Naming one checkpoint must not make a real difference pass."""

    def traces(self):
        return sd.read_trace(DATA / "state-port.txt", 150), sd.read_trace(DATA / "state-oracle.txt", 150)

    def test_the_real_difference_still_fails_at_the_checkpoint(self):
        port, original = self.traces()
        found = sd.diverged(port, original, [0] * 150)
        self.assertEqual(found["first"][CLIP_TOP], 1029)
        self.assertEqual(sd.CHECKPOINT, "poll-entry")
        covered = sd.coverage(port, original)
        self.assertTrue(covered["complete"])  # the slice is complete: the verdict can only fail on the difference
        from abu_decomp import tas_oracle

        record = {
            "outcome": "ok",
            "oracle": {"outcome": "ok"},
            "polls": 1032,
            "state_diff": {
                "polls_compared": found["compared"],
                "divergent": 1,
                "state_polls": covered,
                "trace_polls": covered,
            },
        }
        self.assertEqual(tas_oracle.verdict(record), "fail")

    def test_the_same_slice_against_itself_passes_only_with_both_coverages(self):
        from abu_decomp import tas_oracle

        port, _ = self.traces()
        found = sd.diverged(port, port, [0] * 150)
        self.assertEqual(set(found["first"]), {None})
        covered = sd.coverage(port, port)
        state = {"polls_compared": found["compared"], "divergent": 0, "state_polls": covered, "trace_polls": covered}
        record = {"outcome": "ok", "oracle": {"outcome": "ok"}, "polls": 1032, "state_diff": state}
        self.assertEqual(tas_oracle.verdict(record), "ok")
        state["trace_polls"] = sd.coverage({n: 0 for n in port}, {n: 0 for n in list(port)[:-2]})
        self.assertEqual(tas_oracle.verdict(record), "fail")

    def test_coverage_counts_what_each_side_sampled(self):
        port, original = {n: [] for n in range(1, 11)}, {n: [] for n in range(1, 9)}
        out = sd.coverage(port, original)
        self.assertEqual((out["port_sampled"], out["oracle_sampled"], out["compared"]), (10, 8, 8))
        self.assertEqual((out["complete"], out["port_only_ranges"]), (False, [[9, 10]]))
