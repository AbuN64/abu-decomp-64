"""`decomp tas bake` (module tas_bake): the site files, over goldens cut from the real phase map, coverage table,
gcovr build and relocatable object of one source file (tests/data/tas_bake/)."""

import contextlib
import gzip
import io
import json
import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp import DecompError, tas_bake, tas_phasemap

DATA = Path(__file__).parent / "data" / "tas_bake"
FILE = "src/game/LEVEL/8020402C.CPP"
MAP = json.loads((DATA / "map.json").read_text())
# two functions that the golden object's Level_ShowTriggerMessage calls, defined in files the golden does not hold
MAP["fns"] += [
    ["Camera_PostTargetedScreenMessage", 0x1000, 64, "exact", None, "src/game/CAMERA/x.CPP", False],
    ["func_8025E11C_de", 0x1100, 64, "exact", None, "src/game/CAMERA/x.CPP", False],
]
TABLE = json.loads((DATA / "table.json").read_text())


class StubPhase:
    def __init__(self, *_args):
        pass

    def phase(self):
        return {"stage": "exact", "next_stage": "covered"}, 200


def elf_object(order: str, machine: int, kind: int) -> bytes:
    """A relocatable ELF32 whose per-function section `.text.caller` has one relocation of `kind` against `callee`
    and one against the data symbol `table`; the symbol table is [null, caller, callee, table]."""
    strtab = b"\0caller\0callee\0table\0"
    names = {"caller": 1, "callee": 8, "table": 15}
    shstr = b"\0.text.caller\0.symtab\0.strtab\0.shstrtab\0.rel.text.caller\0"
    shn = {".text.caller": 1, ".symtab": 14, ".strtab": 22, ".shstrtab": 30, ".rel.text.caller": 40}
    sym = b"".join(
        struct.pack(order + "IIIBBH", name, value, size, info, 0, shndx)
        for name, value, size, info, shndx in [
            (0, 0, 0, 0, 0),
            (names["caller"], 0, 16, 0x12, 1),  # GLOBAL FUNC in section 1
            (names["callee"], 0, 0, 0x10, 0),  # undefined
            (names["table"], 0, 0, 0x10, 0),
        ]
    )
    rel = struct.pack(order + "II", 4, (2 << 8) | kind) + struct.pack(order + "II", 8, (3 << 8) | kind)
    text = bytes(16)
    body = bytearray(52)
    offsets = {}
    for label, blob in (("text", text), ("sym", sym), ("str", strtab), ("shstr", shstr), ("rel", rel)):
        offsets[label] = len(body)
        body += blob
    shoff = len(body)

    def header(name, kind_, off, size, link=0, info=0, entsize=0):
        return struct.pack(order + "IIIIIIIIII", name, kind_, 0, 0, off, size, link, info, 0, entsize)

    heads = [
        header(0, 0, 0, 0),
        header(shn[".text.caller"], 1, offsets["text"], len(text)),
        header(shn[".symtab"], 2, offsets["sym"], len(sym), link=3, entsize=16),
        header(shn[".strtab"], 3, offsets["str"], len(strtab)),
        header(shn[".shstrtab"], 3, offsets["shstr"], len(shstr)),
        header(shn[".rel.text.caller"], 9, offsets["rel"], len(rel), link=2, info=1, entsize=8),
    ]
    body += b"".join(heads)
    ident = b"\x7fELF" + bytes([1, 1 if order == "<" else 2, 1]) + bytes(9)
    body[0:52] = ident + struct.pack(
        order + "HHIIIIIHHHHHH", 1, machine, 1, 0, 0, shoff, 0, 52, 0, 0, 40, len(heads), 4
    )
    return bytes(body)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.runs, self.pin, self.units = self.tmp / "runs", self.tmp / "pin", self.tmp / "units"
        (self.pin / FILE).parent.mkdir(parents=True)
        shutil.copy(DATA / "8020402C.CPP", self.pin / FILE)
        self.units.mkdir()
        shutil.copy(DATA / "8020402C.CPP.o", self.units / "8020402C.CPP.o")

    def inputs(self, union: bool = False) -> tas_bake.Inputs:
        got = object.__new__(tas_bake.Inputs)
        got.profile, got.runs, got.site = None, self.runs, self.runs / "site"
        got.cfg = {"runs": self.runs, "movies": self.runs / "movies", "port": self.runs / "port"}
        got.pin, got.claims, got.port_source, got.invocations, got.units = self.pin, None, None, [], self.units
        got.union = DATA / "union.json" if union else self.runs / "coverage" / "union.json"
        got.version, got.table = "us-rev1", TABLE
        got.map_body = lambda: MAP
        return got

    def bake(self, what=None, movie=None, union=False):
        with mock.patch.object(tas_phasemap, "Phase", StubPhase):
            return tas_bake._bake(self.inputs(union), what, movie)

    def manifest(self):
        return json.loads((self.runs / "site" / "manifest.json").read_text())

    def fn(self, name):
        return json.loads((self.runs / "site" / "fn" / f"{name}.json").read_text())


