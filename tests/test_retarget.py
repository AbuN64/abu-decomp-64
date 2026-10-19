import unittest

from test_land import LandTests as LandBase
from test_land import git

from abu_decomp import retarget

PAD = "".join(f"static int pad{i}(void) {{ return {i}; }}\n" for i in range(12))
OLD = PAD + "int func_old(void) { return 1; }\n"
NEW = PAD + "int func_old(void) { return 2; }\n"


class PureTests(unittest.TestCase):
    def test_a_symbol_is_renamed_only_when_one_name_leaves_and_one_comes_at_one_address(self):
        before = "a = 0x80001000;\nb = 0x80002000;\nc = 0x80003000;\n"
        after = "a2 = 0x80001000;\nb = 0x80002000;\nc = 0x80003000;\nc9 = 0x80003000;\n"
        self.assertEqual(retarget.symbol_pairs(before, after, set()), {"a": "a2"})
        self.assertEqual(retarget.symbol_pairs(before, after, {"a"}), {})  # the tree still defines the old name

    def test_rewrite_is_word_bounded_and_follows_a_moved_header_include(self):
        renames = retarget.Renames(symbols={"f1": "f2"}, files={"include/A/x.H": "include/A/y.H"})
        text = '#include "A/x.H"\nf1(); f10(); xf1();\n'
        self.assertEqual(retarget.rewrite(text, renames), '#include "A/y.H"\nf2(); f10(); xf1();\n')


class RetargetTests(LandBase):
    def rename_world(self):
        """A base with a symbol, its file and a caller; a lane patch cut there; then main renames all of it."""
        for name, text in {
            "src_old.c": OLD,
            "caller.c": "int g(void) { return func_old(); }\n",
            "versions_us.txt": "",
        }.items():
            (self.tree / name).write_text(text)
        (self.tree / "versions" / "us").mkdir(parents=True)
        (self.tree / "versions" / "us" / "symbol_addrs.txt").write_text("func_old = 0x80001000;\n")
        git(self.tree, "add", ".")
        git(self.tree, "commit", "-q", "-m", "world")
        lane = self.patch("L1", {"src_old.c": NEW, "caller.c": "int g(void) { return func_old() + 1; }\n"})
        git(self.tree, "mv", "src_old.c", "src_new.c")
        for name in ("src_new.c", "caller.c", "versions/us/symbol_addrs.txt"):
            path = self.tree / name
            path.write_text(path.read_text().replace("func_old", "func_new"))
        git(self.tree, "add", ".")
        git(self.tree, "commit", "-q", "-m", "recipe apply")
        return lane, git(self.tree, "rev-parse", "HEAD").strip()

    def test_a_patch_cut_before_the_renames_lands_on_the_renamed_names(self):
        lane, head = self.rename_world()
        result, _code, findings = self.land([lane], base=head)
        row = result["lanes"][0]
        self.assertEqual(row["status"], "applied", row)
        self.assertEqual(row["retargeted"]["symbols"], 1)
        self.assertEqual(row["retargeted"]["files"], 1)
        self.assertEqual((self.tree / "src_new.c").read_text(), PAD + "int func_new(void) { return 2; }\n")
        self.assertEqual((self.tree / "caller.c").read_text(), "int g(void) { return func_new() + 1; }\n")
        self.assertIn("land.retargeted", [f["key"] for f in findings])

    def test_a_patch_that_names_nothing_renamed_is_not_rewritten(self):
        _lane, head = self.rename_world()
        other = self.patch("L2", {"c.c": "int untouched(void) { return 9; }\n"})
        result, _code, _findings = self.land([other], base=head)
        self.assertNotIn("retargeted", result["lanes"][0])
        self.assertEqual(result["lanes"][0]["status"], "applied")


if __name__ == "__main__":
    unittest.main()
