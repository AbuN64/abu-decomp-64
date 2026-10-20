# SPDX-License-Identifier: GPL-3.0-or-later
"""lockstep.py on synthetic oracle/port recordings, and the port recorder (trace.c, N64C_LOCKSTEP=1) on a fixture."""
import argparse
import contextlib
import importlib.util
import io
import json
import pathlib
import struct
import subprocess
import sys
import tempfile
import unittest

parser = argparse.ArgumentParser()
parser.add_argument("--compiler", default="gcc")
parser.add_argument("--work", type=pathlib.Path, required=True)
parser.add_argument("--m32", action="store_true")
args = parser.parse_args()
args.work.mkdir(parents=True, exist_ok=True)
ROOT = pathlib.Path(__file__).resolve().parent
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("lockstep", ROOT / "lockstep.py")
ls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ls)
PAGES = 4
TRACE = "order\taddress\tsymbol\n1\t0x80001000\tfn_one\n2\t0x80001100\tfn_two\tfn_two_new\n" \
        "3\t0x80001200\tfn_three\n4\t0x80001300\tfn_four\n5\t0x80001400\tfn_five\n"
SYMBOLS = "fn_one = 0x80001000; //\nfn_two_new = 0x80001100; //\nD_80000100 = 0x80000100; //\n" \
          "D_80001800 = 0x80001800; //\nD_80002000 = 0x80002000; //\nD_80003000 = 0x80003000; //\n"


class Recorder:
    """Writes one side's lockstep.jsonl and pages.bin as the C recorders do."""

    def __init__(self, path, side):
        self.path = path
        path.mkdir(parents=True)
        self.side = side
        self.lines = [{"k": "meta", "side": side, "ram": PAGES * 4096, "page": 4096,
                       "bytes": "canonical-be" if side == "oracle" else "native", "hash": "fnv1a64-le64"}]
        self.last, self.stored, self.store, self.n = {}, set(), b"", 0

    def hit(self, key, ram, sp=0x80003F00, thr=1):
        pages = []
        for pg in range(PAGES):
            data = bytes(ram[pg * 4096:(pg + 1) * 4096])
            h = ls.page_hash(data)
            if self.n and self.last.get(pg) == h:
                continue
            self.last[pg] = h
            if h not in self.stored:
                self.stored.add(h)
                self.store += struct.pack("<IIQ", pg, 0, h) + data
            pages.append([pg, f"{h:016x}"])
        rec = {"k": "hit", "n": self.n, "thr": thr, "pages": pages, "sp": f"{sp:08x}"}
        rec.update({"fn": key} if self.side == "oracle" else {"order": key})
        self.lines.append(rec)
        self.n += 1

    def call(self, key, i, words, sp, f12=0, f14=0):
        rec = {"k": "call", "i": i, "thr": 1, "sp": f"{sp:08x}", "a": [f"{w:08x}" for w in words]}
        if self.side == "oracle":
            rec.update(fn=key, f12=f"{f12:016x}", f14=f"{f14:016x}")
        else:
            rec.update(order=key)
        self.lines.append(rec)

    def ret(self, key, i, r, fp=None):
        rec = {"k": "ret", "i": i, "thr": 1, "r": [f"{w:08x}" for w in r]}
        if self.side == "oracle":
            rec.update(fn=key, f0=f"{fp or 0:016x}")
        else:
            rec.update(order=key, st0=None if fp is None else f"{fp:016x}")
        self.lines.append(rec)

    def close(self):
        (self.path / "lockstep.jsonl").write_text("".join(json.dumps(r) + "\n" for r in self.lines))
        (self.path / "pages.bin").write_bytes(self.store)