class Files(Fixture):
    def test_put_is_atomic_gzipped_and_idempotent(self):
        path = self.tmp / "a" / "x.json"
        self.assertTrue(tas_bake.put(path, b'{"a":1}'))
        self.assertEqual(gzip.decompress(path.with_name("x.json.gz").read_bytes()), b'{"a":1}')
        self.assertFalse(tas_bake.put(path, b'{"a":1}'))
        self.assertTrue(tas_bake.put(path, b'{"a":2}'))
        self.assertEqual([p.name for p in path.parent.iterdir() if p.name.startswith(".")], [])

    def test_posters_ship_without_gz(self):
        path = self.tmp / "p.jpg"
        tas_bake.put(path, b"jpg", compress=False)
        self.assertFalse(path.with_name("p.jpg.gz").exists())

    def test_hash_names_follow_the_bytes(self):
        self.assertEqual(tas_bake.short(b"a"), tas_bake.short(b"a"))
        self.assertNotEqual(tas_bake.short(b"a"), tas_bake.short(b"b"))
        self.assertEqual(tas_bake.dump({"b": "é", "a": [1]}), '{"b":"é","a":[1]}'.encode())

    def test_parts_round_trip_and_ignore_garbage(self):
        tas_bake.part_write(self.runs, "xref", {"a": 1})
        self.assertEqual(tas_bake.part_read(self.runs, "xref"), {"a": 1})
        (self.runs / tas_bake.PARTS / "bad.json").write_text("[1]")
        self.assertIsNone(tas_bake.part_read(self.runs, "bad"))
        self.assertIsNone(tas_bake.part_read(self.runs, "missing"))


class Rows(Fixture):
    def test_state_and_reach_are_separate_columns(self):
        files, rows = tas_bake.function_rows(MAP, TABLE)
        by = {r[0]: r for r in rows}
        shown = by["Level_ShowTriggerMessage"]  # the map says `covered`: exact C that something reached
        self.assertEqual((shown[3], shown[5]), ("exact", "both"))
        self.assertEqual(files[shown[4]], FILE)
        self.assertEqual(by["func_80206230_de"][5], "oracle")
        self.assertEqual(by["func_8020A7B0_de"][5], "port")
        self.assertEqual(by["Libsn_WritePortWord"][5], "none")
        self.assertEqual(by["Level_CopyTrackBytes"][3:6], ["named", by["Level_CopyTrackBytes"][4], "none"])
        self.assertEqual(by["Sched_BootUsRevisionOneRom"][3], "asm")
        self.assertEqual(len(rows), len(MAP["fns"]))

    def test_unsafe_names_are_dropped(self):
        body = {**MAP, "fns": [["a/b", 0, 4, "exact", None, "x.c", False], ["ok", 4, 4, "fuzzy", None, "x.c", True]]}
        _, rows = tas_bake.function_rows(body, {})
        self.assertEqual([r[0] for r in rows], ["ok"])
        self.assertEqual(rows[0][6], 1)


