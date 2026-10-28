# SPDX-License-Identifier: GPL-3.0-or-later
import itertools
import pathlib
import subprocess
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
            "src/sdk/libgcc/runtime.C": "", "src/B/not.c.bak": "",
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

    def test_cmake_and_boot_share_every_suffix_case(self):
        # The filename that exposed the real cutover refusal, plus every spelling
        # of the supported suffixes. Headers, backups, assembly and libgcc stay out.
        names = ["src/game/VIDEO/VHANDLER.C", "src/nested/unit.C", "src/nested/unit/unit.C"]
        for suffix in srcfiles.SOURCE_SUFFIXES:
            for letters in itertools.product(*[(c.lower(), c.upper()) for c in suffix[1:]]):
                names.append("src/cases/unit." + "".join(letters))
        for name in names:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("int unit;\n")
        (self.root / "src/looks.C").mkdir()
        shim = pathlib.Path(srcfiles.__file__).resolve().parents[1]
        (self.root / "CMakeLists.txt").write_text(
            'cmake_minimum_required(VERSION 3.20)\nproject(SuffixProof NONE)\n'
            f'include("{shim}/cmake/sources.cmake")\n'
            'n64c_source_files(units "${CMAKE_CURRENT_SOURCE_DIR}")\n'
            'list(JOIN units "\\n" listing)\n'
            'file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/units.txt" "${listing}\\n")\n'
        )
        build = self.root / "build"
        result = subprocess.run(["cmake", "-S", str(self.root), "-B", str(build)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        found = (build / "units.txt").read_text().splitlines()
        expected = [str(p) for p in srcfiles.sources(self.root)]
        self.assertEqual(found, expected)
        self.assertEqual(len(found), 28)  # 22 suffix cases, VHANDLER.C, two nested and the original three
        self.assertEqual(gates.unit_list(self.root), [str(p.relative_to(self.root)) for p in map(pathlib.Path, found)])

    def test_discovery_is_independent_of_ancestor_names(self):
        # Only libgcc directories within src exclude runtime units.
        parent = self.root / "libgcc" / "project"
        path = parent / "src" / "ordinary.C"
        path.parent.mkdir(parents=True)
        path.write_text("int unit;\n")
        self.assertEqual(srcfiles.sources(parent), [path])

    def test_headers_any_case(self):
        got = [str(p.relative_to(self.root)) for p in srcfiles.headers(self.root)]
        self.assertEqual(got, ["include/A.H", "include/b.h"])


if __name__ == "__main__":
    unittest.main()
