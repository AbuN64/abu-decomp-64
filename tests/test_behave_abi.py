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