class Source(Fixture):
    def rows(self):
        return tas_bake.function_rows(MAP, TABLE)

    def test_slice_keeps_the_header_and_highlights(self):
        files, rows = self.rows()
        got = tas_bake.slices(self.inputs(), rows, files, None)
        shown = got["Level_ShowTriggerMessage"]
        self.assertEqual((shown["file"], shown["first"]), (FILE, 164))
        self.assertEqual(len(shown["html"]), shown["last"] - shown["first"] + 1)
        self.assertIn('<span class="nf">Level_ShowTriggerMessage</span>', shown["html"][0])
        self.assertNotIn("Sched_BootUsRevisionOneRom", got)  # an asm file has no C
        self.assertNotIn("Libsn_WritePortWord", got)  # its file is not in the pin

    def test_header_line_walks_up_over_a_wrapped_signature(self):
        lines = ["int x;", "", "static s32 f(s32 a,", "          s32 b)", "{", "}"]
        self.assertEqual(tas_bake.header_line(lines, 4), 2)
        self.assertEqual(tas_bake.header_line(["int x;", "{"], 1), 1)

    def test_gutter_rows_follow_the_slice(self):
        files, rows = self.rows()
        union = tas_bake.read_union(DATA / "union.json")
        got = tas_bake.slices(self.inputs(), rows, files, union)
        lines = tas_bake.gutters(got, union)["Level_ShowTriggerMessage"]
        shown = got["Level_ShowTriggerMessage"]
        self.assertEqual(len(lines), shown["last"] - shown["first"] + 1)
        self.assertEqual(lines[0], [9])  # the header line: a count only
        self.assertIsNone(lines[1])  # a line gcov has no code for
        self.assertIn([0, 0, 2], lines)  # a branch line: count, taken, total
        self.assertIn([9, 1, 2], lines)

    def test_no_union_means_no_gutter(self):
        self.assertEqual(tas_bake.gutters({"a": {"file": "f", "first": 1, "last": 2}}, None), {})
        self.assertIsNone(tas_bake.read_union(self.tmp / "none.json"))

    def test_gcov_positions_fill_in_what_the_parse_misses(self):
        union = {"files": [{"file": f"ragewars-decomp/{FILE}", "functions": [{"name": "Gone", "pos": ["5:1", "9:1"]}]}]}
        self.assertEqual(tas_bake.gcov_positions(union), {(FILE, "Gone"): (5, 9)})
        body = {**MAP, "fns": [["Gone", 0, 4, "exact", None, FILE, False]]}
        files, rows = tas_bake.function_rows(body, {})
        got = tas_bake.slices(self.inputs(), rows, files, union)
        self.assertEqual((got["Gone"]["first"], got["Gone"]["last"]), (5, 9))


class Xref(Fixture):
    def test_real_object_gives_calls_and_data(self):
        names = {r[0] for r in MAP["fns"]}
        calls, _, data = tas_bake.object_refs(self.units / "8020402C.CPP.o", names)
        self.assertEqual(calls["Level_ShowTriggerMessage"], {"Camera_PostTargetedScreenMessage", "func_8025E11C_de"})
        self.assertEqual(data["Level_ShowTriggerMessage"], {"D_80145088"})

    def test_both_byte_orders_and_machines(self):
        for order, machine, call, take in (("<", 3, 4, 1), (">", 8, 4, 2)):
            with self.subTest(machine=machine):
                path = self.tmp / f"m{machine}.o"
                path.write_bytes(elf_object(order, machine, call))
                calls, taken, data = tas_bake.object_refs(path, {"caller", "callee"})
                self.assertEqual(calls, {"caller": {"callee"}})
                self.assertEqual(taken, {})
                self.assertEqual(data, {"caller": {"table"}})
                path.write_bytes(elf_object(order, machine, take))
                calls, taken, _ = tas_bake.object_refs(path, {"caller", "callee"})
                self.assertEqual((calls, taken), ({}, {"caller": {"callee"}}))

    def test_non_elf_is_empty(self):
        (self.units / "x.o").write_bytes(b"nope")
        self.assertEqual(tas_bake.object_refs(self.units / "x.o", set()), ({}, {}, {}))

    def test_callers_are_the_inverse_of_callees(self):
        (self.units / "8020402C.CPP.o").unlink()
        (self.units / "a.o").write_bytes(elf_object("<", 3, 4))
        (self.units / "a.stubs.o").write_bytes(elf_object("<", 3, 4))
        got = tas_bake.xrefs(self.inputs(), {"caller", "callee", "other"})
        self.assertEqual(got["callee"]["callers"], ["caller"])
        self.assertEqual(got["caller"]["callees"], ["callee"])
        self.assertEqual(got["caller"]["refs"], ["table"])
        self.assertEqual(got["other"], {"callers": [], "callees": [], "refs": [], "tables": []})

    def test_missing_units_folder_refuses(self):
        shutil.rmtree(self.units)
        with self.assertRaises(DecompError):
            tas_bake.xrefs(self.inputs(), set())


