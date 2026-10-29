# SPDX-License-Identifier: GPL-3.0-or-later
"""Refused native units are observations, never reusable compilation results."""
import pathlib
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import unitstep


class RefusalCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.base = self.root / "unit"
        self.opts = dict(objcache=str(self.root / "cache"), root=str(self.root), maps=[],
                         outs=[str(self.base) + ".o", str(self.base) + ".failed"], drops=[],
                         base=str(self.base), depfile=str(self.root / "unit.d"))
        self.cache = unitstep.ObjectCache(self.opts, [[sys.executable, "-c", "pass"]])

    def test_old_zero_dependency_refusal_is_not_restored(self):
        # Real failure shape: syntax checking stopped before a depfile was populated.
        self.cache.write([dict(deps=[], sigs=[], files=[b"", b"shared.h:181 duplicate member team\n"])])
        self.assertFalse(self.cache.restore())
        self.assertFalse(pathlib.Path(str(self.base) + ".failed").exists())

    def test_refusal_with_dependencies_is_not_stored(self):
        header = self.root / "shared.h"
        header.write_text("struct Record { int team; int team; };\n")
        pathlib.Path(self.opts["depfile"]).write_text(str(self.base) + ".o: " + str(header) + "\n")
        pathlib.Path(str(self.base) + ".failed").write_text("shared.h:1 duplicate member team\n")
        self.cache.store()
        self.assertFalse(pathlib.Path(self.cache.manifest).exists())

    def test_empty_dependency_success_is_not_stored(self):
        pathlib.Path(self.opts["depfile"]).write_text(str(self.base) + ".o:\n")
        self.cache.store()
        self.assertFalse(pathlib.Path(self.cache.manifest).exists())


if __name__ == "__main__":
    unittest.main()
