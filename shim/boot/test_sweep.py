# SPDX-License-Identifier: GPL-3.0-or-later
import pathlib
import sys
import tempfile
import threading
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import sweep

RELOCS = """
Relocation section '.rel.text.f' at offset 0x10 contains 3 entries:
 Offset     Info    Type                Sym. Value  Symbol's Name
00000004  00001a02 R_386_PC32             00000000   __x86.get_pc_thunk.ax
0000000f  00001c2b R_386_GOT32X           00000000   D_800CDD10
00000020  00001c04 R_386_PLT32            00000000   g
Relocation section '.rel.data.tbl' at offset 0x20 contains 1 entries:
 Offset     Info    Type                Sym. Value  Symbol's Name
00000000  00001d01 R_386_32               00000000   f
Relocation section '.rel.eh_frame' at offset 0x30 contains 1 entries:
 Offset     Info    Type                Sym. Value  Symbol's Name
00000020  00000202 R_386_PC32             00000000   .text.f
"""


class Router:
    def __init__(self, table, sdk=()):
        self.table, self.sdkset = table, set(sdk)

    def external(self, sym):
        return self.table.get(sym, ("ok", "host", ""))

    def sdk(self, sym, unit=None):
        return sym in self.sdkset or unit in self.sdkset

    def internal(self, sym):
        return sym in self.sdkset


def rec(unit, defs, edges, missing=(), nm=()):
    return {"unit": unit, "ok": True, "defs": defs, "edges": edges, "missing": list(missing), "nm_errors": list(nm)}


class Parse(unittest.TestCase):
    def test_relocs(self):
        e = sweep.parse_relocs(RELOCS)
        self.assertEqual(e, {"f": ["D_800CDD10", "g"], "tbl": ["f"]})

    def test_nm(self):
        d, u = sweep.parse_nm("00000000 t loc\n00000010 T f\n         U g\n00000000 D x\n")
        self.assertEqual(d, {"loc": "t", "f": "T", "x": "D"})
        self.assertEqual(u, {"g"})

    def test_classify(self):
        self.assertEqual(sweep.classify_error("a.c:1: error: conflicting types for 'f'"), "decl-conflict")
        self.assertEqual(sweep.classify_error("error: 'x' undeclared"), "undeclared-ident")
        self.assertEqual(sweep.classify_error("weird"), "compile-other")
        self.assertEqual(sweep.first_error("note\na.c:3: error: bad\nmore"), "a.c:3: error: bad")


