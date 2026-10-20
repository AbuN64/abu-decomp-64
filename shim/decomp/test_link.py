# SPDX-License-Identifier: GPL-3.0-or-later
"""Execute native binding output and reject unqualified project input."""
import argparse
import copy
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

parser = argparse.ArgumentParser()
parser.add_argument("--compiler", required=True)
parser.add_argument("--work", type=pathlib.Path, required=True)
parser.add_argument("--m32", action="store_true")
args = parser.parse_args()
args.work.mkdir(parents=True, exist_ok=True)
ROOT = pathlib.Path(__file__).resolve().parent
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("boot_link", ROOT / "link.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=args.work)
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name)
        facts = self.path / "source/versions/test"
        facts.mkdir(parents=True)
        (facts / "symbol_addrs.txt").write_text("marker = 0x80000200; // rom:0x100\nend = 0x80400000; //\n")
        self.project = {"source_pin": "a" * 40, "version": "test", "rdram_bytes": "0x400000",
            "video_clock_hz": 48681812, "rom_bytes": 4096,
            "dumps": [{"id": "test", "size": 4096, "sha256": "b" * 64, "supported": True}],
            "entry": "original_entry", "data": {"marker": "0x80000200", "end": "0x80400000"},
            "bindings": {"original_runtime": "host_runtime"},
            "stops": {"missing_runtime": "Fixture unresolved runtime identity"},
            "loads": [{"symbol": "marker", "rom": "0x100", "bytes": 15, "kind": "raw",
                       "evidence": "Fixture char text[15]"}]}

    def generate(self, project=None):
        manifest = self.path / "input.json"
        manifest.write_text(json.dumps(project or self.project))
        module.generate(manifest, self.path / "source", self.path / "output")

    def test_generation_sanitizes_inherited_sdk_input(self):
        unit = self.path / "source/src/SDK/format.CPP"
        unit.parent.mkdir(parents=True)
        unit.write_text("int func_802BD320_de(char *buffer, const char *format, ...) {\n"
                        "    ORIGINAL_LIBRARY_MUST_NOT_COMPILE;\n}\n"
                        "void original_internal(void) {\n    ORIGINAL_LIBRARY_MUST_NOT_COMPILE;\n}\n")
        self.project["sources"] = ["src/SDK/format.CPP"]
        self.generate()
        code = (self.path / "output/bindings.c").read_text()
        script = (self.path / "output/bindings.ld").read_text()
        self.assertIn("func_802BD320_de = n64c_sprintf;", script)
        self.assertIn('n64c_boot_missing("original_internal")', code)
        self.assertNotIn("ORIGINAL_LIBRARY_MUST_NOT_COMPILE", code)
        self.assertEqual(self.project["sources"], ["src/SDK/format.CPP"])

    def test_real_link_and_terminal_call(self):
        self.project["bindings"]["host_runtime"] = "host_runtime"
        self.generate()
        driver = self.path / "driver.c"
        driver.write_text(r'''#include "boot.h"
#include <stdint.h>
#include <stdio.h>
extern char marker, end;
extern int original_runtime(int);
extern void missing_runtime(void);
int host_runtime(int input) { return input + 7; }
int original_entry(void) {
    if ((uintptr_t)&marker != 0x80000200u || (uintptr_t)&end != 0x80400000u) return 11;
    if (original_runtime(6) != 13 || n64c_boot_load_count != 1) return 12;
    if (n64c_boot_loads[0].ram != 512 || n64c_boot_loads[0].bytes != 15) return 13;
    if (n64c_boot_system.initialized && ((uintptr_t)n64c_boot_system.initialized != 0x80001000u ||
        (uintptr_t)n64c_boot_system.nmi_buffer != 0x80001180u || n64c_boot_tv_type != 1)) return 15;
    fputs("binding-ok\n", stderr);
    missing_runtime();
    return 14;
}
int main(void) { return n64c_boot_entry(); }
''')
        executable = self.path / "check"
        command = [args.compiler, "-no-pie", "-I" + str(ROOT), "-I" + str(ROOT.parent),
                   str(driver), str(self.path / "output/bindings.c"), str(ROOT / "missing.c"),
                   "-Wl,-T," + str(self.path / "output/bindings.ld"), "-o", str(executable)]
        if args.m32:
            command.insert(1, "-m32")
        subprocess.run(command, check=True, capture_output=True, text=True)
        result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertEqual(result.stderr, "binding-ok\nboot-missing: missing_runtime\n")

    def test_resource_descriptors_reach_the_boot_tables(self):
        # ROM resources the game DMAs later: PI byte-order descriptors only, in n64c_boot_resources.
        self.project["resources"] = [
            {"name": "header", "rom": "0x400", "bytes": 8, "kind": "swap32", "evidence": "Fixture u32 pair"},
            {"name": "halves", "rom": "0x408", "bytes": 6, "kind": "swap16", "evidence": "Fixture s16[3]"},
            {"name": "records", "rom": "0x410", "bytes": 24, "kind": "fields", "fields": "4 2 2 4",
             "evidence": "Fixture record table"},
            {"name": "bytes", "rom": "0x430", "bytes": 5, "kind": "raw", "evidence": "Fixture text"}]
        self.generate()
        code = (self.path / "output/bindings.c").read_text()
        self.assertIn('{"records", 1040u, 0u, 24u, N64C_RES_STRUCT, "4 2 2 4"},', code)
        self.assertIn('{"header", 1024u, 0u, 8u, N64C_RES_SWAP32, 0},', code)
        driver = self.path / "resources.c"
        driver.write_text(r'''#include "boot.h"
#include <stdint.h>
#include <string.h>
extern char marker, end;
extern int original_runtime(int);
extern void missing_runtime(void);
int host_runtime(int input) { return input + 7; }
int original_entry(void) {
    if (n64c_boot_resource_count != 4 || n64c_boot_load_count != 1) return 21;
    if (n64c_boot_resources[0].rom != 0x400 || n64c_boot_resources[0].kind != N64C_RES_SWAP32) return 22;
    if (n64c_boot_resources[1].kind != N64C_RES_SWAP16 || n64c_boot_resources[1].bytes != 6) return 23;
    if (n64c_boot_resources[2].kind != N64C_RES_STRUCT || strcmp(n64c_boot_resources[2].fields, "4 2 2 4")) return 24;
    if (n64c_boot_resources[3].kind != N64C_RES_RAW || n64c_boot_resources[3].fields) return 25;
    for (size_t i = 0; i < n64c_boot_resource_count; i++) if (n64c_boot_resources[i].ram) return 26;
    return 0;
}
int main(void) { return n64c_boot_entry(); }
''')
        self.project["bindings"]["host_runtime"] = "host_runtime"
        self.generate()
        executable = self.path / "check_resources"
        command = [args.compiler, "-no-pie", "-I" + str(ROOT), "-I" + str(ROOT.parent),
                   str(driver), str(self.path / "output/bindings.c"), str(ROOT / "missing.c"),
                   "-Wl,-T," + str(self.path / "output/bindings.ld"), "-o", str(executable)]
        if args.m32:
            command.insert(1, "-m32")
        subprocess.run(command, check=True, capture_output=True, text=True)
        result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_resource_descriptor_rejections(self):
        self.project["resources"] = [
            {"name": "table", "rom": "0x400", "bytes": 16, "kind": "swap32", "evidence": "Fixture u32 table"}]
        self.generate()
        for index, change in enumerate([
                lambda r: r.update(kind="u64"),
                lambda r: r.update(bytes=0),
                lambda r: r.update(bytes=15),
                lambda r: r.update(rom=4090),
                lambda r: r.update(evidence=""),
                lambda r: r.update(name="bad name"),
                lambda r: r.update(fields="4"),
                lambda r: r.update(kind="fields"),
                lambda r: r.update(kind="fields", fields=""),
                lambda r: r.update(kind="fields", fields="4 p"),
                lambda r: r.update(kind="fields", fields="9"),
                lambda r: r.update(kind="fields", fields="44 4", bytes=16),
                lambda r: r.update(kind="fields", fields=" ".join("1" * 64)),
                lambda r: r.update(rom=0x100, bytes=16)]):  # overlaps the load of marker
            with self.subTest(case=index):
                bad = copy.deepcopy(self.project)
                change(bad["resources"][0])
                with self.assertRaises(ValueError):
                    self.generate(bad)
        bad = copy.deepcopy(self.project)
        bad["resources"].append(dict(bad["resources"][0], name="again", rom="0x408", bytes=8))
        with self.assertRaises(ValueError):
            self.generate(bad)

    def test_tagged_resource_descriptor(self):
        # records of different sizes back to back; the u16 at +4 picks the field string
        row = {"name": "pages", "rom": "0x440", "bytes": 64, "kind": "tagged", "evidence": "Fixture tagged records",
               "tag": {"offset": 4, "width": 2}, "layouts": {"1": "4 2 2", "2": "4 2 2 4 4"}}
        self.project["resources"] = [row]
        self.generate()
        code = (self.path / "output/bindings.c").read_text()
        self.assertIn('{"pages", 1088u, 0u, 64u, N64C_RES_TAGGED, "4/2;1=4 2 2;2=4 2 2 4 4"},', code)
        for index, change in enumerate([
                lambda r: r.update(tag={"offset": 4, "width": 3}),
                lambda r: r.update(tag={"offset": -1, "width": 2}),
                lambda r: r.update(tag={"offset": 4}),
                lambda r: r.update(tag=None),
                lambda r: r.update(layouts={}),
                lambda r: r.update(layouts=None),
                lambda r: r.update(layouts={"1": "4"}),                  # shorter than its tag
                lambda r: r.update(layouts={"01": "4 2 2"}),
                lambda r: r.update(layouts={"x": "4 2 2"}),
                lambda r: r.update(layouts={"1": "4 p 2"}),
                lambda r: r.update(layouts={str(n): "4 2 2" for n in range(17)}),
                lambda r: r.update(layouts={"1": " ".join("1" * 64)}),
                lambda r: r.update(fields="4 2 2"),
                lambda r: r.update(kind="fields", fields="4 2 2")]):    # fields row with tag/layouts
            with self.subTest(case=index):
                bad = copy.deepcopy(self.project)
                change(bad["resources"][0])
                with self.assertRaises(ValueError):
                    self.generate(bad)
    def test_counted_and_sized_graph(self):
        expr = {"fields": [[0, 2]], "multiply": 1, "divide": 1}
        row = {"root": "Array", "types": {
            "Array": {"counted": {"head": "Head", "element": "Item", "count": expr}},
            "Head": {"fields": "2 2"},
            "Item": {"fields": "@0:Chain 1111"},
            "Chain": {"chain": {"head": "Head", "element": "Block", "count": expr,
                                "payload": {"fields": [[0, 4], [4, 4]], "multiply": 5, "divide": 2}}},
            "Block": {"fields": "4 4"}}}
        self.assertEqual(module.graph_program(row, "fixture"),
                         "A:3,4:1/1:0/2;F:4 4;C:3,1:1/1:0/2:5/2:0/4,4/4;F:2 2;F:@0:2 1111")
        for value in [
                {"fields": [[1, 2]], "multiply": 1, "divide": 1},
                {"fields": [[0, 4]], "multiply": 1, "divide": 1},
                {"fields": [], "multiply": 1, "divide": 1},
                {"fields": [[0, 2]], "multiply": 1, "divide": 0},
                {"fields": [[0, 2]], "multiply": True, "divide": 1},
                {"fields": [[0, 2]], "multiply": 0x100000000, "divide": 1},
                {"fields": [[0, 2]] * 9, "multiply": 1, "divide": 1}]:
            bad = copy.deepcopy(row)
            bad["types"]["Array"]["counted"]["count"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.graph_program(bad, "fixture")
        bad = copy.deepcopy(row)
        bad["types"]["Block"] = {"variants": {"tag": {"offset": 0, "width": 1},
                                            "layouts": {"0": "Head"}}}
        with self.assertRaises(ValueError):
            module.graph_program(bad, "fixture")
    def test_unfacted_rnc_streams(self):
        # an RNC stream (sane header, packed bytes inside the ROM) that no blob fact or row covers is listed; streams
        # inside a fact, a row, or with an impossible header are not
        (self.path / "source/blobs.toml").write_text('[blob."known"]\ncodec = "rnc1"\nrom."test" = [[0x200, 0x240]]\n')
        rom = bytearray(0x1000)
        def stream(at, method, unpacked, packed):
            rom[at:at + 12] = b"RNC" + bytes([method]) + unpacked.to_bytes(4, "big") + packed.to_bytes(4, "big")
        stream(0x200, 1, 0x40, 0x20)   # a blob fact covers it
        stream(0x110, 1, 0x40, 0x20)   # inside the loads row (0x100, 15 bytes)
        stream(0x300, 1, 0x40, 0x20)   # no fact: listed
        stream(0x400, 2, 0x80, 0x10)   # no fact: listed
        stream(0x500, 1, 0, 0x20)      # unpacked size 0: no stream
        stream(0xFF0, 1, 0x40, 0x20)   # packed bytes past the ROM: no stream
        project = copy.deepcopy(self.project)
        project["loads"][0]["bytes"] = 0x20
        self.assertEqual(module.unfacted_streams(bytes(rom), project, self.path / "source"),
                         [(0x300, 1, 0x40, 0x20), (0x400, 2, 0x80, 0x10)])

    def test_blob_layouts_from_blob_facts(self):
        # every RNC blob fact of the version is a packed stream; blob_layouts rows type an RNC stream's unpacked
        # bytes or an uncompressed blob's ROM bytes (GRAPH programs compiled from named types, root first)
        (self.path / "source/blobs.toml").write_text(
            '[blob."pack.a"]\ncodec = "rnc1"\nrom."test" = [[0x500, 0x540]]\n'
            '[blob."pack.b"]\ncodec = "rnc2"\nrom."test" = [[0x540, 0x560]]\nrom."other" = [[0x10, 0x20]]\n'
            '[blob."plain"]\ncodec = "none"\nrom."test" = [[0x600, 0x640]]\n'
            '[blob."bundle.1"]\ncodec = "none"\nformat = "bundle"\nrom."test" = [[0x680, 0x6a0]]\n'
            '[blob."bundle.2"]\ncodec = "none"\nformat = "bundle"\nrom."test" = [[0x6a0, 0x6c0]]\n'
            '[blob."split"]\ncodec = "none"\nrom."test" = [[0x700, 0x710], [0x720, 0x730]]\n'
            '[blob."elsewhere"]\ncodec = "rnc1"\nrom."other" = [[0x700, 0x740]]\n')
        graph = {"blob": "pack.a", "kind": "graph", "root": "Box", "evidence": "Fixture container",
                 "types": {"Box": {"directory": [None, "Items"]}, "Items": {"leaf": "Item"},
                           "Item": {"fields": "2 2 @0:Pair"}, "Pair": {"fields": "4 2 2"}}}
        tagged = {"blob": "plain", "kind": "graph", "root": "Dir", "evidence": "Fixture tagged directory",
                  "types": {"Dir": {"directory": {"tag": {"offset": 0, "width": 4}, "layouts": {"0": "A", "3": "B"}}},
                            "A": {"fields": "4 4"}, "B": {"directory": {"each": "C"}}, "C": {"fields": "2 2"}}}
        bundles = {"format": "bundle", "kind": "graph", "root": "Outer", "evidence": "Fixture nested streams",
                   "types": {"Outer": {"directory": {"each": "Stream"}}, "Stream": {"packed": "Inner"},
                             "Inner": {"planes": "Half"}, "Half": {"fields": "2"}}}
        self.project["blob_layouts"] = [graph, tagged, bundles]
        self.generate()
        code = (self.path / "output/bindings.c").read_text()
        self.assertIn('{"pack.a", 1280u, 0u, 64u, N64C_RES_GRAPH, "D:-,2;F:2 2 @0:3;L:1;F:4 2 2"},', code)
        self.assertIn('{"plain", 1536u, 0u, 64u, N64C_RES_GRAPH, "G:0/4:0=1,3=2;F:4 4;E:3;F:2 2"},', code)
        self.assertIn('{"bundle.1", 1664u, 0u, 32u, N64C_RES_GRAPH, "E:3;F:2;X:1;P:2"},', code)
        self.assertIn('{"bundle.2", 1696u, 0u, 32u, N64C_RES_GRAPH, "E:3;F:2;X:1;P:2"},', code)
        self.assertIn("n64c_boot_packed_count", code)
        self.assertNotIn('"elsewhere"', code)
        packed = code[code.index("n64c_boot_packed[]"):]
        self.assertNotIn('"plain"', packed)                     # an uncompressed blob is a resource, not a stream
        for index, change in enumerate([
                lambda rows: rows[0].update(blob="missing"),
                lambda rows: rows[0].update(blob="split"),
                lambda rows: rows[0].update(blob="elsewhere"),
                lambda rows: rows.append(dict(rows[0])),
                lambda rows: rows[0].update(evidence=""),
                lambda rows: rows[0].update(kind="alctl", root=None, types=None),
                lambda rows: rows[0].update(root="Nowhere"),
                lambda rows: rows[0]["types"].update(Item={"fields": "2 2 @0:Nowhere"}),
                lambda rows: rows[0]["types"].update(Items={"leaf": "Box"}),
                lambda rows: rows[0]["types"].update(Item={"fields": "2 p"}),
                lambda rows: rows[0]["types"].update(Item={"fields": ""}),
                lambda rows: rows[0]["types"].update(Box={"directory": []}),
                lambda rows: rows[0]["types"].update(Box={"tree": [None]}),
                lambda rows: rows[1]["types"]["Dir"]["directory"].update(tag={"offset": 0, "width": 3}),
                lambda rows: rows[1]["types"]["Dir"]["directory"].update(layouts={"01": "A"}),
                lambda rows: rows[2].update(format="no such format"),
                lambda rows: rows[2].update(blob="plain"),                       # blob and format
                lambda rows: rows[2]["types"].update(Stream={"packed": 3}),
                lambda rows: rows[2]["types"].update(Inner={"planes": "Outer"}),       # planes of a directory
                lambda rows: rows[2]["types"].update(Half={"fields": "2 @0:Half"}),    # planes with a reference
                lambda rows: rows.append(dict(rows[2], format="bundle"))]):
            with self.subTest(case=index):
                bad = copy.deepcopy(self.project)
                change(bad["blob_layouts"])
                with self.assertRaises((ValueError, TypeError, KeyError)):
                    self.generate(bad)
        # the byte-order gate: blobs no row types are UNTYPED (the first DMA stops the boot); a byte_streams format is
        # data read byte by byte
        self.assertIn('{"split", 1792u, 0u, 16u, N64C_RES_UNTYPED, "split"},', code)
        self.assertIn('{"pack.b", 1344u, 0u, 32u, N64C_RES_UNTYPED, "pack.b"},', code)
        streams = copy.deepcopy(self.project)
        streams["byte_streams"] = [{"format": "samples", "evidence": "Fixture byte data"}]
        (self.path / "source/blobs.toml").write_text((self.path / "source/blobs.toml").read_text() +
                                                     '[blob."sample.1"]\ncodec = "none"\nformat = "samples"\n'
                                                     'rom."test" = [[0x780, 0x790]]\n')
        self.generate(streams)
        code = (self.path / "output/bindings.c").read_text()
        self.assertIn('{"sample.1", 1920u, 0u, 16u, N64C_RES_BYTE_STREAM, 0},', code)
        classes = module.byte_order(streams, self.path / "source")
        self.assertEqual(classes["sample.1"][0], "bytes")
        self.assertEqual(classes["bundle.1"][0], "layout")
        self.assertEqual(classes["split"][0], "untyped")
        for index, change in enumerate([
                lambda rows: rows.append({"format": "no such format", "evidence": "Fixture"}),
                lambda rows: rows.append({"format": "bundle", "evidence": "Fixture"}),     # has a layout
                lambda rows: rows.append(dict(rows[0])),
                lambda rows: rows[0].update(evidence=" ")]):
            with self.subTest(streams=index):
                bad = copy.deepcopy(streams)
                change(bad["byte_streams"])
                with self.assertRaises(ValueError):
                    self.generate(bad)
        bad = copy.deepcopy(self.project)                      # a graph is no layout for a plain ROM resource row
        bad["resources"] = [{"name": "box", "rom": "0x400", "bytes": 16, "kind": "graph", "root": "Box",
                             "types": graph["types"], "evidence": "Fixture"}]
        with self.assertRaises(ValueError):
            self.generate(bad)
        bad = copy.deepcopy(self.project)                      # a stream overlapping a load
        bad["loads"][0].update(rom="0x500")
        with self.assertRaises(ValueError):
            self.generate(bad)

    def test_byte_stream_transport_ranges(self):
        (self.path / "source/blobs.toml").write_text(
            '[blob."sequence"]\ncodec = "none"\nformat = "sequence bytes"\n'
            'rom."test" = [[0x600,0x604], [0x700,0x720]]\n')
        self.project["resources"] = [{"name": "header", "rom": "0x700", "bytes": 8,
                                     "kind": "swap32", "evidence": "Fixture native header"}]
        self.project["byte_streams"] = [
            {"format": "sequence bytes", "evidence": "Fixture byte reader"},
            {"name": "code_cache", "rom": "0x500", "bytes": 32, "evidence": "Fixture paging transport"}]
        self.generate()
        code = (self.path / "output/bindings.c").read_text()
        self.assertIn('{"sequence", 1536u, 0u, 4u, N64C_RES_BYTE_STREAM, 0},', code)
        self.assertIn('{"sequence", 1792u, 0u, 32u, N64C_RES_BYTE_STREAM, 0},', code)
        self.assertIn('{"header", 1792u, 0u, 8u, N64C_RES_SWAP32, 0},', code)
        self.assertIn('{"code_cache", 1280u, 0u, 32u, N64C_RES_BYTE_STREAM, 0},', code)
        for change in [
                {"bytes": 0}, {"rom": "0xfffffff0", "bytes": 32},
                {"rom": 4090, "bytes": 16}, {"name": "bad:name"}, {"evidence": ""},
                {"format": "sequence bytes"}]:
            bad = copy.deepcopy(self.project)
            bad["byte_streams"][1].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.generate(bad)
        bad = copy.deepcopy(self.project)
        bad["byte_streams"].append(dict(bad["byte_streams"][1]))
        with self.assertRaises(ValueError):
            self.generate(bad)

    def test_audio_control_bank_descriptor(self):
        # a control bank container read in place: the PI layer walks its record graph, so the row has no fields
        self.project["resources"] = [{"name": "banks", "rom": "0x440", "bytes": 6, "kind": "alctl",
                                      "evidence": "Fixture control bank"}]
        self.generate()
        code = (self.path / "output/bindings.c").read_text()
        self.assertIn('{"banks", 1088u, 0u, 6u, N64C_RES_ALCTL, 0},', code)
        bad = copy.deepcopy(self.project)
        bad["resources"][0]["fields"] = "4"
        with self.assertRaises(ValueError):
            self.generate(bad)

    def test_system_globals_and_rejections(self):
        fields = ["initialized", "tv_type", "reset_type", "memory_size", "video_clock", "clock_rate", "nmi_buffer"]
        facts = self.path / "source/versions/test/symbol_addrs.txt"
        with facts.open("a") as output:
            for index, field in enumerate(fields):
                address = 0x80001000 + index * 64
                output.write(f"{field} = 0x{address:08x}; //\n")
                self.project["data"][field] = address
        self.project["system"] = {"globals": {field: field for field in fields}, "tv_type": 1, "reset_type": 0}
        self.test_real_link_and_terminal_call()
        for change in [lambda p: p["system"].update(tv_type=3),
                       lambda p: p["system"]["globals"].pop("clock_rate"),
                       lambda p: p["system"]["globals"].update(clock_rate="initialized")]:
            bad = copy.deepcopy(self.project)
            change(bad)
            with self.assertRaises(ValueError):
                self.generate(bad)

    def test_address_named_data(self):
        # D_auto_<address> data binds at the address its name states when the pinned facts omit it.
        self.project["data"]["D_auto_80000300"] = "0x80000300"
        self.generate()
        self.assertIn("D_auto_80000300 = 0x80000300;", (self.path / "output/bindings.ld").read_text())
        for symbol, address in [("D_auto_80000300", "0x80000304"), ("D_auto_00000300", "0x80000300"),
                                ("D_80000300", "0x80000300")]:
            bad = copy.deepcopy(self.project)
            bad["data"].pop("D_auto_80000300")
            bad["data"][symbol] = address
            with self.assertRaises(ValueError):
                self.generate(bad)
    def test_tlb_mapped_record_binds_its_virtual_address(self):
        # D_00XXXXXX at a KUSEG address (overlay data the game maps through its TLB) binds below the host image
        with (self.path / "source/versions/test/symbol_addrs.txt").open("a") as facts:
            facts.write("D_0044A850 = 0x0044B4A0; //\nD_80900000 = 0x80900000; //\n")
        self.project["data"]["D_0044A850"] = "0x0044B4A0"
        self.generate()
        self.assertIn("D_0044A850 = 0x0044b4a0;", (self.path / "output/bindings.ld").read_text())
        bad = copy.deepcopy(self.project)
        bad["data"]["D_80900000"] = "0x80900000"  # past RDRAM, not a TLB record
        with self.assertRaises(ValueError):
            self.generate(bad)
    def test_invalid_input_preserves_existing_outputs(self):
        self.generate()
        output = self.path / "output"
        original = {p.name: p.read_bytes() for p in output.iterdir()}
        changes = [
            lambda p: p["data"].update(marker="0x80000204"),
            lambda p: p["bindings"].update(marker="host_runtime"),
            lambda p: p["bindings"].update(original_runtime="bad();"),
            lambda p: p["stops"].update(missing_runtime=""),
            lambda p: p["loads"][0].update(bytes=0),
            lambda p: p["loads"][0].update(kind="unknown"),
            lambda p: p["loads"][0].update(kind="u32"),
            lambda p: p["loads"][0].update(rom=4090),
            lambda p: p["loads"][0].update(evidence=""),
            lambda p: p["loads"].append(copy.deepcopy(p["loads"][0])),
            lambda p: p["dumps"][0].update(sha256="wrong"),
            lambda p: p["dumps"][0].update(size=8192),
            lambda p: p["dumps"][0].update(supported=False),
            lambda p: p.update(source_pin="wrong"),
            lambda p: p.update(loads=[]),
            lambda p: p.update(audio_abi={"flavour": "mp3", "evidence": "x"}),
            lambda p: p.update(audio_abi={"flavour": "audio", "evidence": ""}),
        ]
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                project = copy.deepcopy(self.project)
                change(project)
                with self.assertRaises(ValueError):
                    self.generate(project)
                self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, original)


unittest.main(argv=[sys.argv[0]])
