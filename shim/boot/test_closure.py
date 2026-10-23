# SPDX-License-Identifier: GPL-3.0-or-later
"""Route undefined boot symbols: libultra to the shim or a stop, D_auto data by its name, data never a stop."""
import argparse
import json
import importlib.util
import pathlib
import subprocess
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

    def test_tidy_drops_frontier_stops_unless_walking_the_legacy_frontier(self):
        stop = {"f": "src/GAME/80200000.CPP defines it; not linked yet at abcdef0 (boot frontier)",
                "g": "no C definition at abcdef0"}
        self.d["stops"] = dict(stop)
        closure.tidy(self.d, self.facts, {}, "abcdef0")
        self.assertEqual(sorted(self.d["stops"]), ["g"], "the link-all archive pulls the unit instead")
        self.d["stops"] = dict(stop)
        closure.tidy(self.d, self.facts, {}, "abcdef0", frontier=True)
        self.assertEqual(sorted(self.d["stops"]), ["f", "g"])
    def test_one_line_body_has_an_owner_and_a_prototype_does_not(self):
        source = self.facts.source
        (source / "src/GAME/802A23A4.CPP").write_text(
            "void func_802A18FC_de(void) { func_80418F8C_de(); }\nextern void func_80418F8C_de(void);\n"
            "extern void func_80418F90_de(int);\n")
        self.facts.text = {str(p.relative_to(source)): p.read_text() for p in (source / "src").rglob("*.CPP")}
        self.assertEqual(self.facts.definers("func_802A18FC_de"), ["src/GAME/802A23A4.CPP"])
        self.assertEqual(self.facts.definers("func_80418F8C_de"), [])
        self.assertEqual(self.facts.definers("func_80418F90_de"), [])

    def test_stale_sdk_decision_stop_for_game_code_is_dropped(self):
        d = {"sources": [], "data": {}, "bindings": {},
             "stops": {"func_80200000_de": "original SDK unit; clean shim required (SDK: shim binding decision)",
                       "func_802BADC0_de": "original SDK unit src/AUDIO/802BFEB0.CPP; clean shim required (SDK: shim binding decision)"}}
        closure.tidy(d, self.facts)
        self.assertNotIn("func_80200000_de", d["stops"])
        self.assertEqual(d["bindings"].get("func_802BADC0_de"), "osGetTime")

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

    def test_no_c_stop_from_an_older_pin_is_routed_again(self):
        self.d["stops"]["func_8044BFD0"] = "no C definition at dc7362a (pinned 0x8044BFD0 // type:label)"
        self.d["stops"]["func_80200000_de"] = "no C definition at abcdef0"
        closure.tidy(self.d, self.facts, {}, "abcdef0")
        self.assertEqual(list(self.d["stops"]), ["func_80200000_de"], "a stop of this pin stays")
        closure.tidy(self.d, self.facts, {}, "1234567")
        self.assertEqual(self.d["stops"], {})
        self.assertEqual(self.route("func_80200000_de"), ("added", "src/GAME/80200000.CPP"))

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

    def test_versioned_alias_of_a_function_binds_to_the_function_not_to_storage(self):
        # D_0024B52C_us_rev1 pins 0x0024B52C; a function sits at 0x8024B52C, and the game calls through the address.
        self.facts.addrs["D_00200000_us_rev1"] = ("0x00200000", "")
        kind, detail = self.route("D_00200000_us_rev1")
        self.assertEqual(self.d["bindings"].get("D_00200000_us_rev1"), "func_80200000_de")
        self.assertNotIn("D_00200000_us_rev1", self.d["data"], "zeroed data would be executed as code")
    def test_alias_of_a_renamed_function_binds_and_an_unbindable_one_is_listed(self):
        # A table of callbacks stores (s32) &D_00203AD8; the function there was renamed (no func_ prefix).
        self.facts.text["src/GAME/80203AD8.CPP"] = "void Turret_Code_IDLE(void) {\n}\nextern char D_00203AD8[];\n" \
            "extern char D_00203B00[];\nextern char D_00203B20[];\nu32 tbl[] = {(u32)D_00203AD8, (u32)D_00203B00};\n"
        self.facts.addrs["Turret_Code_IDLE"] = ("0x80203AD8", " // type:func")
        self.facts.addrs["Turret_Gone"] = ("0x80203B00", " // type:func")  # pinned function nothing defines
        self.facts.addrs["D_00203B20"] = ("0x00203B20", "")  # a TLB data record: nothing pinned as a function there
        self.assertEqual(self.facts.function_alias("D_00203AD8"), "Turret_Code_IDLE")
        self.assertEqual(self.route("D_00203AD8")[0], "added")
        self.assertEqual(self.d["bindings"]["D_00203AD8"], "Turret_Code_IDLE")
        self.assertEqual([(s, n) for s, _, n in self.facts.unbound_aliases()], [("D_00203B00", ["Turret_Gone"])])
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
        self.d["stops"]["func_80200000_de"] = "alias target; not linked yet at dc7362a (boot frontier)"
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

    def test_tlb_mapped_record_binds_its_virtual_address(self):
        facts = self.facts.addrs
        facts["D_0044A850"] = ("0x0044B4A0", "")
        self.assertEqual(self.route("D_0044A850", frontier=True), ("routed", "data (TLB-mapped) 0x0044B4A0"))
        self.assertEqual(self.d["data"]["D_0044A850"], "0x0044B4A0")
        self.assertNotIn("D_0044A850", self.d["stops"])
        closure.tidy(self.d, self.facts)  # the next round keeps it (tidy used to drop every sub-KSEG0 data row)
        self.assertEqual(self.d["data"].get("D_0044A850"), "0x0044B4A0")

    def test_advance_routes_an_image_code_pointer_stop(self):
        # boot-missing names a stop no manifest row has (the data image's generated stop for a KUSEG alias): it
        # binds to the function and the walk goes on; the manifest gains the binding
        self.facts.addrs["D_00200000"] = ("0x00200000", "")
        root = pathlib.Path(self.tmp.name)
        (root / "decomp-boot").mkdir()
        (root / "decomp-boot/image.json").write_text(json.dumps({"function_stops": ["D_00200000"]}))
        _, unchanged, _, closures = self.run_advance((78, "boot-missing: D_00200000"),
                                                          mock.Mock(returncode=0, stdout="closure: links\n", stderr=""))
        self.assertEqual((unchanged, closures), (False, 1))

    def test_linker_script_binding_target_counts_as_undefined(self):
        log = ("bindings.ld:321: undefined symbol `func_802953FC_us_rev1' referenced in expression\n"
               "a.o: in function `f': undefined reference to `func_80200000_de'\n")
        self.assertEqual(closure.undefined_symbols(log), ["func_80200000_de", "func_802953FC_us_rev1"])

    def test_fuzzy_view_of_a_global_binds_the_globals_address(self):
        self.assertEqual(self.route("fz_D_80100000"), ("routed", "data (fz_ view of D_80100000) 0x80104000"))
        self.assertEqual(self.d["data"]["fz_D_80100000"], "0x80104000")
        self.assertEqual(self.route("fz_D_80100000"), ("skip", ""))
        self.assertEqual(self.route("fz_D_80999999")[0], "routed")  # no pinned address: not bound as the global
        self.assertNotIn("fz_D_80999999", self.d["data"])
    def test_host_libgcc_helper_is_neither_bound_nor_stopped(self):
        self.facts.addrs["__udivdi3"] = ("0x802C5E90", " // type:func")
        self.facts.numeric["__udivdi3"] = 0x802C5E90
        self.assertEqual(self.route("__udivdi3"), ("skip", ""))
        self.assertNotIn("__udivdi3", self.d["stops"])
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

    RUN_ON_LOG = ("boot-diag: run-on: failures are recorded in boot order and the boot goes on (hang 15 s, limit 105 s)\n"
                  "boot-step: 5/932 func_80201000_de\n"
                  "boot-diag: missing func_80200000_de at=5/932 after=func_80201000_de in=func_80201000_de<main\n"
                  "boot-diag: layout dma-rom-0x00100000 at=5/932 after=func_80201000_de in=func_80201000_de "
                  "bytes=0x10 ram=0x00001000\n"
                  "boot-diag: missing func_80200100_de at=6/932 after=func_80201004_de in=func_80201004_de\n"
                  "boot-diag: fault SIGSEGV@func_80201008_de+0x4 at=7/932 after=func_80201008_de in=func_80201008_de "
                  "addr-00000000 load at host pc 0x8049491 return=func_80201008_de\n"
                  "boot-diag: end 4 failures (2 missing, 0 stop, 0 code-pointer, 1 fault, 1 layout, 0 hang), census 7/932\n")

    def test_advance_run_on_links_every_frontier_stop_of_one_boot(self):
        # one run-on boot names two frontier stops past a fault: both units link in one step, one closure
        for sym, unit in [("func_80200000_de", "src/GAME/80200000.CPP"), ("func_80200100_de", "src/GAME/80200100.CPP")]:
            self.d["stops"][sym] = unit + " defines it; not linked yet at x (boot frontier)"
            self.facts.text[unit] = "void " + sym + "(void) {\n}\n"
        root = pathlib.Path(self.tmp.name)
        (root / "boot.log").write_text(self.RUN_ON_LOG)
        ok = mock.Mock(returncode=0, stdout="closure: links\n", stderr="")
        rc, same, boots, closes = self.run_advance((78, "boot-step: census 7/932 hit"), ok, steps=1)
        self.assertEqual((rc, same, boots, closes), (0, False, 1, 1))
        d = json.loads((root / "advance.json").read_text())
        self.assertEqual(d["sources"][-2:], ["src/GAME/80200000.CPP", "src/GAME/80200100.CPP"])
        self.assertNotIn("func_80200000_de", d["stops"])

    def test_advance_run_on_halts_with_the_whole_list_routed(self):
        self.d["stops"]["func_80200000_de"] = "no C definition at abcdef0"
        self.d["stops"]["func_80200100_de"] = "src/GAME/80200000.CPP:7 conflicting types for 'x'; RW NEEDS one declaration"
        root = pathlib.Path(self.tmp.name)
        (root / "boot.log").write_text(self.RUN_ON_LOG)
        rc, same, boots, closes = self.run_advance((78, "boot-step: census 7/932 hit"))
        self.assertEqual((rc, same, boots, closes), (0, True, 1, 0))
        rows = [json.loads(line) for line in (root / "boot-diag.jsonl").read_text().splitlines()]
        self.assertEqual([(r["boot_order"], r["class"], r["owner"]) for r in rows],
                         [(1, "missing", "body"), (2, "layout", "byte-order"), (3, "missing", "decl"),
                          (4, "fault", "runtime")])
        self.assertEqual(rows[1]["details"], "bytes=0x10 ram=0x00001000")
        needs = {r["symbol"]: r for r in map(json.loads, (root / "needs.jsonl").read_text().splitlines())}
        self.assertEqual(needs["func_80200000_de"]["boot_order"], 1)
        self.assertEqual(needs["dma-rom-0x00100000"]["class"], "byte-order")
        self.assertEqual(needs["SIGSEGV@func_80201008_de+0x4"]["class"], "runtime")
        self.assertEqual(needs["SIGSEGV@func_80201008_de+0x4"]["trace_order"], 7)

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