class Bake(Fixture):
    def test_full_bake_lists_every_function_file(self):
        result = self.bake(union=True)
        manifest = self.manifest()
        site = self.runs / "site"
        names = (site / manifest["fnrows"]).read_text().split()
        self.assertEqual(len(names), len(MAP["fns"]))
        self.assertEqual(result["fns"], len(names))
        for name in names:
            self.assertTrue((site / "fn" / f"{name}.json").is_file(), name)
            self.assertTrue((site / "fn" / f"{name}.json.gz").is_file(), name)
        fns = json.loads((site / manifest["fns"]).read_text())
        self.assertEqual([r[0] for r in fns["rows"]], names)
        self.assertEqual(fns["cols"][3:6], ["state", "file", "reach"])
        self.assertEqual(fns["cols"], ["name", "rom", "size", "state", "file", "reach", "foundation", "behaviour"])
        self.assertTrue(all(len(row) == len(fns["cols"]) for row in fns["rows"]))
        indexed = {row[0]: dict(zip(fns["cols"], row, strict=True)) for row in fns["rows"]}
        for function in TABLE["functions"]:
            if function["name"] in indexed and function.get("in_version"):
                self.assertEqual(indexed[function["name"]]["behaviour"], function.get("behaviour") or "")
        self.assertEqual(manifest["fn"]["count"], len(names))
        self.assertTrue(manifest["gutter"])

    def test_function_file_has_c_gutter_and_callers(self):
        self.bake(union=True)
        shown = self.fn("Level_ShowTriggerMessage")
        self.assertEqual((shown["state"], shown["reach"], shown["first"]), ("exact", "both", 164))
        self.assertTrue(shown["html"])
        self.assertEqual(len(shown["lines"]), len(shown["html"]))
        self.assertEqual(shown["port"]["hit"], True)
        self.assertEqual(shown["movies"], ["a-boxed-set"])
        self.assertEqual(shown["movies_total"], 1)
        self.assertEqual(self.fn("Sched_BootUsRevisionOneRom")["html"], [])

    def test_callers_come_from_the_relocations(self):
        self.bake()
        shown = self.fn("Level_ShowTriggerMessage")
        self.assertEqual(shown["callees"], ["Camera_PostTargetedScreenMessage", "func_8025E11C_de"])
        self.assertEqual(shown["refs"], ["D_80145088"])
        self.assertEqual(self.fn("Camera_PostTargetedScreenMessage")["callers"], ["Level_ShowTriggerMessage"])
        self.assertEqual(self.fn("Camera_PostTargetedScreenMessage")["html"], [])

    def test_without_a_union_the_gutter_is_empty_not_faked(self):
        self.bake()
        self.assertEqual(self.fn("Level_ShowTriggerMessage")["lines"], [])
        self.assertFalse(self.manifest()["gutter"])

    def test_second_bake_rewrites_nothing_and_keeps_the_names(self):
        self.bake(union=True)
        first = self.manifest()
        result = self.bake(union=True)
        self.assertEqual(result["fn_written"], 0)
        self.assertEqual((self.manifest()["fns"], self.manifest()["fnrows"]), (first["fns"], first["fnrows"]))

    def test_partial_bake_reads_the_others_from_parts(self):
        self.bake(union=True)
        shutil.rmtree(self.units)  # xref would refuse if it were read again
        self.bake({"coverage"}, union=True)
        with self.assertRaises(DecompError):
            self.bake({"xref"})
        self.assertTrue(self.fn("Level_ShowTriggerMessage")["callees"])

    def test_old_hashed_names_and_dropped_functions_are_removed(self):
        self.bake()
        site = self.runs / "site"
        (site / "fn" / "Gone.json").write_text("{}")
        (site / "fn" / "Gone.json.gz").write_text("{}")
        (site / "fns.0000000000.json").write_text("{}")
        self.bake()
        self.assertFalse((site / "fn" / "Gone.json").exists())
        self.assertFalse((site / "fn" / "Gone.json.gz").exists())
        self.assertFalse((site / "fns.0000000000.json").exists())

    def test_no_table_refuses(self):
        inputs = self.inputs()
        inputs.table = {}
        with self.assertRaises(DecompError), mock.patch.object(tas_phasemap, "Phase", StubPhase):
            tas_bake._bake(inputs, None, None)

    def test_summary_carries_stage_and_build(self):
        self.bake()
        summary = json.loads((self.runs / "site" / "summary.json").read_text())
        self.assertEqual((summary["version"], summary["stage"], summary["next_stage"]), ("us-rev1", "exact", "covered"))
        self.assertIn("pin", summary["build"])

    def test_arguments_are_checked(self):
        with self.assertRaises(DecompError):
            tas_bake.bake(None, {"nope"})
        with self.assertRaises(DecompError):
            tas_bake.bake(None, {"source"}, "m")


