"""data/stamp.py: a diff touching declarations or data needs a passing all-version check stamp of its hash."""

import tempfile
import unittest
from pathlib import Path

from abu_decomp.data import stamp
from abu_decomp.data.config import DataConfig

TYPE = """diff --git a/include/COMMON/UNUSED.H b/include/COMMON/UNUSED.H
--- a/include/COMMON/UNUSED.H
+++ b/include/COMMON/UNUSED.H
@@ -1,2 +1,1 @@
-extern s32 D_80154024;
"""
USE = """diff --git a/src/a.CPP b/src/a.CPP
--- a/src/a.CPP
+++ b/src/a.CPP
@@ -1 +1 @@
-    if (D_80154024 == 0) {
+    if (D_80154020.ready == 0) {
"""
PLAIN = """diff --git a/src/a.CPP b/src/a.CPP
--- a/src/a.CPP
+++ b/src/a.CPP
@@ -1 +1 @@
-    x = 1;
+    x = 2;
"""


MAKEFILE = """diff --git a/Makefile b/Makefile
--- a/Makefile
+++ b/Makefile
@@ -1 +1 @@
-# extern D_80154024 header list
+# extern D_80154020 header key
"""


class StampTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.cfg = DataConfig()

    def test_touching_diffs_need_a_stamp(self):
        for text in (TYPE, USE):
            self.assertTrue(stamp.touches_data(self.cfg, text))
            self.assertTrue(stamp.missing(self.cfg, self.dir, text, Path("x.diff")))
        self.assertFalse(stamp.touches_data(self.cfg, PLAIN))
        self.assertFalse(stamp.touches_data(self.cfg, MAKEFILE))
        self.assertEqual(stamp.missing(self.cfg, self.dir, PLAIN, Path("x.diff")), [])

    def test_stamp_is_for_that_change(self):
        stamp.write(self.dir, USE)
        self.assertEqual(stamp.missing(self.cfg, self.dir, USE, Path("x.diff")), [])
        self.assertTrue(stamp.missing(self.cfg, self.dir, USE.replace("ready", "done"), Path("x.diff")))

    def test_files_of(self):
        self.assertEqual(stamp.files_of(TYPE + USE), ["include/COMMON/UNUSED.H", "src/a.CPP"])


if __name__ == "__main__":
    unittest.main()


def test_pure_rename_allows_redundant_externs_and_the_include_that_replaces_them():
    rows = (
        "diff --git a/versions/us/symbol_addrs.txt b/versions/us/symbol_addrs.txt\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n"
        "-func_80000000_de = 0x80000000; // type:func\n+osX = 0x80000000; // type:func\n"
    )
    src = (
        "diff --git a/src/a.c b/src/a.c\n--- a/src/a.c\n+++ b/src/a.c\n@@ -1,3 +1,3 @@\n"
        "-extern void func_80000000_de(int);\n-extern void func_80000004_de(int);\n+extern void osX(int);\n"
        '+#include "COMMON/EXTERNS.H"\n'
    )
    assert stamp.pure_rename(rows + src, (".c",))
    retyped = src.replace("+extern void osX(int);", "+extern long osX(int);")
    assert not stamp.pure_rename(rows + retyped, (".c",))  # a changed type is not a rename
    added = src.replace("+extern void osX(int);", "+extern void osX(int);\n+extern void other(int);")
    assert not stamp.pure_rename(rows + added, (".c",))  # a new declaration is not redundant


def test_extraction_files_are_symbol_rows_and_splat_config():
    assert stamp.extraction_files(["src/a.c", "versions/us/symbol_addrs.txt", "versions/de/splat.yaml"]) == [
        "versions/us/symbol_addrs.txt",
        "versions/de/splat.yaml",
    ]
    assert stamp.extraction_files(["include/x.h"]) == []


def test_a_rename_may_drop_the_alias_row_of_the_symbol_it_renames():
    diff = (
        "diff --git a/versions/de/aliases.txt b/versions/de/aliases.txt\n--- a/versions/de/aliases.txt\n+++ b/versions/de/aliases.txt\n"
        "@@ -1,2 +1,1 @@\n-literals_func_1_de = 0x800C4AEC; // rom:0x1 size:0xC\n keep = 0x800C4AF0;\n"
        "diff --git a/versions/de/symbol_addrs.txt b/versions/de/symbol_addrs.txt\n--- a/versions/de/symbol_addrs.txt\n"
        "+++ b/versions/de/symbol_addrs.txt\n@@ -1,1 +1,1 @@\n-literals_func_1_de = 0x800C4AEC; // rom:0x1 size:0xC\n"
        "+literals_Fn = 0x800C4AEC; // rom:0x1 size:0xC\n"
    )
    assert stamp.pure_rename(diff, (".c", ".cpp"))
    other = diff.replace(
        "-literals_func_1_de", "-unrelated_alias", 1
    )  # an alias row of a name the diff does not rename
    assert not stamp.pure_rename(other, (".c", ".cpp"))


def test_a_msg_line_added_after_the_check_keeps_the_stamp(tmp_path):
    from abu_decomp.data import stamp

    diff = "diff --git a/a.c b/a.c\n--- a/a.c\n+++ b/a.c\n@@ -1 +1 @@\n-x\n+y\n"
    stamp.write(tmp_path, diff)
    assert stamp.stamped(tmp_path, "# MSG: Exact Foo\n" + diff)
    assert stamp.stamped(tmp_path, "# MSG: Exact Foo, other words\n# note\n" + diff)
    assert not stamp.stamped(tmp_path, "# MSG: Exact Foo\n" + diff.replace("+y", "+z"))


def test_a_clean_rebase_keeps_the_stamp_and_an_edited_line_does_not(tmp_path):
    from abu_decomp.data import stamp

    before = (
        "diff --git a/a.c b/a.c\nindex 1111111..2222222 100644\n--- a/a.c\n+++ b/a.c\n"
        "@@ -10,4 +10,4 @@ int f(void)\n ctx\n-x\n+y\n tail\n"
    )
    after = (
        "diff --git a/a.c b/a.c\nindex 3333333..4444444 100644\n--- a/a.c\n+++ b/a.c\n"
        "@@ -42,5 +42,5 @@ int g(void)\n moved\n other\n-x\n+y\n tail\n"
    )
    stamp.write(tmp_path, before)
    assert stamp.stamped(tmp_path, after)
    assert not stamp.stamped(tmp_path, after.replace("-x", "-w"))
    assert not stamp.stamped(tmp_path, after.replace("a/a.c b/a.c", "a/b.c b/b.c"))
    two_hunks = after + "@@ -90,2 +90,2 @@\n-x\n+y\n"
    assert not stamp.stamped(tmp_path, two_hunks)  # a second hunk is a second change