spec = importlib.util.spec_from_file_location("boot_linkall", ROOT / "linkall.py")
linkall = importlib.util.module_from_spec(spec)
spec.loader.exec_module(linkall)
spec = importlib.util.spec_from_file_location("boot_unitstep", ROOT / "unitstep.py")
unitstep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(unitstep)
class LinkAllTests(unittest.TestCase):
    """The link-all archive: the linker pulls units, and what nothing defines becomes a generated stop."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=args.work)
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
    def compile(self, name, text):
        source = self.root / (name + ".c")
        source.write_text(text)
        out = self.root / (name + ".o")
        subprocess.run(["gcc", "-c", str(source), "-o", str(out)], check=True)
        return out
    def test_pull_follows_references_through_archive_members(self):
        defined_by = {"a": ("A", {"a"}, {"b", "lib"}), "b": ("B", {"b"}, set()), "c": ("C", {"c"}, {"x"})}
        pulled, unresolved = linkall.pull({"a"}, set(), defined_by)
        self.assertEqual(sorted(pulled), ["A", "B"], "C is not referenced, so it is never pulled")
        self.assertEqual(unresolved, {"lib"})
    def test_unit_that_does_not_compile_leaves_empty_objects_and_a_marker(self):
        base = self.root / "u"
        bad = [sys.executable, "-c", "import sys; print('x.c:3:5: error: conflicting types', file=sys.stderr); sys.exit(1)"]
        self.assertEqual(unitstep.unit(["--base", str(base), "--source", "src/X.c", "--", *bad]), 0)
        self.assertEqual((self.root / "u.failed").read_text(), "src/X.c:3 conflicting types\n")
        self.assertEqual((self.root / "u.o").stat().st_size, 0)
        self.assertEqual((self.root / "u.stubs.o").stat().st_size, 0)
        touch = [sys.executable, "-c", "import pathlib; pathlib.Path('%s').write_text('ok')" % (self.root / "u.o")]
        self.assertEqual(unitstep.unit(["--base", str(base), "--source", "src/X.c", "--", *touch, "---", *touch]), 0)
        self.assertFalse((self.root / "u.failed").exists(), "a unit that compiles again drops its marker")
    def test_archive_holds_the_non_empty_objects_of_the_plan(self):
        objdir = self.root / "decomp"
        (objdir / "src").mkdir(parents=True)
        for unit in ("src/A.c", "src/B.c"):
            (self.root / "t.c").write_text("int " + pathlib.Path(unit).stem.lower() + "_v = 1;\n")
            subprocess.run(["gcc", "-c", str(self.root / "t.c"), "-o", str(objdir / (unit + ".o"))], check=True)
        (objdir / "src/B.c.o").write_bytes(b"")  # B did not compile
        (self.root / "plan.txt").write_text("src/A.c\nsrc/B.c\n")
        (self.root / "closure.txt").write_text("/x/boot.o\n")
        out = self.root / "lib.a"
        linkall.archive(["--plan", str(self.root / "plan.txt"), "--objdir", str(objdir), "--closure-objects",
                         str(self.root / "closure.txt"), "--output", str(out), "--members", str(self.root / "m.txt"),
                         "--linked", str(self.root / "l.txt")])
        names = subprocess.run(["nm", "-g", "-P", str(out)], capture_output=True, text=True).stdout
        self.assertIn("a_v", names)
        self.assertNotIn("b_v", names)
        self.assertEqual((self.root / "m.txt").read_text().split(), [str(objdir / "src/A.c.o")])
        self.assertEqual((self.root / "l.txt").read_text().split(), ["/x/boot.o", str(objdir / "src/A.c.o")])
    def test_autogen_routes_what_nothing_defines_and_pulls_the_rest(self):
        source = self.root / "source"
        (source / "src/GAME").mkdir(parents=True)
        (source / "versions/us-rev1").mkdir(parents=True)
        (source / "src/GAME/A.CPP").write_text("void func_80200000_de(void) {\n}\n")
        (source / "versions/us-rev1/symbol_addrs.txt").write_text(
            "func_80200000_de = 0x80200000; // type:func\nfunc_80200100_de = 0x80200100; // type:func\n"
            "D_80100000 = 0x80104000; //\n")
        boot = self.root / "boot"
        boot.mkdir()
        entry = self.compile("entry", "void func_80200000_de(void); void n64c_entry(void){func_80200000_de();}\n")
        a = self.compile("a", "extern int D_80100000; void func_80200100_de(void); extern int platform_call(void);\n"
                              "void func_80200000_de(void){func_80200100_de(); D_80100000 = platform_call();}\n")
        b = self.compile("b", "void func_80200300_de(void){}\n")
        lib = self.compile("lib", "int platform_call(void){return 1;}\n")
        (boot / "linked-objects.txt").write_text(f"{entry}\n")
        (boot / "image-objects.txt").write_text("")
        (boot / "image.ld").write_text("")
        (boot / "image.json").write_text("{}")
        (self.root / "members.txt").write_text(f"{a}\n{b}\n")
        (self.root / "plan.txt").write_text("src/GAME/A.CPP\n")
        manifest = self.root / "us_rev1.json"
        manifest.write_text(json.dumps({"source_pin": "abcdef0" + "0" * 33, "entry": "n64c_entry", "sources": [],
                                        "data": {}, "bindings": {}, "stops": {}}))
        out = self.root / "effective.json"
        # the entry object calls func_80200000_de, which only the archive defines; pretend a.o is the member
        (boot / "linked-objects.txt").write_text(f"{entry}\n")
        linkall.autogen(["--source", str(source), "--manifest", str(manifest), "--boot", str(boot),
                         "--objdir", str(self.root), "--plan", str(self.root / "plan.txt"),
                         "--members", str(self.root / "members.txt"), "--output", str(out), "--libs", str(lib)])
        effective = json.loads(out.read_text())
        self.assertIn("func_80200100_de", effective["stops"], "no unit defines it: a generated run-on stop")
        self.assertEqual(effective["data"], {"D_80100000": "0x80104000"}, "pinned data binds to its address")
        self.assertNotIn("func_80200000_de", effective["stops"], "the archive defines it; the linker pulls the unit")
        self.assertNotIn("platform_call", effective["stops"], "a platform library name is the linker's")
        self.assertEqual(json.loads(manifest.read_text())["stops"], {}, "the committed manifest is untouched")
    def autogen_tree(self, files, symbols, entry_c, member_c, stops=None):
        source = self.root / "source"
        (source / "src/GAME").mkdir(parents=True)
        (source / "versions/us-rev1").mkdir(parents=True)
        for name, text in files.items():
            (source / "src/GAME" / name).write_text(text)
        (source / "versions/us-rev1/symbol_addrs.txt").write_text(symbols)
        boot = self.root / "boot"
        boot.mkdir()
        entry = self.compile("entry", entry_c)
        members = [self.compile(n, text) for n, text in member_c.items()]
        (boot / "linked-objects.txt").write_text(f"{entry}\n")
        (boot / "image-objects.txt").write_text("")
        (boot / "image.ld").write_text("")
        (boot / "image.json").write_text("{}")
        (self.root / "members.txt").write_text("".join(f"{m}\n" for m in members))
        (self.root / "plan.txt").write_text("")
        manifest = self.root / "us_rev1.json"
        manifest.write_text(json.dumps({"source_pin": "abcdef0" + "0" * 33, "entry": "n64c_entry", "sources": [],
                                        "data": {}, "bindings": {}, "stops": stops or {}}))
        out = self.root / "effective.json"
        linkall.autogen(["--source", str(source), "--manifest", str(manifest), "--boot", str(boot),
                         "--objdir", str(self.root), "--plan", str(self.root / "plan.txt"),
                         "--members", str(self.root / "members.txt"), "--output", str(out)])
        return json.loads(out.read_text()), json.loads(manifest.read_text())
    def test_autogen_replaces_a_committed_stop_the_pulled_member_defines(self):
        effective, committed = self.autogen_tree(
            {"A.CPP": "void func_80200100_de(void) {\n}\n"},
            "func_80200100_de = 0x80200100; // type:func\n",
            "void func_80200100_de(void); void n64c_entry(void){func_80200100_de();}\n",
            {"a": "void func_80200100_de(void){}\n"},
            stops={"func_80200100_de": "src/GAME/A.CPP defines it; not linked yet (boot frontier)"})
        self.assertEqual(effective["stops"], {}, "the member defines it: the stop would be a multiple definition")
        self.assertIn("func_80200100_de", committed["stops"], "the committed manifest is untouched")
    def test_autogen_pulls_alias_targets_and_routes_their_own_undefined_names(self):
        effective, _ = self.autogen_tree(
            {"B.CPP": "void func_80200300_de(void) {\n}\n"},
            "func_80200300_de = 0x80200300; // type:func\nfunc_80200400_de = 0x80200400; // type:func\n",
            "extern int D_00200300; int n64c_entry(void){return D_00200300;}\n",
            {"b": "void func_80200400_de(void); void func_80200300_de(void){func_80200400_de();}\n"})
        self.assertEqual(effective["bindings"].get("D_00200300"), "func_80200300_de", "the alias binds to its function")
        self.assertIn("func_80200400_de", effective["stops"],
                      "the pulled target undefined name is routed (fixpoint), not left as a link failure")
unittest.main(argv=[sys.argv[0]])
