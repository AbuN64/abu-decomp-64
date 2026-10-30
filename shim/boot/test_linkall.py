# SPDX-License-Identifier: GPL-3.0-or-later
"""Autogen's empty data-needs case must compile without inventing game storage."""
import contextlib
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import closure  # noqa: E402
import linkall  # noqa: E402


class AutogenTests(unittest.TestCase):
    def test_zero_needs_compiles_without_defining_symbols(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest = {"entry": "game_entry", "bindings": {}, "stops": {},
                        "data": {"game_entry": 0}, "sources": [], "source_pin": "1234567"}
            (root / "manifest.json").write_text(json.dumps(manifest))
            for name in ("linked-objects.txt", "members.txt", "image-objects.txt", "image.ld", "plan.txt"):
                (root / name).write_text("")
            (root / "image.json").write_text("{}")
            output = root / "effective.json"
            argv = ["--source", str(root), "--manifest", str(root / "manifest.json"),
                    "--boot", str(root), "--objdir", str(root), "--plan", str(root / "plan.txt"),
                    "--members", str(root / "members.txt"), "--output", str(output)]
            with mock.patch.object(closure, "Facts"), mock.patch.object(linkall, "symbols", return_value={}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(linkall.autogen(argv), 0)
            self.assertEqual(json.loads(output.with_suffix(".report.json").read_text())["needs"], [])
            self.assertEqual(json.loads(output.read_text()), manifest)
            obj = root / "autodata.o"
            subprocess.run(["cc", "-std=c99", "-pedantic-errors", "-Werror",
                            "-c", str(root / "autodata.c"), "-o", str(obj)], check=True)
            symbols = subprocess.run(["nm", "--defined-only", str(obj)], check=True, capture_output=True, text=True)
            self.assertEqual(symbols.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