class Ram:
    """The same guest memory in the oracle's canonical bytes and the port's native ones, written by type."""

    def __init__(self):
        self.oracle, self.port = bytearray(PAGES * 4096), bytearray(PAGES * 4096)

    def u32(self, address, value, port_value=None):
        at = address - 0x80000000
        self.oracle[at:at + 4] = value.to_bytes(4, "big")
        self.port[at:at + 4] = (value if port_value is None else port_value).to_bytes(4, "little")

    def u16(self, address, value):
        at = address - 0x80000000
        self.oracle[at:at + 2] = value.to_bytes(2, "big")
        self.port[at:at + 2] = value.to_bytes(2, "little")

    def u8(self, address, value):
        at = address - 0x80000000
        self.oracle[at] = self.port[at] = value

    def f64(self, address, value):
        at = address - 0x80000000
        self.oracle[at:at + 8] = struct.pack(">d", value)
        self.port[at:at + 8] = struct.pack("<d", value)


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=args.work)
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name)
        (self.path / "trace.tsv").write_text(TRACE)
        (self.path / "symbols.txt").write_text(SYMBOLS)
        self.oracle = Recorder(self.path / "oracle", "oracle")
        self.port = Recorder(self.path / "port", "port")
        self.ram = Ram()
        self.names = {1: "fn_one", 2: "fn_two", 3: "fn_three", 4: "fn_four", 5: "fn_five"}

    def both(self, key, sp=0x80003F00):
        self.oracle.hit(self.names[key], self.ram.oracle, sp)
        self.port.hit(key, self.ram.port)

    def run_compare(self, *extra):
        self.oracle.close()
        self.port.close()
        argv = ["compare", "--oracle", str(self.path / "oracle"), "--port", str(self.path / "port"), "--trace",
                str(self.path / "trace.tsv"), "--symbols", str(self.path / "symbols.txt"), "--pak", "5", *extra]
        return ls.compare(ls.build_parser().parse_args(argv))

    def test_all_memory_arguments_and_returns_are_listed_with_exact_acceptance(self):
        self.both(1)
        # More than both former display limits (10 text, 200 JSON).
        for i in range(210):
            self.ram.u32(0x80001800 + i * 4, 1, port_value=2)
        self.both(2)
        self.both(3)
        self.both(4)
        (self.path / "types.tsv").write_text("name\treturn\tparams\nfn_three\tint32\tint32,int32\n")
        self.oracle.call("fn_three", 0, [3, 4, 0, 0], 0x80003EC0)
        self.port.call(3, 0, [7, 8, 0, 0, 0, 0, 0, 0], 0xFFFFD000)
        self.oracle.ret("fn_three", 0, [5, 0])
        self.port.ret(3, 0, [9, 0])
        accepts = self.path / "accept.tsv"
        header = "\t".join(ls.ACCEPT_FIELDS) + "\n"
        accepts.write_text(header + "memory\t2\tfn_two\t0x80001800\t00000001/00000002\tfixture reason\tfixture review\n")
        report = self.run_compare("--types", str(self.path / "types.tsv"), "--accept", str(accepts))
        self.assertEqual(len(report["memory"]), 210)
        self.assertEqual(len(report["calls"]), 3)
        self.assertEqual([r["why"].split()[0] for r in report["calls"]], ["arg0", "arg1", "return"])
        self.assertEqual(report["first"]["address"], "0x80001800")
        self.assertEqual(report["first_after_accepted"]["address"], "0x80001804")
        self.assertEqual(report["accepted"][0]["acceptance"]["reason"], "fixture reason")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            ls.print_report(report)
        self.assertEqual(output.getvalue().count("lockstep: memory order"), 210)
        self.assertEqual(output.getvalue().count("lockstep: argument order"), 2)
        self.assertEqual(output.getvalue().count("lockstep: return order"), 1)
        self.assertIn("first after accepted rows: order 2", output.getvalue())
        self.assertIn("fixture reason", output.getvalue())

    def test_acceptance_advances_only_reviewed_exact_values(self):
        self.both(1)
        self.ram.u32(0x80001800, 1, port_value=2)
        self.both(2)
        self.both(3)
        self.ram.u32(0x80001804, 3, port_value=4)
        self.both(4)
        self.both(5)
        report = self.run_compare()
        review = dict(zip(ls.ACCEPT_FIELDS, ["memory", 2, "fn_two", "0x80001800", "00000001/00000002", "known", "reviewer"]))
        ls.apply_acceptances(report, [review], [1, 2, 3, 4, 5])
        self.assertEqual(report["matched_through"], 1)
        self.assertEqual(report["matched_through_after_accepted"], 3)
        self.assertEqual(report["first_after_accepted"]["order"], 4)
        review["detail"] = "00000001/00000009"
        ls.apply_acceptances(report, [review], [1, 2, 3, 4, 5])
        self.assertEqual(report["matched_through_after_accepted"], 1)
        self.assertEqual(len(report["unmatched_accepts"]), 1)
        self.assertEqual(report["accepted"], [])
        self.assertNotIn("acceptance", report["memory"][0])

    def test_accept_file_requires_reason_reviewer_and_unique_exact_rows(self):
        path = self.path / "accept.tsv"
        header = "\t".join(ls.ACCEPT_FIELDS) + "\n"
        good = "memory\t2\tfn_two\t0x80001800\t00000001/00000002\treason\treviewer\n"
        for bad in [good.replace("reason", ""), good.replace("reviewer", ""),
                    good.replace("00000001/00000002", "*"), good + good,
                    good.replace("0x80001800", "0x80001800-0x80001900")]:
            with self.subTest(row=bad):
                path.write_text(header + bad)
                with self.assertRaises(ValueError):
                    ls.read_acceptances(path)

    def test_layouts_that_agree_are_not_divergences(self):
        self.ram.u32(0x80000100, 0x12345678)
        self.ram.u16(0x80000104, 0xBEEF)
        self.ram.u16(0x80000106, 0x0102)
        for i, b in enumerate(b"TEXT"):
            self.ram.u8(0x80000108 + i, b)
        self.ram.u16(0x8000010C, 0xA1B2)
        self.ram.u8(0x8000010E, 7)
        self.ram.u8(0x8000010F, 9)
        self.ram.f64(0x80000110, 1.5)
        self.both(1)
        self.ram.u32(0x80001800, 0xDEADBEEF)
        self.both(2)
        self.both(3)
        report = self.run_compare()
        self.assertIsNone(report["first"], report)
        self.assertEqual(report["matched_through"], 3)

    def test_persistent_divergence_names_function_page_symbol_and_words(self):
        self.both(1)
        self.both(2)
        self.ram.u32(0x80001810, 5, port_value=6)
        self.both(3)
        self.both(4)
        self.both(5)
        report = self.run_compare()
        first = report["first"]
        self.assertEqual((first["kind"], first["order"], first["function"]), ("memory", 3, "fn_three"))
        self.assertEqual((first["page"], first["symbol"]), ("0x80001000", "D_80001800+0x10"))
        self.assertTrue(first["oracle"].startswith("00000005"), first)
        self.assertTrue(first["port"].startswith("00000006"), first)
        self.assertEqual(report["matched_through"], 2)

    def test_transient_and_stack_and_host_pointer_words_do_not_count(self):
        self.both(1)
        self.ram.u32(0x80001900, 1, port_value=2)  # differs at one point only, then agrees
        self.both(2)
        self.ram.u32(0x80001900, 3)
        self.ram.u32(0x80003E00, 1, port_value=0)  # inside the oracle's guest stack page
        self.ram.u32(0x80001910, 0x80002100, port_value=0x5655A000)  # a shim's host pointer
        self.both(3)
        self.both(4)
        self.both(5)
        report = self.run_compare()
        self.assertIsNone(report["first"], report)
        self.assertEqual(report["transient_words"], 1)
        self.assertEqual(report["shim_words"], 1)
        self.assertEqual(report["matched_through"], 5)

    def test_ignore_rows_and_oracle_volatility_mask_words(self):
        (self.path / "ignore.tsv").write_text("D_80001800\tshim-owned queue\n")
        self.both(1)
        self.ram.u32(0x80001800, 1, port_value=2)
        self.both(2)
        self.both(3)
        report = self.run_compare("--ignore", str(self.path / "ignore.tsv"))
        self.assertIsNone(report["first"], report)
        self.assertIn(["0x80001800", "0x80002000", "shim-owned queue"], report["masked"])

    def test_return_divergence_needs_a_type_and_comes_first(self):
        (self.path / "types.tsv").write_text("name\treturn\tparams\nfn_two\tint32\tint32,ptr,float\n"
                                             "fn_three\tvoid\t\n")
        self.both(1)
        self.both(2)
        self.both(3)
        self.ram.u32(0x80001810, 5, port_value=6)
        self.both(4)
        self.both(5)
        # fn_two(7, &stack, 2.5f): equal; returns 1 vs garbage
        self.oracle.call("fn_two", 0, [7, 0x80003E80, 0x40200000, 0], 0x80003EC0, f12=0)
        self.port.call(2, 0, [7, 0xFFFFD010, 0x40200000, 0, 0, 0, 0, 0], 0xFFFFD000)
        self.oracle.ret("fn_two", 0, [1, 0])
        self.port.ret(2, 0, [0x0806B2C4, 0])
        self.oracle.ret("fn_three", 0, [9, 0])  # void: never compared
        report = self.run_compare("--types", str(self.path / "types.tsv"))
        first = report["first"]
        self.assertEqual((first["kind"], first["order"], first["function"]), ("call", 2, "fn_two"), report)
        self.assertEqual(first["why"], "return int32 0x00000001 vs port 0x0806b2c4")
        self.assertEqual(report["matched_through"], 1)

    def test_float_and_double_arguments_follow_o32(self):
        self.assertEqual(ls.o32_slots(["float", "float", "int32"]), ([("f", 12), ("f", 14), ("a", 2)], [0, 4, 8]))
        self.assertEqual(ls.o32_slots(["int32", "double"]), ([("a", 0), ("a", 2)], [0, 4]))
        self.assertEqual(ls.o32_slots(["double", "int32", "int32", "int32"]),
                         ([("f", 12), ("a", 2), ("a", 3), None], [0, 8, 12, 16]))
        matcher = ls.Matcher(PAGES * 4096, ([], {}), [])
        oc = {"a": ["0", "0", "0", "0"], "f12": f"{struct.unpack('<Q', struct.pack('<d', 2.0))[0]:016x}",
              "f14": "0", "sp": "80003f00"}
        lo, hi = struct.unpack("<II", struct.pack("<d", 2.0))
        pc = {"a": [f"{lo:08x}", f"{hi:08x}"] + ["0"] * 6, "sp": "ffffd000"}
        self.assertEqual(ls.compare_call(matcher, ["double"], False, oc, pc), [])
        pc["a"][0] = "00000001"
        self.assertIn("arg0 double", ls.compare_call(matcher, ["double"], False, oc, pc)[0])

    def test_function_pointers_match_by_original_address(self):
        matcher = ls.Matcher(PAGES * 4096, ls.read_symbols(self.path / "symbols.txt"),
                             [(0x08049000, 0x40, "fn_one", False), (0x08049100, 0x40, "fn_two_new", True)])
        self.assertTrue(matcher.same_function(0x80001100, 0x08049100))
        self.assertFalse(matcher.same_function(0x80001000, 0x08049100))
        page_o, page_p = bytearray(4096), bytearray(4096)
        page_o[0:4] = (0x80001000).to_bytes(4, "big")
        page_p[0:4] = (0x08049000).to_bytes(4, "little")
        self.assertEqual(matcher.page_diff(bytes(page_o), bytes(page_p)), {})
        self.assertTrue(matcher.is_local(["fn_two", "fn_two_new"]))

    def test_native_data_pointers_match_by_original_address(self):
        matcher = ls.Matcher(PAGES * 4096, ls.read_symbols(self.path / "symbols.txt"), [],
                             [(0x0805A000, 0x100, "D_80001800", True), (0x0805B000, 0x20, "rw_table.12", False)])
        self.assertTrue(matcher.same_address(0x80001808, 0x0805A008))
        self.assertTrue(matcher.same_address(0x80001900, 0x0805A100))  # one past the end
        self.assertFalse(matcher.same_address(0x80001808, 0x0805A00C))
        self.assertIsNone(matcher.port_data(0x0805B000))  # no original address for the name
        self.assertFalse(matcher.host_pointer(0x80001808, 0x0805A008))

    def test_calls_without_a_prototype_are_not_compared(self):
        matcher = ls.Matcher(PAGES * 4096, ([], {}), [])
        oc = {"a": ["1", "2", "3", "4"], "f12": "0", "f14": "0", "sp": "80003f00"}
        pc = {"a": ["9"] * 8, "sp": "ffffd000"}
        self.assertEqual(ls.compare_call(matcher, None, False, oc, pc), [])
        self.assertIn("arg0 int32", ls.compare_call(matcher, ["int32"], False, oc, pc)[0])

    def test_returned_stack_pointers_and_other_threads_stacks_match(self):
        matcher = ls.Matcher(PAGES * 4096, ([], {}), [])
        oret, pret = {"r": ["80003e80", "0"]}, {"r": ["ffffd010", "0"]}
        self.assertIsNone(ls.compare_return(matcher, "ptr", oret, pret, 0x80003EC0, 0xFFFFD000))
        self.assertIsNotNone(ls.compare_return(matcher, "ptr", oret, pret))
        matcher.set_stacks({1: [0x80003EC0], 2: [0x80002F00]}, {7: [0xFFFFD000], 8: [0xB92FF000]})
        self.assertTrue(ls.same_word(matcher, "ptr", 0x80002E80, 0xB92FFF38, 0x80003EC0, 0xFFFFD000))
        self.assertFalse(ls.same_word(matcher, "ptr", 0x80001000, 0xB92FFF38, 0x80003EC0, 0xFFFFD000))

    def test_float_rounding_is_listed_not_counted(self):
        self.assertTrue(ls.near_float(0x441DE734, 0x441DE733))
        self.assertFalse(ls.near_float(0x441DE734, 0x441DE740))
        self.assertFalse(ls.near_float(5, 6))  # small integers are not floats
        self.both(1)
        self.ram.u32(0x80001810, 0x3F59E2CA, port_value=0x3F59E2C9)
        self.both(2)
        self.both(3)
        self.both(4)
        report = self.run_compare()
        self.assertIsNone(report["first"], report)
        self.assertEqual(report["fp_ulp_words"], 1)

    def test_setup_differences_count_once_the_game_writes_them(self):
        self.ram.u32(0x80001820, 0, port_value=0x77)  # the shim wrote it before the first game function
        self.ram.u32(0x80001830, 0, port_value=0x55)
        self.both(1)
        self.both(2)
        self.ram.u32(0x80001830, 3, port_value=4)  # the game writes it and the port disagrees
        self.both(3)
        self.both(4)
        self.both(5)
        report = self.run_compare()
        self.assertEqual(report["setup_words"], 1)
        self.assertEqual((report["first"]["order"], report["first"]["address"]), (3, "0x80001830"))
        self.assertEqual(report["matched_through"], 2)

    def test_rom_boot_copy_is_masked(self):
        rom = bytearray(0x1000 + 0x100000)
        rom[0:4] = bytes.fromhex("80371240")
        rom[0x1000 + 0x1400:0x1000 + 0x1404] = bytes.fromhex("27bdffc8")  # IPL3 puts it at 0x80001800
        (self.path / "rom.z64").write_bytes(rom)
        self.ram.oracle[0x1800:0x1804] = bytes.fromhex("27bdffc8")
        self.ram.u32(0x80001900, 2, port_value=1)  # differs after the first point: counted
        self.both(1)
        self.both(2)
        self.ram.u32(0x80001904, 2, port_value=1)
        self.both(3)
        self.both(4)
        self.both(5)
        report = self.run_compare("--rom", str(self.path / "rom.z64"))
        self.assertEqual(report["boot_copy_words"], 1)
        self.assertEqual(report["setup_words"], 1)
        self.assertEqual(report["first"]["address"], "0x80001904")
        v64 = bytearray(rom)
        v64[0::2], v64[1::2] = rom[1::2], rom[0::2]
        (self.path / "rom.v64").write_bytes(v64)
        self.assertEqual(ls.read_boot_copy(self.path / "rom.v64"), ls.read_boot_copy(self.path / "rom.z64"))

    def test_aux_info_kinds(self):
        typedefs = {"s32": "int", "f32": "float", "u8": "unsigned char", "Vec": "struct", "OSTime": "u64",
                    "u64": "unsigned long long"}
        text = ("/* a.c:3:NF */ extern s32 fn_one (f32 x, u8 *p, OSTime t, ...); /* (x, p, t) */\n"
                "/* a.c:9:OF */ extern void fn_two (); /* () */\n"
                "/* a.c:12:NC */ extern Vec fn_three (Vec, int (*)(void));\n")
        out = {}
        ls.parse_aux(text, {"fn_one", "fn_two", "fn_three"}, typedefs, out)
        self.assertEqual(out["fn_one"], ("int32", ["float", "ptr", "int64", "var"]))
        self.assertEqual(out["fn_two"], ("void", None))
        self.assertEqual(out["fn_three"], ("agg", ["agg", "ptr"]))


