import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from abu_decomp import DecompError
from abu_decomp.port import PortError, generate, load_spec

PIN_SHIM, PIN_DECOMP = "a" * 40, "b" * 40
SPEC = {
    "schema_version": 3,
    "format": 1,
    "name": "example-port",
    "title": "Example",
    "license": "GPL-3.0-or-later",
    "shim": {"repo": "https://example.invalid/abu-decomp-64.git", "pin": PIN_SHIM},
    "decomp": {"repo": "https://example.invalid/example-decomp.git", "version": "us", "pin": PIN_DECOMP},
    "boot": {"entry": "func_80001000_de", "rdram_bytes": "0x800000", "video_clock_hz": 48681812},
    "install": {
        "game": "example",
        "title": "Example",
        "product": "Example Port",
        "package": "example-port",
        "dumps": [
            {
                "id": "us",
                "label": "USA",
                "cart": "NEXE",
                "revision": 0,
                "sha256": "1" * 64,
                "sha1": "2" * 40,
                "size": 8388608,
                "supported": True,
            }
        ],
    },
}
CONTRACT = Path(__file__).resolve().parents[1] / "src/abu_decomp/port/template"
FIXTURE = Path(__file__).resolve().parent / "fixtures/port/spec-v3.json"


def manifest_dumps(out):
    return json.loads((out / "platform/port/manifest.json").read_text())["dumps"]


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
            "tests/unit/test_shim_link.c",
            "cmake/decomp.cmake",
            "docs/FORMAT.md",
            "ci/format-check",
            "ci/format-pending",
            "ci/check",
            "src/game/g_public.h",
            ".github/workflows/ci.yml",
            "tools/.gitkeep",
            *(f"src/{d}/README.md" for d in ("common", "platform", "renderer", "sound", "input", "install")),
            *(f"packaging/{d}/README.md" for d in ("linux", "windows", "android")),
            *(f"tests/{d}/CMakeLists.txt" for d in ("unit", "iface", "install")),
        ):
            self.assertIn(name, result["files"])
        for path in out.rglob("*"):
            if path.is_file():
                self.assertIsNone(re.search(r"\{\{", path.read_text()), path)
        shim_line = f"abu_shim https://example.invalid/abu-decomp-64.git {PIN_SHIM}"
        self.assertIn(shim_line, (out / "cmake/shim.cmake").read_text())
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
                "dumps": SPEC["install"]["dumps"],
            },
        )
        readme = (out / "README.md").read_text().splitlines()
        self.assertEqual(readme[0], "# example-port")
        self.assertIn("> This is a personal tool. It is not officially maintained", "\n".join(readme))
        self.assertTrue((out / "ci/check").stat().st_mode & 0o100)
        facts = json.loads((out / "port.json").read_text())
        self.assertEqual(facts["format"], 1)
        self.assertEqual(facts["shim"]["pin"], "cmake/shim.cmake")
        self.assertEqual(facts["decomp"]["pin"], "cmake/decomp.cmake")
        self.assertEqual(facts["install"]["dumps"], "platform/port/manifest.json#dumps")
        self.assertEqual(manifest_dumps(out), SPEC["install"]["dumps"])
        self.assertNotIn(PIN_SHIM, (out / "port.json").read_text())
        self.assertIn(f"example-decomp.git {PIN_DECOMP}", (out / "cmake/decomp.cmake").read_text())
        for mode_path in ("ci/check", "ci/format-check"):
            self.assertTrue((out / mode_path).stat().st_mode & 0o100, mode_path)
        self.assertEqual(list(out.glob("LICENSE.*")), [])
        self.assertIn("GNU GENERAL PUBLIC LICENSE", (out / "LICENSE").read_text())
        self.assertFalse((out / "platform/port/test_shim_link.c").exists(), "the shim link test lives in tests/unit")
        self.assertEqual((out / "ci/format-pending").read_text(), "")

    def test_contract_files_are_byte_identical_to_the_package_copies(self):
        generate(self.spec(), self.root / "out")
        for name in ("docs/FORMAT.md", "ci/format-check", "src/game/g_public.h"):
            self.assertEqual((self.root / "out" / name).read_bytes(), (CONTRACT / name).read_bytes(), name)

    def test_fixture_spec_loads_and_generates_a_format_clean_tree(self):
        out = self.root / "fixture"
        generate(load_spec(FIXTURE), out)
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run(["git", "init", "-q", str(out)], check=True)
        subprocess.run([*git, "-C", str(out), "add", "-A"], check=True)
        subprocess.run([*git, "-C", str(out), "commit", "-q", "-m", "x"], check=True)
        done = subprocess.run([str(out / "ci/format-check")], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("format: ok (format 1)", done.stdout)
        done = subprocess.run([str(out / "ci/format-check"), "--strict"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_v2_spec_is_refused_with_the_fix(self):
        old = {k: v for k, v in SPEC.items() if k not in ("format", "install")}
        path = self.root / "old.json"
        path.write_text(json.dumps({**old, "schema_version": 2}))
        with self.assertRaisesRegex(PortError, "add .*install"):
            load_spec(path)

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
            {"format": 2},
            {"install": {**SPEC["install"], "game": "Bad Game"}},
            {"install": {**SPEC["install"], "dumps": []}},
            {"install": {**SPEC["install"], "dumps": [{**SPEC["install"]["dumps"][0], "sha256": "abc"}]}},
            {"install": {**SPEC["install"], "dumps": [{**SPEC["install"]["dumps"][0], "supported": "yes"}]}},
            {"install": {**SPEC["install"], "dumps": SPEC["install"]["dumps"] * 2}},
            {"install": {k: v for k, v in SPEC["install"].items() if k != "package"}},
        ):
            with self.subTest(changes=changes), self.assertRaises(PortError):
                self.spec(**changes)
        (self.root / "busy").mkdir()
        (self.root / "busy" / "file").write_text("")
        with self.assertRaisesRegex(DecompError, "not empty"):
            generate(self.spec(), self.root / "busy")


if __name__ == "__main__":
    unittest.main()
