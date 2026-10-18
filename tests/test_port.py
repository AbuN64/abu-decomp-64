import json
import re
import tempfile
import unittest
from pathlib import Path

from abu_decomp import DecompError
from abu_decomp.port import PortError, generate, load_spec

SPEC = {
    "schema_version": 1,
    "name": "example-port",
    "title": "Example",
    "executable": "example",
    "license": "CC0-1.0",
    "boot": {"subsystem": "Boot", "label": "entry", "owner": "0x80001000"},
    "decomp": {"repo": "https://example.invalid/example-decomp", "version": "us"},
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
            "src/platform/sys_public.h",
            "src/boot/boot.c",
            "tests/boot_gate.txt",
            "cmake/boot_gate.cmake",
        ):
            self.assertIn(name, result["files"])
        for path in out.rglob("*"):
            if path.is_file():
                self.assertIsNone(re.search(r"\{\{", path.read_text()), path)
        self.assertIn("owner 0x80001000 has no native implementation", (out / "src/boot/boot.c").read_text())
        readme = (out / "README.md").read_text().splitlines()
        self.assertEqual(readme[0], "# example-port")
        self.assertIn("> This is a personal tool. It is not officially maintained", "\n".join(readme))
        self.assertTrue((out / "ci/check").stat().st_mode & 0o100)
        self.assertEqual(json.loads((out / "port.json").read_text()), SPEC)
        self.assertEqual(list(out.glob("LICENSE.*")), [])

    def test_bad_specs_and_nonempty_destinations_are_refused(self):
        for changes in (
            {"name": "Bad Name"},
            {"license": "MIT"},
            {"boot": {**SPEC["boot"], "owner": "1000"}},
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
