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