FIXTURE_GAME = r"""
#include <setjmp.h>
#include <stdio.h>
#define NI __attribute__((noinline))
extern unsigned char *ram_base;
static jmp_buf escape;
NI int add(int a, int b) { ram_base[0x1000] = (unsigned char)(a + b); return a + b; }
NI float half(float x) { return x * 0.5f; }
NI long long wide(int a) { return ((long long)a << 32) | 5; }
NI int depth(int n) { return n ? depth(n - 1) + 1 : add(n, 40); }
NI void leave(int n) { if (n > 0) leave(n - 1); if (n <= 0) longjmp(escape, 1); }
static __attribute__((always_inline)) inline int bump(int x) { return x + 3; }
static int (*volatile bump_ptr)(int) = bump;
NI int inline_parent(int x) { return bump(x * 2) + x; }
NI int run(void) {
    printf("add=%d\n", add(2, 3));
    printf("half=%g\n", (double)half(3.0f));
    printf("wide=%llx\n", wide(9));
    printf("depth=%d\n", depth(3));
    if (!setjmp(escape)) leave(2);
    printf("after=%d\n", add(1, 1));
    int sum = 0;
    for (int i = 0; i < 12; i++) sum += inline_parent(i);
    printf("inline=%d indirect=%d\n", sum, bump_ptr(7));
    return 0;
}
"""
FIXTURE_TABLE = r"""#include "trace.h"
const N64cTraceEntry n64c_trace_entries[] = {
    {1, 0x80001000u, 0, {"run"}}, {2, 0x80001100u, 0, {"add"}}, {3, 0x80001200u, 0, {"half"}},
    {4, 0x80001300u, 0, {"wide"}}, {5, 0x80001400u, 0, {"depth"}}, {6, 0x80001500u, 0, {"leave"}},
    {7, 0x80001600u, 0, {"bump"}}, {8, 0x80001700u, 0, {"inline_parent"}}};
const unsigned n64c_trace_count = 8;
/* This fixture executes original bodies only; no native bindings. */
const N64cShimEntry n64c_shim_entries[] = {{0, 0}};
const unsigned n64c_shim_count = 0;
"""
FIXTURE_DRIVER = r"""#include <stdio.h>
#include <string.h>
static unsigned char ram[0x400000] __attribute__((aligned(4096)));
unsigned char *ram_base = ram;
void *n64c_memory_ram(unsigned physical, unsigned bytes) { return physical + bytes <= sizeof ram ? ram + physical : 0; }
extern void n64c_trace_init(void);
extern void n64c_trace_summary(void);
extern int run(void);
int main(void) { memcpy(ram + 0x2000, "\x78\x56\x34\x12", 4); n64c_trace_init(); int r = run(); n64c_trace_summary();
    fflush(stdout); return r; }
"""


