import json
import re
import tempfile
import unittest
from pathlib import Path

from abu_decomp import DecompError
from abu_decomp.port import PortError, generate, load_spec

PIN_SHIM, PIN_DECOMP = "a" * 40, "b" * 40
SPEC = {
    "schema_version": 2,
    "name": "example-port",
    "title": "Example",
    "license": "GPL-3.0-or-later",
    "shim": {"repo": "https://example.invalid/abu-decomp-64.git", "pin": PIN_SHIM},
    "decomp": {"repo": "https://example.invalid/example-decomp.git", "version": "us", "pin": PIN_DECOMP},
    "boot": {"entry": "func_80001000_de", "rdram_bytes": "0x800000", "video_clock_hz": 48681812},
}


class PortTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def spec(self, **changes):
        path = self.root / "spec.json"
        path.write_text(json.dumps({**SPEC, **changes}))
        return load_spec(path)

    def test_skeleton_is_complete_and_fully_rendered(self):
        result = generate(self.spec(), self.root / "out")
        out = self.root / "out"
        for name in (
            "README.md",
            "LICENSE",
            ".gitignore",
            "CMakeLists.txt",
            "CMakePresets.json",
            "ci/check",
            "port.json",
            "cmake/deps.cmake",
            "cmake/shim.cmake",
            "platform/port/manifest.json",
            "platform/port/ENTRY_POINTS.tsv",
            "platform/port/OPEN_COUNT",
            "platform/port/VERSION_DEBT",
            "platform/port/shim-bindings.txt",
            "platform/port/boot-trace.tsv",
            "platform/port/test_shim_link.c",
        ):
            self.assertIn(name, result["files"])
        for path in out.rglob("*"):
            if path.is_file():
                self.assertIsNone(re.search(r"\{\{", path.read_text()), path)
        self.assertIn(f"abu_shim https://example.invalid/abu-decomp-64.git {PIN_SHIM}", (out / "cmake/shim.cmake").read_text())
        manifest = json.loads((out / "platform/port/manifest.json").read_text())
        self.assertEqual(
            manifest,
            {
                "source_pin": PIN_DECOMP,
                "version": "us",
                "rdram_bytes": "0x800000",
                "video_clock_hz": 48681812,
                "entry": "func_80001000_de",
                "sources": [],
            },
        )
        readme = (out / "README.md").read_text().splitlines()
        self.assertEqual(readme[0], "# example-port")
        self.assertIn("> This is a personal tool. It is not officially maintained", "\n".join(readme))
        self.assertTrue((out / "ci/check").stat().st_mode & 0o100)
        self.assertEqual(json.loads((out / "port.json").read_text()), SPEC)
        self.assertEqual(list(out.glob("LICENSE.*")), [])
        self.assertIn("GNU GENERAL PUBLIC LICENSE", (out / "LICENSE").read_text())
        self.assertFalse((out / "src/platform").exists(), "the retired native-crawl shape is gone")

    def test_bad_specs_and_nonempty_destinations_are_refused(self):
        for changes in (
            {"name": "Bad Name"},
            {"license": "CC0-1.0"},
            {"schema_version": 1},
            {"shim": {**SPEC["shim"], "pin": "abc123"}},
            {"decomp": {**SPEC["decomp"], "pin": "B" * 40}},
            {"boot": {**SPEC["boot"], "entry": "not a symbol"}},
            {"boot": {**SPEC["boot"], "rdram_bytes": "8MB"}},
            {"boot": {**SPEC["boot"], "video_clock_hz": 0}},
            {"title": 'quote"'},
            {"extra": 1},
        ):
            with self.subTest(changes=changes), self.assertRaises(PortError):
                self.spec(**changes)
        (self.root / "busy").mkdir()
        (self.root / "busy" / "file").write_text("")
        with self.assertRaisesRegex(DecompError, "not empty"):
            generate(self.spec(), self.root / "busy")


if __name__ == "__main__":
    unittest.main()
