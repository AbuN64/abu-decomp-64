# SPDX-License-Identifier: GPL-3.0-or-later
import pathlib
import re
import shutil
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True
import gates  # noqa: E402

SAMPLE = '''# 1 "src/A/a.CPP"
typedef unsigned long long u64;
typedef u64 OSTime;
typedef struct { int x; } Box;
extern int two();
extern int half(void);
int two(int a, int b) { return a + b; }
static int local(u64 t, int k) { return (int)t + k; }
int caller(int v) {
    int g();
    two(v);
    two(v, 2, 3);
    return half() + two(v, v);
}
void sdkish(OSTime t, long long *p, int z) { }
'''


class WarningTests(unittest.TestCase):
    def test_return_type_uses_the_enclosing_function(self):
        text = ("src/A/a.CPP: In function 'func_8020_de':\n"
                "src/A/a.CPP:9:1: warning: control reaches end of non-void function [-Wreturn-type]\n")
        self.assertEqual(gates.parse_warnings(text, "/rw"), [
            ("gate-return-type", "func_8020_de", "src/A/a.CPP", 9, "control reaches end of non-void function")])

    def test_implicit_declaration_names_the_callee_and_other_flags_are_ignored(self):
        text = ("/rw/src/A/a.CPP: In function 'f':\n"
                "/rw/src/A/a.CPP:4:3: warning: implicit declaration of function 'osFoo' "
                "[-Wimplicit-function-declaration]\n"
                "/rw/src/A/a.CPP:5:3: warning: assignment to 'int' from 'char *' makes integer [-Wint-conversion]\n")
        rows = gates.parse_warnings(text, "/rw")
        self.assertEqual([(r[0], r[1], r[2]) for r in rows], [("gate-implicit-decl", "osFoo", "src/A/a.CPP")])


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.scan = gates.scan_text(SAMPLE)

    def test_definitions_and_static_flag(self):
        self.assertEqual(self.scan["defs"]["two"][:2], (2, False))
        self.assertEqual(self.scan["defs"]["local"][:2], (2, True))
        self.assertEqual(self.scan["defs"]["caller"][0], 1)

    def test_empty_parens_declarations_only(self):
        self.assertEqual(self.scan["knr_decls"], ["two"])

    def test_calls_skip_local_declarations_and_find_both_counts(self):
        calls = gates.calls_of(self.scan, ["two", "g"])
        self.assertEqual(sorted((c[0], c[1]) for c in calls), [("two", 1), ("two", 2), ("two", 3)])

    def test_long_long_parameters_follow_typedef_chains_and_skip_pointers(self):
        got = [(n, i) for n, i, *_ in self.scan["u64"]]
        self.assertEqual(sorted(got), [("local", 0), ("sdkish", 0)])
        self.assertEqual(gates.scan_text(SAMPLE, unit_is_sdk=True)["u64"], [])

    def test_stray_semicolons_do_not_break_the_reader(self):
        got = gates.scan_text('# 1 "x.c"\nint f(void) { return 1; };\n;\nint g(int a) { return a; }\n')
        self.assertEqual(sorted(got["defs"]), ["f", "g"])

    def test_source_line_numbers_come_from_linemarkers(self):
        self.assertEqual(self.scan["defs"]["two"][2:], ("src/A/a.CPP", 6))

    def test_knr_rows_join_definitions_across_units(self):
        scans = {"src/A/a.CPP": {"defs": self.scan["defs"], "calls": gates.calls_of(self.scan, ["two"])},
                 "src/B/b.CPP": {"defs": {}, "calls": [("two", 1, "other", "src/B/b.CPP", 3)]}}
        rows = gates.knr_rows(scans, "/rw", "abc1234", {"src/B/b.CPP"})
        got = sorted((r["class"], r["unit"]) for r in rows)
        self.assertEqual(got, [("gate-knr-args", "src/A/a.CPP"), ("gate-knr-args", "src/B/b.CPP"),
                               ("gate-knr-extra", "src/A/a.CPP")])
        self.assertTrue([r for r in rows if r["unit"] == "src/B/b.CPP"][0]["boot"])


