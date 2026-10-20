import unittest
from pathlib import Path
from unittest import mock

from abu_decomp.behave import candidate as cand
from abu_decomp.behave import names

REV1 = """
glabel f
    lui        $s0, %hi(D_8014D340_us_rev1)
    addiu      $s0, $s0, %lo(D_8014D340_us_rev1)
    jal        func_8028BEAC_de
"""
DE = REV1.replace("D_8014D340_us_rev1", "D_801470C0_de")
SYMBOLS = {"D_8014D340_us_rev1": [0x8014D340, None, 4, "d"], "func_8028BEAC_de": [0x8028BEAC, 0, 4, "f"]}


class CounterpartTests(unittest.TestCase):
    def test_operands_are_relocations_jal_targets_and_words(self):
        text = "lui $a0, %hi(A)\nlw $a0, %gp_rel(B)($gp)\njal C\n.word D\naddiu $a0, $a0, 4\n"
        self.assertEqual(names.operands(text), ["A", "B", "C", "D"])

    def test_a_name_pairs_with_the_name_the_tested_version_uses_at_the_same_operand(self):
        found = names.counterparts(REV1, {"de": DE}, ["D_801470C0_de"], SYMBOLS)
        self.assertEqual(found, {"D_801470C0_de": 0x8014D340})

    def test_names_that_agree_or_were_not_asked_for_are_left_alone(self):
        self.assertEqual(names.counterparts(REV1, {"de": DE}, ["func_8028BEAC_de"], SYMBOLS), {})

    def test_two_different_counterparts_leave_the_name_unresolved(self):
        clash = REV1.replace("%lo(D_8014D340_us_rev1)", "%lo(D_other)")
        symbols = {**SYMBOLS, "D_other": [0x8014D344, None, 4, "d"]}
        self.assertEqual(names.counterparts(clash, {"a": DE}, ["D_801470C0_de"], symbols), {})

    def test_listings_that_do_not_line_up_vote_nothing(self):
        self.assertEqual(names.counterparts(REV1 + "jal extra\n", {"de": DE}, ["D_801470C0_de"], SYMBOLS), {})

    def test_a_counterpart_without_an_address_is_not_used(self):
        self.assertEqual(names.counterparts(REV1, {"de": DE}, ["D_801470C0_de"], {"D_8014D340_us_rev1": [None]}), {})


class VersionNamesTests(unittest.TestCase):
    def test_the_function_is_read_in_every_version_that_holds_it(self):
        tables = {"us-rev1": {"f": [0x80000000]}, "de": {"f": [0x80000000]}, "eu": {}}
        listings = {"us-rev1.s": REV1, "de.s": DE}

        def api(profile, function, args, **kw):
            self.assertEqual(function, "asm_path")
            return f"{args[0]}.s"

        with (
            mock.patch.object(cand, "all_symbol_tables", return_value=tables),
            mock.patch.object(cand, "api_call", side_effect=api),
            mock.patch.object(Path, "read_text", lambda self, **kw: listings[self.name]),
        ):
            found = cand.version_names(
                None,
                version="us-rev1",
                functions=["f"],
                wanted=["D_801470C0_de"],
                symbols=SYMBOLS,
                project=Path("/nonexistent"),
                host=None,
            )
        self.assertEqual(found, {"D_801470C0_de": 0x8014D340})

    def test_a_name_only_a_sibling_uses_resolves_through_the_sibling(self):
        # the function under test (`f`) spells no D_801470C0_de; its sibling `g` does
        tables = {"us-rev1": {"f": [1], "g": [2]}, "de": {"f": [1], "g": [2]}}
        listings = {
            "us-rev1-f.s": "glabel f\n    jal func_8028BEAC_de\n",
            "de-f.s": "glabel f\n    jal func_8028BEAC_de\n",
            "us-rev1-g.s": REV1,
            "de-g.s": DE,
        }
        asked = []

        def api(profile, function, args, **kw):
            asked.append(args[1])
            return f"{args[0]}-{args[1]}.s"

        with (
            mock.patch.object(cand, "all_symbol_tables", return_value=tables),
            mock.patch.object(cand, "api_call", side_effect=api),
            mock.patch.object(Path, "read_text", lambda self, **kw: listings[self.name]),
        ):
            found = cand.version_names(
                None,
                version="us-rev1",
                functions=["f", "g"],
                wanted=["D_801470C0_de"],
                symbols=SYMBOLS,
                project=Path("/nonexistent"),
                host=None,
            )
        self.assertEqual(found, {"D_801470C0_de": 0x8014D340})
        self.assertEqual(asked[0], "f")  # the function under test is read first

    def test_the_functions_holding_a_relocation_against_a_name_use_it(self):
        functions = [("g", 0x40, 0x20), ("f", 0, 0x40), ("h", 0x60, 0x10)]
        relocs = [(0x10, "R_MIPS_26", "h"), (0x44, "R_MIPS_HI16", "D_x"), (0x48, "R_MIPS_LO16", "D_x")]
        self.assertEqual(cand.referencing(functions, relocs, {"D_x"}), ["g"])
        self.assertEqual(cand.referencing(functions, relocs, {"h", "D_x"}), ["f", "g"])

    def test_a_version_with_no_listing_resolves_nothing(self):
        with mock.patch.object(cand, "all_symbol_tables", return_value={"us-rev1": {}}):
            found = cand.version_names(
                None, version="us-rev1", functions=["f"], wanted=["x"], symbols={}, project=Path("/n"), host=None
            )
        self.assertEqual(found, {})


class UndefinedTests(unittest.TestCase):
    def test_undefined_names_come_from_the_object_symbol_table(self):
        out = (
            "x.o: format\n00000000 g       *UND*\t00000000 D_x\n"
            "00000000         *UND*\t00000000 D_y\n00000000 l       *UND*\t00000000 $L1\n"
            "80800000 g     F .text\t00000040 f\n"
        )
        with mock.patch.object(cand.subprocess, "run", return_value=mock.Mock(stdout=out)):
            self.assertEqual(cand.undefined_symbols("objdump", Path("x.o")), ["D_x", "D_y"])


if __name__ == "__main__":
    unittest.main()
