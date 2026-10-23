# SPDX-License-Identifier: GPL-3.0-or-later
"""The shared digest memo: a warm file is not read again, a changed file is, and a damaged memo costs only speed."""
import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("digests", ROOT / "digests.py")
digests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(digests)


class DigestMemo(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.memo = str(self.dir / "cache" / "digests.json")
        self.file = self.dir / "h.h"
        self.file.write_text("one")

    def test_second_process_reads_nothing(self):
        first = digests.Digests(self.memo)
        value = first.of(str(self.file))
        first.save()
        with mock.patch.object(digests, "digest_file", side_effect=AssertionError("re-read")):
            self.assertEqual(digests.Digests(self.memo).of(str(self.file)), value)

    def test_changed_file_is_hashed_again(self):
        first = digests.Digests(self.memo)
        old = first.of(str(self.file))
        first.save()
        self.file.write_text("two!")
        self.assertNotEqual(digests.Digests(self.memo).of(str(self.file)), old)

    def test_same_size_new_mtime_is_hashed_again(self):
        first = digests.Digests(self.memo)
        old = first.of(str(self.file))
        first.save()
        self.file.write_text("two")
        os.utime(self.file, ns=(1, 1))
        self.assertNotEqual(digests.Digests(self.memo).of(str(self.file)), old)

    def test_damaged_memo_is_ignored(self):
        os.makedirs(os.path.dirname(self.memo))
        pathlib.Path(self.memo).write_text("{not json")
        memo = digests.Digests(self.memo)
        self.assertEqual(len(memo.of(str(self.file))), 64)
        memo.save()
        self.assertEqual(len(digests.Digests(self.memo).known), 1)

    def test_oldest_entries_go_past_the_cap(self):
        with mock.patch.object(digests, "KEEP", 2):
            memo = digests.Digests(self.memo)
            for index in range(4):
                path = self.dir / f"f{index}"
                path.write_text(str(index))
                memo.of(str(path))
            memo.save()
            self.assertEqual(len(digests.Digests(self.memo).known), 2)


if __name__ == "__main__":
    unittest.main()