class Check(unittest.TestCase):
    def graph(self, table=None):
        a = rec("a.c", {"f": "T", "h": "T", "stubbed": "T"}, {"f": ["g", "D1"], "h": ["stubbed"]}, missing=["stubbed"])
        b = rec("b.c", {"g": "T", "k": "T"}, {"g": ["osX"], "k": []})
        return sweep.Graph(Router(table or {}), lambda unit: {"unit": unit, "ok": False}, {}, [a, b])

    def test_ok(self):
        self.assertEqual(sweep.check_function(self.graph(), {}, "k", "b.c")[:2], ("ok", "ok"))

    def test_closure_blocker_is_found_through_callee(self):
        g = self.graph({"osX": ("blocked", "shim-row-missing", "libultra osX")})
        check, verdict, why, n, _ = sweep.check_function(g, {}, "f", "a.c")
        self.assertEqual((check, verdict, n), ("closure", "shim-row-missing", 1))
        self.assertIn("osX", why)

    def test_direct_callee_asm_stop(self):
        check, verdict, *_ = sweep.check_function(self.graph(), {}, "h", "a.c")
        self.assertEqual((check, verdict), ("callee", "callee-asm-stop"))

    def test_own_stub_is_body(self):
        self.assertEqual(sweep.check_function(self.graph(), {}, "stubbed", "a.c")[:2], ("body", "asm-only"))
        g = self.graph()
        g.rec["a.c"]["nm_errors"] = ["a.c:9: error: conflicting types"]
        self.assertEqual(sweep.check_function(g, {}, "stubbed", "a.c")[:2], ("body", "asm-only-nm-rejected"))

    def test_denied_unit(self):
        deny = {"c.c": "c.c:2: error: conflicting types for 'q'"}
        self.assertEqual(sweep.check_function(self.graph(), deny, "q", "c.c")[:2], ("unit-compile", "decl-conflict"))
        self.assertEqual(sweep.check_function(self.graph(), {}, "q", "d.c")[:2], ("unit-compile", "unit-missing"))

    def test_missing_symbol_in_object(self):
        self.assertEqual(sweep.check_function(self.graph(), {}, "nope", "a.c")[:2], ("body", "not-in-object"))

    def test_units_build_on_demand_once(self):
        built = []

        def build(unit):
            built.append(unit)
            return rec("b.c", {"g": "T"}, {"g": []}) if unit == "b.c" else {"unit": unit, "ok": False}

        a = rec("a.c", {"f": "T"}, {"f": ["g"]})
        g = sweep.Graph(Router({}), build, {"g": ["b.c"]}, [a])
        self.assertEqual(sweep.check_function(g, {}, "f", "a.c")[:2], ("ok", "ok"))
        sweep.check_function(g, {}, "f", "a.c")
        self.assertEqual(built, ["b.c"])

    def test_index_finds_column0_definitions(self):
        idx = sweep.build_index({"u.c": "extern int x;\nint f(int a) {\n  return g(a);\n}\nINCLUDE_ASM(\"p\", h);\n"})
        self.assertEqual((idx.get("f"), idx.get("h"), idx.get("x"), idx.get("g")), (["u.c"], ["u.c"], None, None))

    def test_stale_list_unit_resolves_to_the_tree(self):
        idx = sweep.build_index({"src/particle/a.c": "int f(int a) {\n  return a;\n}\n", "src/b.c": "int g(void) {\n}\n"})
        self.assertEqual(sweep.owner(idx, "f", "src/FX_2/a.c"), "src/particle/a.c")  # moved since the list
        self.assertEqual(sweep.owner(idx, "g", "src/b.c"), "src/b.c")
        self.assertEqual(sweep.owner(idx, "nowhere", "src/c.c"), "src/c.c")  # unknown: the row reports it as listed
        idx = sweep.build_index({"src/x.c": "int f(int a);\n", "src/y.c": "int f(int a) {\n  return a;\n}\n"})
        self.assertEqual(sweep.owner(idx, "f", "src/old.c", lambda s: ["src/y.c"]), "src/y.c")  # not the prototype
    def test_rows_appear_before_the_list_finishes(self):
        gate, seen = threading.Event(), []
        a = rec("a.c", {"f": "T", "slow": "T"}, {"f": [], "slow": []})
        g = sweep.Graph(Router({}), None, {}, [a])
        orig = sweep.check_function

        def check(graph, deny, sym, unit):
            if sym == "slow":
                gate.wait(5)
            return orig(graph, deny, sym, unit)

        sweep.check_function = check
        try:
            rows = [{"order": 1, "symbol": "f", "unit": "a.c"}, {"order": 2, "symbol": "slow", "unit": "a.c"}]
            with tempfile.TemporaryDirectory() as d:
                out = pathlib.Path(d)

                def on_row(results):
                    if len(results) == 1:
                        seen.append((out / "sweep.tsv").read_text())
                        gate.set()

                done = sweep.run_sweep(rows, g, {}, {"a.c"}, 2, out, on_row)
                self.assertEqual([r["symbol"] for r in done], ["f", "slow"])
                self.assertEqual(len(seen[0].splitlines()), 2)  # header + row 1 while row 2 still waits
                self.assertTrue((out / "summary.tsv").exists())
        finally:
            sweep.check_function = orig

    def test_summary_orders_by_first_occurrence(self):
        rows = [{"check": "body", "verdict": "x", "order": 9, "symbol": "s9"},
                {"check": "body", "verdict": "x", "order": 3, "symbol": "s3"},
                {"check": "ok", "verdict": "ok", "order": 1, "symbol": "s1"}]
        self.assertEqual(sweep.summarize(rows), [(("ok", "ok"), 1, 1, "s1"), (("body", "x"), 2, 3, "s3")])

    def sdk_graph(self, table):
        a = rec("a.c", {"f": "T"}, {"f": ["g"]})
        b = rec("sdk/b.c", {"g": "T"}, {"g": ["osX"]})
        return sweep.Graph(Router(table, sdk={"sdk/b.c"}), lambda unit: {"unit": unit, "ok": False}, {}, [a, b])

    def test_walk_stops_at_a_shim_bound_libultra_callee(self):
        g = self.sdk_graph({"g": ("ok", "manifest", ""), "osX": ("blocked", "shim-unidentified", "never walked")})
        self.assertEqual(sweep.check_function(g, {}, "f", "a.c")[:2], ("ok", "ok"))

    def test_unbound_libultra_callee_is_the_blocker_not_its_body(self):
        g = self.sdk_graph({"g": ("blocked", "shim-unidentified", "libultra g; not identified for the shim"),
                            "osX": ("blocked", "shim-row-missing", "never walked")})
        check, verdict, why, n, _ = sweep.check_function(g, {}, "f", "a.c")
        self.assertEqual((check, verdict, n), ("callee", "shim-unidentified", 1))
        self.assertTrue(why.startswith("g: "))

    def test_libultra_row_is_decided_by_its_shim_binding(self):
        g = self.sdk_graph({"g": ("ok", "manifest", "")})
        self.assertEqual(sweep.check_function(g, {}, "g", "sdk/b.c")[:2], ("ok", "shim"))
        g = self.sdk_graph({"g": ("blocked", "shim-unidentified", "libultra g; not identified for the shim")})
        self.assertEqual(sweep.check_function(g, {}, "g", "sdk/b.c")[:2], ("sdk", "shim-unidentified"))
        self.assertEqual(sweep.check_function(g, {}, "g", "-")[:2], ("unit-compile", "no-owning-file"))
        g.router.sdkset.add("g")  # pinned libultra, and no game source names g
        self.assertEqual(sweep.check_function(g, {}, "g", "sdk/b.c")[:2], ("ok", "shim-internal"))

    def test_router_sdk_and_rebind(self):
        class Facts:
            def __init__(self):
                self.text = {"sdk/u.c": "void inner(void) { deep(); }", "g.c": "void f(void) { outer(); } /* inner() */"}

            def sdk(self, sym):
                return 0x802B0000 if sym == "osX" else None

            def sdk_unit(self, unit):
                return unit.startswith("sdk/")

        seen = []
        facts = Facts()
        r = sweep.Router(facts, {"stops": {}, "bindings": {}, "data": {}}, {}, set(), None,
                         lambda d, facts: seen.append(d))
        self.assertEqual(len(seen), 1)
        self.assertTrue(r.sdk("osX") and r.sdk("y", "sdk/u.c"))
        self.assertFalse(r.sdk("y", "g.c") or r.sdk("y", "missing.c") or r.sdk("y"))
        with tempfile.TemporaryDirectory() as d:
            facts.source = pathlib.Path(d)
            (facts.source / "include/sdk").mkdir(parents=True)
            (facts.source / "include/GAME.H").write_text("#define CALL hdr()\n")
            (facts.source / "include/sdk/os.h").write_text("void deep(void);\n")
            (facts.source / "include/TYPES.H").write_text("extern void declared(s32);\nvoid *proto(void *, u64 n);\n"
                                                          "extern u8 table[4];\n")
            self.assertEqual([r.internal(s) for s in ("outer", "hdr", "inner", "deep", "declared", "proto")],
                             [False, False, True, True, True, True])

    def test_declarations_are_not_uses(self):
        text = ("extern void a(s32);\nvoid b(void *, u64 size);\ns32 *c(int (*cb)(int), int);\nvoid g(void) {\n"
                "    d(1);\n    return e(2);\n    x = f(3);\n    s32 v = h();\n    else i(1);\n}\n#define M() j()\n")
        used = set(sweep.IDENT.findall(sweep.DECLARATION.sub(" ", text)))
        self.assertFalse({"a", "b", "c"} & used)
        self.assertLessEqual({"d", "e", "f", "h", "i", "j"}, used)

    def test_stop_class(self):
        self.assertEqual(sweep.stop_class("libultra 0x1 = osX (e); shim lacks osX at p (SDK: shim binding decision)")[0],
                         "shim-row-missing")
        self.assertEqual(sweep.stop_class("u.c:1 boom at p; RW NEEDS one declaration")[0], "callee-unit-uncompilable")


if __name__ == "__main__":
    unittest.main()
