# SPDX-License-Identifier: GPL-3.0-or-later
"""hangcore.py: memory operands of the spinning frame resolve to the addresses the spin reads."""
import pathlib
import sys
import unittest
HERE = pathlib.Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import hangcore


class Operands(unittest.TestCase):
    def test_base_displacement(self):
        self.assertEqual(hangcore.operands("mov    0x10(%esi),%eax"), [(0x10, "esi", None, 1)])

    def test_scaled_index(self):
        self.assertEqual(hangcore.operands("cmp    %eax,-0x1c(%ebp,%ecx,4)"), [(-0x1c, "ebp", "ecx", 4)])

    def test_absolute_loads(self):
        self.assertEqual(hangcore.operands("mov    0x800f7d20,%edi"), [(0x800f7d20, None, None, 1)])
        self.assertEqual(hangcore.operands("mov    %ds:0x80104574,%eax"), [(0x80104574, None, None, 1)])
        self.assertEqual(hangcore.operands("cmpl   $0x0,0x8010515c"), [(0x8010515c, None, None, 1)])

    def test_no_memory(self):
        for text in ("mov    $0x800f7d20,%edi", "call   0x80926d0 <func_8028FDB4_de>", "lea    0x4d0(%edi),%eax",
                     "jne    0x8088fa0 <func_80254B58_de+16>", "add    %ecx,%eax"):
            self.assertEqual(hangcore.operands(text), [], text)

    def test_address(self):
        regs = {"esi": 0x80104560, "ecx": 3, "ebp": 0xb881eb40}
        self.assertEqual(hangcore.address((0x10, "esi", None, 1), regs), 0x80104570)
        self.assertEqual(hangcore.address((-0x1c, "ebp", "ecx", 4), regs), 0xb881eb30)
        self.assertEqual(hangcore.address((-4, None, None, 1), regs), 0xfffffffc)


if __name__ == "__main__":
    unittest.main()