class Movies(Fixture):
    def setUp(self):
        super().setUp()
        (self.runs / "movies").mkdir(parents=True)
        for name in ("m-one", "m-two"):
            (self.runs / "movies" / f"{name}.tas").write_text("# tas 1\n")

    def patched(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(tas_bake.tas, "decode", return_value=({}, [1, 2, 3])))
        stack.enter_context(mock.patch.object(tas_phasemap, "Phase", StubPhase))
        return stack

    def test_reach_bits_prefer_the_live_file_then_the_table(self):
        run = self.tmp / "run"
        run.mkdir()
        index = {r[0]: i for i, r in enumerate(MAP["fns"])}
        size = (len(index) + 7) // 8
        (run / "reach.bin").write_bytes(b"\xff" * size)
        inputs = self.inputs()
        self.assertEqual(tas_bake.reach_bits(inputs, "a-boxed-set", [(run, {})], index), b"\xff" * size)
        (run / "reach.bin").write_bytes(b"\xff")  # wrong size: the table decides
        bits = tas_bake.reach_bits(inputs, "a-boxed-set", [(run, {})], index)
        at = index["Level_ShowTriggerMessage"]
        self.assertTrue(bits[at >> 3] >> (at & 7) & 1)
        self.assertEqual(sum(bin(b).count("1") for b in bits), 2)  # that movie covered two functions
        self.assertIsNone(tas_bake.reach_bits(inputs, "no-such-movie", [], index))

    def test_movie_rows_cover_every_saved_movie_and_one_movie_keeps_the_rest(self):
        index = {r[0]: i for i, r in enumerate(MAP["fns"])}
        with self.patched():
            got = tas_bake.bake_movies(self.inputs(), None, index)
            self.assertEqual(got["movies"], 2)
            rows = json.loads((self.runs / "site" / "movies.json").read_text())["rows"]
            self.assertEqual([r[:2] for r in rows], [["m-one", 3], ["m-two", 3]])
            tas_bake.put(
                self.runs / "site" / "movies.json", tas_bake.dump({"cols": [], "rows": [rows[0], ["m-two", 9]]})
            )
            tas_bake.bake_movies(self.inputs(), "m-two", index)
        rows = json.loads((self.runs / "site" / "movies.json").read_text())["rows"]
        self.assertEqual([r[:2] for r in rows], [["m-one", 3], ["m-two", 3]])

    def test_movie_bake_after_a_full_bake_touches_only_the_movies(self):
        with self.patched():
            self.bake()
            fns = self.manifest()["fns"]
            shutil.rmtree(self.units)
            result = self.bake({"movie"}, "m-one")
        self.assertEqual(result["wrote"], ["movie"])
        self.assertEqual(self.manifest()["fns"], fns)
        self.assertEqual(result["movies"], 2)

    def test_movie_bake_with_no_site_bakes_everything(self):
        with self.patched():
            result = self.bake({"movie"}, "m-one")
        self.assertEqual(result["fns"], len(MAP["fns"]))
        self.assertTrue((self.runs / "site" / "movies.json").is_file())

    def test_unknown_movie_refuses(self):
        index = {"a": 0}
        with self.patched(), self.assertRaises(DecompError):
            tas_bake.bake_movies(self.inputs(), "no-such", index)

    def test_hook_never_fails_a_play(self):
        err = io.StringIO()
        with (
            mock.patch.object(tas_bake, "bake", side_effect=DecompError("tas.bake", "boom")),
            contextlib.redirect_stderr(err),
        ):
            tas_bake.movie_hook(None, "m-one")
            tas_bake.movie_hook(None, None)
        self.assertIn("boom", err.getvalue())


if __name__ == "__main__":
    unittest.main()
