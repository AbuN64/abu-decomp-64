# SPDX-License-Identifier: GPL-3.0-or-later
import pathlib
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gates  # noqa: E402
import srcfiles  # noqa: E402


class SourceFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        for name, text in {
            "src/A/a.CPP": "", "src/B/b.c": "", "src/B/c.cpp": "", "src/libgcc/g.c": "", "src/B/x.s": "",
            "include/A.H": "extern u32 D_00000000;\n", "include/b.h": "extern u8 D_00000001;\n", "include/n.txt": "",
        }.items():
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(text)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sources_any_case_and_c(self):
        got = [str(p.relative_to(self.root)) for p in srcfiles.sources(self.root)]
        self.assertEqual(got, ["src/A/a.CPP", "src/B/b.c", "src/B/c.cpp"])
        self.assertEqual(gates.unit_list(self.root), got)

    def test_headers_any_case(self):
        got = [str(p.relative_to(self.root)) for p in srcfiles.headers(self.root)]
        self.assertEqual(got, ["include/A.H", "include/b.h"])


if __name__ == "__main__":
    unittest.main()
