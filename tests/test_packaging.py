"""The installed package ships every file of the source tree, and the tests import the source tree, not a venv copy."""

import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import abu_decomp

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CODE = ".py"


def shipped_names(wheel_dir: Path) -> set[str]:
    (wheel,) = wheel_dir.glob("*.whl")
    with zipfile.ZipFile(wheel) as z:
        return set(z.namelist())


class SourceTree(unittest.TestCase):
    def test_tests_import_the_source_tree(self):
        self.assertEqual(Path(abu_decomp.__file__).resolve().parent, SRC / "abu_decomp")


class Wheel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = Path(tempfile.mkdtemp(prefix="wheel-"))
        tree = cls.scratch / "tree"
        tree.mkdir()
        for name in ("pyproject.toml", "MANIFEST.in"):
            shutil.copy(ROOT / name, tree)
        for name in ("README.md", "LICENSE"):
            shutil.copy(ROOT / name, tree)
        shutil.copytree(SRC, tree / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        out = cls.scratch / "out"
        built = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "--quiet", "-w", str(out), str(tree)],
            capture_output=True,
            text=True,
        )
        if built.returncode:
            raise RuntimeError(f"wheel build failed: {built.stderr[-2000:]}")
        cls.names = shipped_names(out)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.scratch, ignore_errors=True)

    def test_every_template_file_ships(self):
        template = SRC / "abu_decomp" / "port" / "template"
        want = {
            f"abu_decomp/port/template/{p.relative_to(template).as_posix()}" for p in template.rglob("*") if p.is_file()
        }
        self.assertIn("abu_decomp/port/template/.github/workflows/ci.yml", want)
        self.assertEqual(sorted(want - self.names), [])

    def test_every_data_file_ships(self):
        package = SRC / "abu_decomp"
        want = {
            p.relative_to(SRC).as_posix()
            for p in package.rglob("*")
            if p.is_file() and p.suffix not in (CODE, ".pyc") and "__pycache__" not in p.parts
        }
        self.assertIn("abu_decomp/runner.html", want)
        self.assertEqual(sorted(want - self.names), [])


if __name__ == "__main__":
    unittest.main()