@unittest.skipUnless(args.m32, "the argument words and the return trampoline are i386")
class PortRecorderTests(unittest.TestCase):
    def test_fixture_records_points_calls_and_returns_and_still_behaves(self):
        with tempfile.TemporaryDirectory(dir=args.work) as tmp:
            path = pathlib.Path(tmp)
            (path / "game.c").write_text(FIXTURE_GAME)
            (path / "table.c").write_text(FIXTURE_TABLE)
            (path / "driver.c").write_text(FIXTURE_DRIVER)
            objects = []
            game = ["-O2", "-finstrument-functions", "-fno-omit-frame-pointer", "-fno-optimize-sibling-calls",
                    "-msse2", "-mfpmath=sse"]
            for source, extra in [(path / "game.c", game), (ROOT / "trace.c", ["-O2"]), (path / "table.c", []),
                                  (path / "driver.c", [])]:
                obj = path / (source.stem + ".o")
                subprocess.run([args.compiler, "-m32", "-Wall", "-Werror", *extra, "-I" + str(ROOT), "-c",
                                str(source), "-o", str(obj)], check=True, capture_output=True, text=True)
                objects.append(str(obj))
            exe = path / "fixture"
            subprocess.run([args.compiler, "-m32", "-no-pie", *objects, "-lpthread", "-o", str(exe)], check=True,
                           capture_output=True, text=True)
            out = path / "ls"
            env = {"N64C_LOCKSTEP": "1", "N64C_LOCKSTEP_DIR": str(out), "N64C_LOCKSTEP_WATCH": "0x80001000",
                   "PATH": "/usr/bin:/bin"}
            result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, env=env, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.split(), ["add=5", "half=1.5", "wide=900000005", "depth=43", "after=2",
                                                   "inline=234", "indirect=10"])
            self.assertIn("boot-lockstep: on, 1024 pages", result.stderr)
            self.assertIn("boot-lockstep: 8 points written", result.stderr)
            self.assertNotIn("boot-shim-bind:", result.stderr)
            self.assertNotIn("boot-shim:", result.stderr)
            side = ls.Side(out, lambda r: r.get("order"))
            self.assertEqual(sorted(side.points), [1, 2, 3, 4, 5, 6, 7, 8])
            self.assertEqual(side.meta["bytes"], "native")
            first = side.points[1][0]
            self.assertEqual(side.page(first[2])[:4], b"\x78\x56\x34\x12")
            self.assertEqual(ls.page_hash(side.page(first[2])), first[2])
            self.assertEqual(side.page(side.points[3][0][1])[0], 5)  # add wrote 5 before half's first hit
            self.assertEqual([side.calls[2][i]["a"][:2] for i in range(3)],
                             [["00000002", "00000003"], ["00000000", "00000028"], ["00000001", "00000001"]])
            self.assertEqual([side.rets[2][i]["r"][0] for i in range(3)], ["00000005", "00000028", "00000002"])
            self.assertEqual(side.rets[3][0]["st0"], f"{struct.unpack('<Q', struct.pack('<d', 1.5))[0]:016x}")
            self.assertEqual(side.rets[4][0]["r"], ["00000005", "00000009"])
            self.assertEqual([side.rets[5][i]["r"][0] for i in range(4)],
                             ["0000002b", "0000002a", "00000029", "00000028"])
            self.assertNotIn(6, side.rets)  # longjmp left every leave frame without a return
            # Twelve inlined entries have no bump frame. They must not consume its
            # call budget or hijack inline_parent's return with fabricated samples.
            records = [json.loads(line) for line in (out / "lockstep.jsonl").read_text().splitlines()]
            skipped = [r for r in records if r["k"] == "unframed"]
            self.assertEqual([r["order"] for r in skipped], [7])
            self.assertEqual(list(side.calls[7]), [0])
            self.assertEqual(side.calls[7][0]["a"][0], "00000007")
            self.assertEqual(side.rets[7][0]["r"][0], "0000000a")
            self.assertEqual([side.calls[8][i]["a"][0] for i in range(8)],
                             [f"{i:08x}" for i in range(8)])
            self.assertEqual([side.rets[8][i]["r"][0] for i in range(8)],
                             [f"{3 * i + 3:08x}" for i in range(8)])
            self.assertEqual([w["new"] for w in side.watch], ["00000005", "00000028", "00000002"])
            plain = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30,
                                   env={"PATH": "/usr/bin:/bin"}, cwd=path, check=False)
            self.assertEqual(plain.returncode, 0)
            self.assertNotIn("boot-lockstep", plain.stderr)
            self.assertFalse((path / "lockstep").exists())


unittest.main(argv=[sys.argv[0]])
