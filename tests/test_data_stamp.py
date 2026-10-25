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

    def test_stamp_is_for_that_exact_diff(self):
        stamp.write(self.dir, USE)
        self.assertEqual(stamp.missing(self.cfg, self.dir, USE, Path("x.diff")), [])
        self.assertTrue(stamp.missing(self.cfg, self.dir, USE + " ", Path("x.diff")))

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