DWARF = '''
 <1><b>: Abbrev Number: 2 (DW_TAG_base_type)
    <c>   DW_AT_byte_size   : 8
    <d>   DW_AT_name        : (indirect string, offset: 0x0): long long int
 <1><20>: Abbrev Number: 3 (DW_TAG_typedef)
    <21>   DW_AT_name        : (indirect string, offset: 0x5): u64
    <25>   DW_AT_type        : <0xb>
 <1><30>: Abbrev Number: 4 (DW_TAG_structure_type)
    <31>   DW_AT_name        : (indirect string, offset: 0x9): Msg
    <35>   DW_AT_byte_size   : 16
 <2><38>: Abbrev Number: 5 (DW_TAG_member)
    <39>   DW_AT_name        : tag
    <3d>   DW_AT_data_member_location: 0
 <2><40>: Abbrev Number: 5 (DW_TAG_member)
    <41>   DW_AT_name        : stamp
    <45>   DW_AT_type        : <0x20>
    <49>   DW_AT_data_member_location: 4
 <1><50>: Abbrev Number: 4 (DW_TAG_structure_type)
    <51>   DW_AT_byte_size   : 4
 <2><52>: Abbrev Number: 5 (DW_TAG_member)
    <53>   DW_AT_name        : flag
    <57>   DW_AT_bit_size    : 3
 <1><60>: Abbrev Number: 3 (DW_TAG_typedef)
    <61>   DW_AT_name        : Bits
    <65>   DW_AT_type        : <0x50>
 <1><70>: Abbrev Number: 4 (DW_TAG_structure_type)
    <71>   DW_AT_name        : Fwd
    <75>   DW_AT_declaration : 1
'''


