# SPDX-License-Identifier: GPL-3.0-or-later
"""Route undefined boot symbols: libultra to the shim or a stop, D_auto data by its name, data never a stop."""
import argparse
import json
import importlib.util
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

parser = argparse.ArgumentParser()
parser.add_argument("--work", type=pathlib.Path, required=True)
args = parser.parse_args()
args.work.mkdir(parents=True, exist_ok=True)
ROOT = pathlib.Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("boot_needs", ROOT / "needs.py")
needsfeed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(needsfeed)
spec = importlib.util.spec_from_file_location("boot_repin", ROOT.parents[2] / "tools" / "repin.py")
repin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repin)
spec = importlib.util.spec_from_file_location("boot_closure", ROOT / "closure.py")
closure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(closure)
spec = importlib.util.spec_from_file_location("boot_advance", ROOT / "advance.py")
advance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(advance)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=args.work)
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        source, shim = root / "source", root / "shim"
        (source / "src/GAME").mkdir(parents=True)
        (source / "src/AUDIO").mkdir(parents=True)
        (source / "versions/us-rev1").mkdir(parents=True)
        shim.mkdir()
        (source / "src/GAME/80200000.CPP").write_text(
            "void func_80200000_de(void) {\n}\nINCLUDE_ASM(\"span\", func_80200100_de);\n")
        # A libultra function with a C body in the tree: the closure must still not link it.
        (source / "src/AUDIO/802BFEB0.CPP").write_text("u64 func_802BADC0_de(void) {\n    return 0;\n}\n"
                                                      "u32 func_802BD170_de(u32 mask) {\n    return mask;\n}\n")
        (source / "versions/us-rev1/symbol_addrs.txt").write_text(
            "func_80200000_de = 0x80200000; // type:func\n"
            "func_80200100_de = 0x80200100; // type:func\n"
            "func_802BADC0_de = 0x802BFEB0; // type:func\n"
            "func_802BD170_de = 0x802C2260; // type:func\n"
            "func_802BAD70_de = 0x802BFE60; // type:func\n"
            "D_80100000 = 0x80104000; //\n")
        (shim / "n64c_os.h").write_text("OSTime osGetTime(void);\n")
        bindings = root / "shim-bindings.txt"
        bindings.write_text("# original libultra name evidence\n"
                            "func_802BADC0_de osGetTime fixture\nfunc_802BD170_de osSetIntMask fixture mask\n")
        self.facts = closure.Facts(source, shim, bindings)
        self.d = {"sources": [], "data": {}, "bindings": {}, "stops": {}}

    def route(self, sym, frontier=False):
        return closure.route(self.d, sym, self.facts, {}, "abcdef0", frontier, set())

    def test_libultra_binds_to_the_shim_or_stops(self):
        self.assertEqual(self.route("func_802BADC0_de")[0], "routed")
        self.assertEqual(self.d["bindings"]["func_802BADC0_de"], "osGetTime")
        self.route("func_802BD170_de")
        self.assertIn("shim lacks osSetIntMask", self.d["stops"]["func_802BD170_de"])
        self.route("func_802BAD70_de")
        self.assertIn("not identified for the shim", self.d["stops"]["func_802BAD70_de"])
        self.assertEqual(self.d["sources"], [], "no original libultra unit is linked")

    def test_data_alias_binds_to_its_pinned_address(self):
        # aliases.txt pins a second name for pinned storage; function aliases never move the libultra range.
        source = self.facts.source
        (source / "versions/us-rev1/aliases.txt").write_text(
            "D_801470C0_de = 0x8014D340;\n__udivdi3 = 0x802C5E90;\nD_80100000 = 0x80104000;\n")
        facts = closure.Facts(source, pathlib.Path(self.tmp.name) / "shim", pathlib.Path(self.tmp.name) / "shim-bindings.txt")
        self.assertEqual(closure.route(self.d, "D_801470C0_de", facts, {}, "abcdef0", False, set()),
                         ("routed", "data 0x8014D340"))
        self.assertEqual(self.d["data"]["D_801470C0_de"], "0x8014D340")
        self.assertIsNone(facts.sdk("__udivdi3"))
        (source / "versions/us-rev1/aliases.txt").write_text("D_80100000 = 0x80100000;\n")
        with self.assertRaises(ValueError):
            closure.policy.pinned(source, "us-rev1")

    def test_declaration_stop_from_an_older_pin_is_tried_again(self):
        why = "src/GAME/80200000.CPP:116 conflicting types for f at abcdef0; RW NEEDS one declaration"
        self.d["stops"]["func_80200000_de"] = why
        closure.tidy(self.d, self.facts, {"src/GAME/80200000.CPP": "src/GAME/80200000.CPP:116 conflicting types"})
        self.assertEqual(self.d["stops"], {"func_80200000_de": why}, "still refused: the stop stays")
        closure.tidy(self.d, self.facts, {})
        self.assertEqual(self.d["stops"], {})
        self.assertEqual(self.route("func_80200000_de"), ("added", "src/GAME/80200000.CPP"))
        # Still failing at this pin: refuse() denies the unit again and the route stops it again.
        deny = {}
        closure.refuse(self.d, deny, "FAILED: decomp/src/GAME/80200000.CPP.i \n"
                       "src/GAME/80200000.CPP:116:5: error: conflicting types for f\n")
        self.assertEqual(self.d["sources"], [])
        kind, _ = closure.route(self.d, "func_80200000_de", self.facts, deny, "abcdef0", False, set())
        self.assertEqual(kind, "routed")
        self.assertTrue(self.d["stops"]["func_80200000_de"].endswith("RW NEEDS one declaration"))
        closure.tidy(self.d, self.facts, deny)
        self.assertIn("func_80200000_de", self.d["stops"])

    def test_game_code_in_the_libultra_range_links_as_game_code(self):
        source = self.facts.source
        (source / "src/sdk").mkdir(parents=True)
        (source / "src/sdk/802B2378.c").write_text("s32 func_802B2378(s32 a) {\n    return a;\n}\n")
        (source / "src/GAME/802B2350.CPP").write_text("float func_802B2350(int a) {\n    return a;\n}\n")
        root = pathlib.Path(self.tmp.name)
        game = closure.policy.game_code({"game_in_sdk_range": {"func_802B2378": "e", "func_802B2350": "e"}})
        facts = closure.Facts(source, root / "shim", root / "shim-bindings.txt", game)
        self.d["stops"]["func_802B2350"] = "libultra 0x802B2350; not identified for the shim at x (SDK: shim binding decision)"
        closure.tidy(self.d, facts)
        self.assertEqual(self.d["stops"], {})
        for sym, unit in (("func_802B2378", "src/sdk/802B2378.c"), ("func_802B2350", "src/GAME/802B2350.CPP")):
            self.assertIsNone(facts.sdk(sym))
            self.assertEqual(closure.route(self.d, sym, facts, {}, "abcdef0", False, set()), ("added", unit))
        self.assertIsNotNone(self.facts.sdk("func_802B2350"), "without the project entry it stays libultra")
        project = {"sources": ["src/sdk/802B2378.c"], "stops": {}, "bindings": {},
                   "game_in_sdk_range": {"func_802B2378": "e"}}
        closure.policy.sanitize(project, source, {}, {}, "")
        self.assertEqual(project["sources"], ["src/sdk/802B2378.c"])

    def test_corrected_shim_row_rebinds(self):
        self.d["bindings"]["func_802BADC0_de"] = "n64c_old_guess"
        closure.tidy(self.d, self.facts)
        self.assertEqual(self.d["bindings"]["func_802BADC0_de"], "osGetTime")

    def test_libultra_stop_becomes_binding_when_shim_provides_it(self):
        self.d["stops"]["func_802BADC0_de"] = "no C definition"
        closure.tidy(self.d, self.facts)
        self.assertEqual(self.d["bindings"], {"func_802BADC0_de": "osGetTime"})
        self.assertEqual(self.d["stops"], {})

    def test_inherited_sdk_units_are_removed_before_first_build(self):
        root = pathlib.Path(self.tmp.name)
        manifest = root / "project.json"
        self.d["source_pin"] = "a" * 40
        self.d["sources"] = ["src/GAME/80200000.CPP", "src/AUDIO/802BFEB0.CPP"]
        self.d["stops"]["func_802BADC0_de"] = "old shim stop"
        manifest.write_text(json.dumps(self.d))
        built = []
        def build(*args, **kwargs):
            built.append(json.loads(manifest.read_text()))
            return mock.Mock(returncode=0)
        argv = ["closure", "--build", str(root), "--source", str(root / "source"),
                "--work", str(root), "--manifest", str(manifest)]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(closure, "Facts", return_value=self.facts), \
             mock.patch.object(closure.subprocess, "run", side_effect=build):
            self.assertEqual(closure.main(), 0)
        self.assertEqual(len(built), 1)
        self.assertEqual(built[0]["sources"], ["src/GAME/80200000.CPP"])
        self.assertEqual(built[0]["bindings"]["func_802BADC0_de"], "osGetTime")
        self.assertNotIn("func_802BADC0_de", built[0]["stops"])
        self.assertIn("SDK:", built[0]["stops"]["func_802BD170_de"])

    def test_sdk_directory_blocks_unpinned_symbols(self):
        unit = "src/SDK/odd.CPP"
        path = self.facts.source / unit
        path.parent.mkdir()
        text = 'void original_helper(void) {\n}\nINCLUDE_ASM("sdk", original_asm);\n'
        path.write_text(text)
        self.facts.text[unit] = text
        self.assertEqual(self.route("original_helper")[0], "routed")
        self.assertIn("SDK:", self.d["stops"]["original_helper"])
        self.d["sources"] = [unit]
        closure.tidy(self.d, self.facts)
        self.assertEqual(self.d["sources"], [])
        self.assertIn("original_asm", self.d["stops"])
        saved = json.dumps(self.d, sort_keys=True)
        closure.tidy(self.d, self.facts)
        self.assertEqual(saved, json.dumps(self.d, sort_keys=True))

    def test_sdk_data_cannot_reintroduce_an_excluded_code_unit(self):
        unit = "src/AUDIO/802BFEB0.CPP"
        text = self.facts.text[unit] + "int D_sdk_state;\n"
        (self.facts.source / unit).write_text(text)
        self.facts.text[unit] = text
        kind, reason = self.route("D_sdk_state")
        self.assertEqual(kind, "need")
        self.assertIn("clean provider", reason)
        self.assertEqual(self.d["sources"], [])
        self.assertNotIn("D_sdk_state", self.d["stops"])

    def test_game_function_links_its_unit(self):
        self.assertEqual(self.route("func_80200000_de"), ("added", "src/GAME/80200000.CPP"))
        self.d["sources"].clear()
        self.route("func_80200000_de", frontier=True)
        self.assertIn("(boot frontier)", self.d["stops"]["func_80200000_de"])

    def test_address_named_data(self):
        self.route("D_auto_80104444")
        self.assertEqual(self.d["data"]["D_auto_80104444"], "0x80104444")
        self.route("D_auto_80200000")  # a defined function sits at the named address
        self.assertEqual(self.d["bindings"]["D_auto_80200000"], "func_80200000_de")
        self.route("D_auto_00200100")  # pre-KSEG0 alias of a function
        self.assertEqual(self.d["bindings"]["D_auto_00200100"], "func_80200100_de")
        self.route("D_80100000")  # pinned data binds at its us-rev1 address, not its name
        self.assertEqual(self.d["data"]["D_80100000"], "0x80104000")

    def test_alias_target_links_its_unit(self):
        # D_00XXXXXX takes a function's address; the function's unit is linked like a direct call would link it.
        kind, detail = self.route("D_00200000")
        self.assertEqual((kind, detail), ("added", "src/GAME/80200000.CPP"))
        self.assertEqual(self.d["bindings"]["D_00200000"], "func_80200000_de")
        self.assertEqual(self.d["sources"], ["src/GAME/80200000.CPP"])
        self.assertEqual(self.d["stops"], {})
        # An older closure's dead-end stop for such a target is dropped so this round routes it.
        self.d["sources"].clear()
        self.d["stops"]["func_80200000_de"] = "no linked definition at 0000000"
        closure.tidy(self.d, self.facts)
        self.assertEqual(self.d["stops"], {})
        # At the frontier the target waits as a frontier stop; a libultra target goes to the shim, never linked.
        self.d["bindings"].clear()
        self.route("D_00200000", frontier=True)
        self.assertIn("(boot frontier)", self.d["stops"]["func_80200000_de"])
        self.route("D_002BFEB0")
        self.assertEqual(self.d["bindings"]["func_802BADC0_de"], "osGetTime")
        self.assertEqual(self.d["sources"], [])

    def test_data_defined_only_for_another_version_binds_its_pin(self):
        unit = "src/GAME/table.CPP"
        self.facts.text[unit] = "#if defined(VERSION_DE)\nunsigned int D_80100000[] = {\n    1,\n};\n#endif\n"
        self.assertEqual(self.route("D_80100000"), ("added", unit))
        # Linked and still undefined: the definition is for another version; the pinned address binds it.
        self.assertEqual(self.route("D_80100000"), ("routed", "data 0x80104000"))
        self.assertEqual(self.d["data"]["D_80100000"], "0x80104000")

    def test_linker_script_binding_target_counts_as_undefined(self):
        log = ("bindings.ld:321: undefined symbol `func_802953FC_us_rev1' referenced in expression\n"
               "a.o: in function `f': undefined reference to `func_80200000_de'\n")
        self.assertEqual(closure.undefined_symbols(log), ["func_80200000_de", "func_802953FC_us_rev1"])

    def test_data_is_never_a_stop(self):
        kind, detail = self.route("D_80999999")
        self.assertEqual(kind, "need")
        self.assertIn("no C definition", detail)
        self.d["stops"]["D_80104444"] = "old function stop"
        closure.tidy(self.d, self.facts)
        self.assertNotIn("D_80104444", self.d["stops"])

    def test_frontier_links_data_storage_and_defers_functions(self):
        unit = "src/GAME/storage.CPP"
        self.facts.text[unit] = "int D_storage[4];\nvoid func_80200200_de(void) {\n}\n"
        fresh = set()
        self.assertEqual(closure.route(self.d, "D_storage", self.facts, {}, "abcdef0", True, fresh),
                         ("added", unit))
        self.assertEqual(self.d["sources"], [unit])
        self.assertNotIn("D_storage", self.d["stops"])
        self.assertIn(unit, fresh)
        self.assertEqual(closure.route(self.d, "func_80200200_de", self.facts, {}, "abcdef0", True, fresh),
                         ("skip", ""))
        self.route("func_80200000_de", frontier=True)
        self.assertIn("(boot frontier)", self.d["stops"]["func_80200000_de"])

    def test_frontier_unavailable_data_is_a_need_not_a_stop(self):
        unit = "src/GAME/storage.CPP"
        self.facts.text[unit] = "int D_storage[4];\n"
        for selected, deny in [([], {unit: "declaration conflict"}), ([unit], {})]:
            with self.subTest(selected=selected, deny=deny):
                self.d["sources"] = selected
                kind, reason = closure.route(self.d, "D_storage", self.facts, deny, "abcdef0", True, set())
                self.assertEqual(kind, "need")
                self.assertIn("D_storage", reason)
                self.assertNotIn("D_storage", self.d["stops"])

    def test_advance_routes_old_sdk_stops_to_shim_owner(self):
        for reason in ["no C definition", "conflicting types; RW NEEDS one declaration", ""]:
            self.d["stops"]["func_802BD170_de"] = reason
            unit, why = advance.next_unit(self.d, "func_802BD170_de", self.facts)
            self.assertIsNone(unit)
            self.assertIn("shim binding decision", why)
            self.assertIn("802C2260", why)

    def test_advance_halts_at_libultra_frontier(self):
        self.d["stops"]["func_80200000_de"] = "src/GAME/80200000.CPP defines it; not linked yet at x (boot frontier)"
        self.assertEqual(advance.next_unit(self.d, "func_80200000_de", self.facts)[0], "src/GAME/80200000.CPP")
        self.d["stops"]["func_802BADC0_de"] = "src/AUDIO/802BFEB0.CPP defines it; not linked yet at x (boot frontier)"
        unit, why = advance.next_unit(self.d, "func_802BADC0_de", self.facts)
        self.assertIsNone(unit)
        self.assertIn("shim binding decision", why)
        self.assertIsNone(advance.next_unit(self.d, "func_80200100_de", self.facts)[0])
    def test_advance_names_game_functions_filed_under_sdk(self):
        unit = "src/SDK/SCHED/game_sched.CPP"
        self.facts.text[unit] = "void func_80200400_de(void) {\n}\n"
        self.d["stops"]["func_80200400_de"] = unit + " defines it; not linked yet at x (boot frontier)"
        got, why = advance.next_unit(self.d, "func_80200400_de", self.facts)
        self.assertIsNone(got)
        self.assertIn("no libultra-range function", why)
        self.assertIn("decomp must move", why)

    def run_advance(self, result, closure_result=None, steps=10):
        root = pathlib.Path(self.tmp.name)
        manifest = root / "advance.json"
        before = json.dumps(self.d)
        manifest.write_text(before)
        argv = ["advance", "--build", str(root), "--source", str(root / "source"),
                "--work", str(root), "--rom", str(root / "rom"), "--manifest", str(manifest), "--steps", str(steps)]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(advance.closure, "Facts", return_value=self.facts), \
             mock.patch.object(advance, "boot", return_value=result) as boot, \
             mock.patch.object(advance.subprocess, "run", return_value=closure_result) as close:
            rc = advance.main()
        return rc, before == manifest.read_text(), boot.call_count, close.call_count

    def test_advance_rejects_abnormal_boot_without_mutating_manifest(self):
        symbol = "func_80200000_de"
        self.route(symbol, frontier=True)
        for result in [(124, ""), (-11, "boot-entry: original C"),
                       (1, "boot-input: cannot read ROM"), (78, ""),
                       (78, "boot-missing: malformed extra"),
                       (0, "boot-missing: " + symbol), (139, "boot-missing: " + symbol)]:
            with self.subTest(result=result):
                self.assertEqual(self.run_advance(result), (1, True, 1, 0))

    def test_advance_accepts_normal_exit_and_expected_owned_stop(self):
        for result in [(0, ""), (78, "boot-missing: func_80200100_de")]:
            with self.subTest(result=result):
                self.assertEqual(self.run_advance(result), (0, True, 1, 0))

    def test_advance_requires_both_closure_exit_and_completion_marker(self):
        self.route("func_80200000_de", frontier=True)
        for code, output in [(1, "closure: links\n"), (0, "closure: stuck\n"),
                             (0, "old log contained closure: links\n")]:
            with self.subTest(code=code, output=output):
                result = mock.Mock(returncode=code, stdout=output, stderr="")
                self.assertEqual(self.run_advance((78, "boot-missing: func_80200000_de"), result),
                                 (1, False, 1, 1))

    def test_boot_keeps_last_missing_marker_and_full_diagnostics(self):
        root = pathlib.Path(self.tmp.name)
        stderr = ("boot-entry: original C\nboot-missing: first\n"
                  "boot-missing: second\nboot-detail: shutdown\n")
        (root / "boot.ppm").write_text("stale image")
        with mock.patch.object(advance.subprocess, "run",
                               return_value=mock.Mock(returncode=78, stderr=stderr)):
            self.assertEqual(advance.boot(root / "binary", root / "rom", root),
                             (78, "boot-missing: second"))
        self.assertEqual((root / "boot.log").read_text(), stderr)
        self.assertFalse((root / "boot.ppm").exists())

    def test_tidy_drops_stops_naming_a_unit_the_pin_no_longer_has(self):
        self.d["stops"]["func_80200000_de"] = "src/SDK/PFS/gone.CPP defines it; not linked yet at x (boot frontier)"
        self.d["stops"]["func_80200100_de"] = "src/GAME/80200000.CPP:7 conflicting types for 'x'; RW NEEDS one declaration"
        self.d["stops"]["func_80200200_de"] = "src/GAME/moved.CPP:9 conflicting types for 'y'"
        self.d["stops"]["func_80200300_de"] = "no C definition at abcdef0"
        closure.tidy(self.d, self.facts)
        self.assertEqual(sorted(self.d["stops"]), ["func_80200100_de", "func_80200300_de"])

    def test_advance_reroutes_a_stale_stop_instead_of_halting(self):
        symbol = "func_80200000_de"
        self.d["stops"][symbol] = "src/SDK/PFS/gone.CPP defines it; not linked yet at x (boot frontier)"
        ok = mock.Mock(returncode=0, stdout="closure: links\n", stderr="")
        rc, same, boots, closes = self.run_advance((78, "boot-missing: " + symbol), ok, steps=1)
        self.assertEqual((rc, same, boots, closes), (0, False, 1, 1))

    def test_needs_feed_classifies_stops_and_data(self):
        root = pathlib.Path(self.tmp.name)
        manifest, work = root / "needs.json", root / "needs-work"
        work.mkdir()
        (root / "worklist.tsv").write_text("order\tsymbol\n66\tfunc_802B72B0_de\n7\tfunc_80200100_de\n")
        (work / "data-needs.txt").write_text("D_80999999 | data with no pinned address at abc; no C definition\n")
        manifest.write_text(json.dumps({"source_pin": "a" * 40, "stops": {
            "func_80200100_de": "src/GAME/80200000.CPP:7 conflicting types for 'x'; have 'int'; RW NEEDS one declaration",
            "func_802B72B0_de": "libultra 0x802BC380; not identified for the shim at abcdef0 (SDK: shim binding decision)",
            "func_80200200_de": "src/GAME/80200000.CPP:9 expected ';' before '}' token",
            "func_80200300_de": "no C definition at abcdef0",
            "func_80200400_de": "SDK-directory unit holds no libultra-range function: src/SDK/a.CPP (f); decomp must move",
            "func_80200500_de": "src/GAME/80200000.CPP defines it; not linked yet at abcdef0 (boot frontier)"}}))
        self.assertEqual(needsfeed.write(work, manifest, root / "worklist.tsv", "func_802B72B0_de"), 6)
        got = {r["symbol"]: r for r in map(json.loads, (work / "needs.jsonl").read_text().splitlines())}
        self.assertEqual({k: v["class"] for k, v in got.items()},
                         {"func_80200100_de": "decl", "func_802B72B0_de": "library-name",
                          "func_80200200_de": "nm-compile", "func_80200300_de": "body",
                          "func_80200400_de": "placement", "D_80999999": "data-type"})
        self.assertEqual(got["func_80200100_de"]["file_line"], "src/GAME/80200000.CPP:7")
        self.assertEqual((got["func_802B72B0_de"]["trace_order"], got["func_802B72B0_de"]["halt"]), (66, True))
        self.assertEqual(got["func_80200100_de"]["pin"], "aaaaaaa")

    def test_repin_moves_the_three_pin_files_and_reads_trace_order(self):
        root = pathlib.Path(self.tmp.name) / "ports"
        old, new = "a" * 40, "b" * 40
        for name, text in [(repin.PIN_FILES[0], json.dumps({"source_pin": old})), ("cmake/decomp.cmake", f"{old} x {old} pinned {old[:7]}"),
                           (repin.PIN_FILES[2], f"pin {old}")]:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        self.assertEqual(repin.repin_files(root, new), old)
        self.assertEqual(json.loads((root / repin.PIN_FILES[0]).read_text())["source_pin"], new)
        self.assertEqual((root / "cmake/decomp.cmake").read_text(), f"{new} x {new} pinned {new[:7]}")
        self.assertEqual((root / repin.PIN_FILES[2]).read_text(), f"pin {new}")
        self.assertIsNone(repin.repin_files(root, new))
        wl = root / "wl.tsv"
        wl.write_text("order\tsymbol\n66\tfunc_802B72B0_de\n")
        self.assertEqual(repin.trace_order(wl, "func_802B72B0_de"), 66)
        self.assertIsNone(repin.trace_order(wl, "nope"))


unittest.main(argv=[sys.argv[0]])
