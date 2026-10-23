import tempfile
import unittest
from pathlib import Path

from abu_decomp.behave import abi

HAND = """glabel rnc
    /* 1 80000000 2003FFFF */  addi       $v1, $zero, -0x1 /* handwritten instruction */
    /* 2 80000004 0233A820 */  add        $s5, $s1, $s3 /* handwritten instruction */
    /* 3 80000008 03E00008 */  jr         $ra
"""
COMPILED = """glabel plain
    /* 1 80000000 AFB00000 */  sw         $s0, 0x0($sp)
    /* 2 80000004 00808021 */  addu       $s0, $a0, $zero
    /* 3 80000008 02001021 */  addu       $v0, $s0, $zero
    /* 4 8000000C 8FB00000 */  lw         $s0, 0x0($sp)
    /* 5 80000010 03E00008 */  jr         $ra
"""


class AbiTests(unittest.TestCase):
    def test_reading_a_saved_register_before_writing_it_is_a_hand_convention(self):
        self.assertTrue(abi.reads_saved_first(HAND))
        self.assertFalse(abi.reads_saved_first(COMPILED))

    def test_opaque_finds_the_routines_in_the_versions_asm(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "asm" / "v"
            home.mkdir(parents=True)
            (home / "rnc.s").write_text(HAND)
            (home / "plain.s").write_text(COMPILED)
            (home / "nonmatchings").mkdir()
            (home / "nonmatchings" / "marked.s").write_text("/* Handwritten function */\nglabel marked\n")
            got = abi.opaque(Path(directory), "v", {"rnc", "plain", "marked", "missing"})
            self.assertEqual(got, {"rnc", "marked"})


class MidFrameTests(unittest.TestCase):
    ENTERED_LATE = """glabel late
    /* 1 80000004 AFB10004 */  sw         $s1, 0x4($sp)
    /* 2 80000008 8FB90018 */  lw         $t9, 0x18($sp)
    /* 3 8000000C 03E00008 */  jr         $ra
    /* 4 80000010 27BD0008 */   addiu     $sp, $sp, 0x8
"""
    WHOLE = """glabel whole
    /* 1 80000000 27BDFFF8 */  addiu      $sp, $sp, -0x8
    /* 2 80000004 AFB10004 */  sw         $s1, 0x4($sp)
    /* 3 8000000C 03E00008 */  jr         $ra
    /* 4 80000010 27BD0008 */   addiu     $sp, $sp, 0x8
"""

    def test_a_release_with_no_allocation_means_the_entry_is_after_the_prologue(self):
        self.assertEqual(abi.enters_mid_frame(self.ENTERED_LATE), 8)

    def test_a_balanced_frame_and_a_frameless_leaf_are_fine(self):
        self.assertIsNone(abi.enters_mid_frame(self.WHOLE))
        self.assertIsNone(abi.enters_mid_frame(COMPILED))

    def test_a_stack_pointer_set_another_way_is_not_judged(self):
        text = self.ENTERED_LATE.replace("lw         $t9, 0x18($sp)", "addu       $sp, $a0, $zero")
        self.assertIsNone(abi.enters_mid_frame(text))

    def test_the_defect_names_the_function_and_reads_the_projects_assembly(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "asm" / "v1" / "nonmatchings" / "span"
            home.mkdir(parents=True)
            (home / "late.s").write_text(self.ENTERED_LATE)
            (home / "whole.s").write_text(self.WHOLE)
            self.assertIn("late releases 8 bytes", abi.entry_defect(Path(tmp), "v1", "late"))
            self.assertIsNone(abi.entry_defect(Path(tmp), "v1", "whole"))
            self.assertIsNone(abi.entry_defect(Path(tmp), "v1", "absent"))