class LayoutTests(unittest.TestCase):
    def test_dwarf_reader_names_typedef_structs_and_flags_wide_and_bit_members(self):
        got = {s["expr"]: s for s in gates.dwarf_structs(DWARF)}
        self.assertEqual(sorted(got), ["Bits", "struct Msg"])
        self.assertEqual(got["struct Msg"]["wide"], 1)
        self.assertEqual([m["name"] for m in got["struct Msg"]["members"]], ["tag", "stamp"])
        self.assertEqual(got["Bits"]["bitfields"], 1)

    def test_bit_fields_in_a_nested_anonymous_struct_count(self):
        dump = DWARF + """ <1><80>: Abbrev Number: 4 (DW_TAG_union_type)
    <81>   DW_AT_name        : Outer
    <85>   DW_AT_byte_size   : 4
 <2><a0>: Abbrev Number: 5 (DW_TAG_member)
    <a1>   DW_AT_name        : inner
    <a5>   DW_AT_type        : <0x88>
 <1><88>: Abbrev Number: 4 (DW_TAG_structure_type)
    <89>   DW_AT_byte_size   : 4
 <2><8c>: Abbrev Number: 5 (DW_TAG_member)
    <8d>   DW_AT_name        : kind
    <91>   DW_AT_bit_size    : 2
"""
        got = {s["expr"]: s for s in gates.dwarf_structs(dump)}
        self.assertEqual(got["union Outer"]["bitfields"], 1)
    def test_layout_keys_only_boot_units_so_a_no_layout_seed_warms_the_rest(self):
        def fp(unit, layout):
            ctx = {"source": "/src", "adapter": gates.HERE, "layout": layout, "boot": {"src/b.c"}, "sdk": set()}
            return gates.unit_fingerprint(ctx, unit)
        self.assertEqual(fp("src/a.c", True), fp("src/a.c", False))
        self.assertNotEqual(fp("src/b.c", True), fp("src/b.c", False))
    def test_shape_follows_the_type_closure_not_the_rest_of_the_unit(self):
        base = {s["expr"]: s["shape"] for s in gates.dwarf_structs(DWARF)}
        unrelated = {s["expr"]: s["shape"] for s in gates.dwarf_structs(DWARF + """ <1><90>: Abbrev Number: 4 (DW_TAG_structure_type)
    <91>   DW_AT_name        : Other
    <95>   DW_AT_byte_size   : 8
""")}
        self.assertEqual(base["struct Msg"], unrelated["struct Msg"])
        wider = {s["expr"]: s["shape"] for s in gates.dwarf_structs(DWARF.replace(
            "<c>   DW_AT_byte_size   : 8", "<c>   DW_AT_byte_size   : 4"))}
        self.assertNotEqual(base["struct Msg"], wider["struct Msg"])
        self.assertEqual(base["Bits"], wider["Bits"])

    def test_probe_round_trip_and_difference(self):
        s = {"expr": "struct Msg", "members": [{"name": "tag", "bit": False}, {"name": "stamp", "bit": False},
                                               {"name": "f", "bit": True}]}
        text = gates.probe_source([s])
        self.assertIn("__builtin_offsetof(struct Msg, stamp)", text)
        self.assertNotIn("offsetof(struct Msg, f)", text)
        asm = "__gp0_0:\n\t.long\t13\n__gp0_1:\n\t.long\t5\n__gp0_2:\n\t.long\t1\n__gp0_3:\n\t.long\t5\n"
        host = gates.probe_values(asm)
        self.assertEqual(host, {(0, 0): 12, (0, 1): 4, (0, 2): 0, (0, 3): 4})
        mips = {0: 16, 1: 8, 2: 0, 3: 8}
        flat = {j: v for (i, j), v in host.items()}
        self.assertEqual(gates.differences(s, flat, mips),
                         [("sizeof", 12, 16), ("alignof", 4, 8), ("stamp", 4, 8)])

    def test_real_compilers_agree_on_a_double_after_an_int(self):
        mips = shutil.which("mips-linux-gnu-gcc")
        if not (mips and shutil.which("gcc")):
            self.skipTest("needs gcc and mips-linux-gnu-gcc")
        with tempfile.TemporaryDirectory() as tmp:
            unit = pathlib.Path(tmp) / "u.i"
            unit.write_text("struct Msg { int tag; double stamp; };\nstruct Ok { int a; int b; };\n")
            structs = [{"expr": "struct Msg", "members": [{"name": "tag", "bit": False},
                                                          {"name": "stamp", "bit": False}]},
                       {"expr": "struct Ok", "members": [{"name": "a", "bit": False}, {"name": "b", "bit": False}]}]
            comps = {"host": ["gcc"] + gates.HOST_FLAGS, "aligned": ["gcc"] + gates.HOST_FLAGS + ["-malign-double"],
                     "mips": [mips] + gates.MIPS_FLAGS}
            got = gates.probe_unit(unit, structs, comps, tmp)
            msg = gates.differences(structs[0], got["host"][0], got["mips"][0])
            self.assertEqual([d[0] for d in msg], ["sizeof", "alignof", "stamp"])
            self.assertEqual(gates.differences(structs[0], got["aligned"][0], got["mips"][0]), [])
            self.assertEqual(gates.differences(structs[1], got["host"][1], got["mips"][1]), [])

    def test_decision_recommends_the_flag_only_when_it_closes_every_diff(self):
        rec = {"expr": "struct A", "bitfields": 0, "host_vs_mips": [("sizeof", 12, 16)], "aligned_vs_mips": []}
        self.assertEqual(gates.decision([rec], "", 0)["recommend"], "flag")
        self.assertEqual(gates.decision([rec], "struct A shim", 0)["recommend"], "per-struct")
        rec2 = dict(rec, aligned_vs_mips=[("x", 1, 2)])
        self.assertEqual(gates.decision([rec2], "", 0)["recommend"], "per-struct")


class TypesOnlyTests(unittest.TestCase):
    def test_bodies_become_declarations_and_types_stay(self):
        text = "struct S { int a; };\nstatic inline int f(int x) { return __builtin_ia32_x(x); }\nint g;\n"
        out = gates.types_only(text)
        self.assertIn("struct S", out)
        self.assertIn("static inline int f(int x);", out)
        self.assertNotIn("ia32", out)


class SubwordTests(unittest.TestCase):
    def test_view_at_plus_two_of_a_u32(self):
        with tempfile.TemporaryDirectory() as tmp:
            rw = pathlib.Path(tmp)
            (rw / "versions" / "v").mkdir(parents=True)
            (rw / "src").mkdir()
            (rw / "include").mkdir()
            (rw / "versions" / "v" / "symbol_addrs.txt").write_text(
                "D_80107E98 = 0x80107E98; // size:4\nD_80107E9A = 0x80107E9A; // size:2\nD_80107EA0 = 0x80107EA0;\n")
            (rw / "src" / "a.CPP").write_text("extern u32 D_80107E98;\nextern u16 D_80107E9A;\nextern u16 D_80107EA0;\n")
            rows = gates.subword_rows(rw, "v", "abc1234")
            self.assertEqual([(r["class"], r["symbol"]) for r in rows], [("gate-subword", "D_80107E9A")])


class BaselineTests(unittest.TestCase):
    def test_only_new_keys_fail_and_knr_extra_is_informational(self):
        old = gates.row("gate-return-type", "f", "src/A/a.CPP", "src/A/a.CPP:9", "p", True, "m")
        moved = dict(old, file_line="src/A/a.CPP:40")
        new = gates.row("gate-return-type", "g", "src/A/a.CPP", "src/A/a.CPP:20", "p", True, "m")
        extra = gates.row("gate-knr-extra", "h", "src/B/b.CPP", "src/B/b.CPP:1", "p", False, "m")
        base = {gates.key(old)}
        self.assertEqual(gates.new_sites([moved, extra], base), [])
        self.assertEqual(gates.new_sites([moved, new, extra], base), [gates.key(new)])
        renamed = gates.row("gate-return-type", "f2", "src/A/b.CPP", "", "p", True, "m")
        self.assertEqual(gates.new_sites([renamed], base), [])  # a rename keeps the count: not a new site

    def test_classes_table_counts_boot_units_and_folders(self):
        rows = [gates.row("gate-return-type", "f", "src/A/a.CPP", "", "p", True, ""),
                gates.row("gate-return-type", "g", "src/A/a.CPP", "", "p", True, ""),
                gates.row("gate-return-type", "h", "src/B/b.CPP", "", "p", False, "")]
        text = gates.render_classes(rows, {"units": 3, "boot_units": 2})
        self.assertIn("| gate-return-type | 3 | 2 | 1 | 2 | A 2, B 1 |", text)


class CacheTests(unittest.TestCase):
    def test_depfile_and_dependency_state(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / "a.h").write_text("one")
            (d / "x.d").write_text("x.i: x.c \\\n  %s/a.h\n" % d)
            self.assertEqual(gates.read_depfile(d / "x.d"), ["%s/a.h" % d, "x.c"])
            state = {str(d / "a.h"): gates.file_state(str(d / "a.h"))}
            self.assertTrue(gates.deps_unchanged(state))
            (d / "a.h").write_text("two")
            gates._STATE.clear()
            self.assertFalse(gates.deps_unchanged(state))
            (d / "a.h").write_text("one")  # same bytes, new mtime: still valid
            gates._STATE.clear()
            self.assertTrue(gates.deps_unchanged(state))
class PrepareTests(unittest.TestCase):
    def test_cheap_preprocess_is_the_same_unit_as_prepare_py_and_records_its_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            (tmp / "include").mkdir()
            (tmp / "src" / "sdk" / "libgcc").mkdir(parents=True)
            (tmp / "include" / "h.h").write_text("#define K 3\n")
            (tmp / "src" / "a.c").write_text('#include "h.h"\nint f(void) { return K; }\n')
            deps = []
            fast, _ = gates.prepare_unit("src/a.c", tmp, tmp / "w1", exact=False, deps=deps)
            exact, _ = gates.prepare_unit("src/a.c", tmp, tmp / "w2")
            self.assertEqual(fast.read_text(), exact.read_text())
            self.assertTrue(any(d.endswith("h.h") for d in deps))



if __name__ == "__main__":
    unittest.main()
