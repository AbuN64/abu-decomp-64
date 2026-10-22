"""Header-only rounds proved by object identity (objproof; fake make, real files). The land stage: test_land."""

import os
import tempfile
import time
import unittest
from pathlib import Path

from abu_decomp import objproof

STAMP = "build/v1/src/a.CPP.i.ok"
PLAIN = "build/v1/src/b.CPP.i.ok"


class ObjectProofTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tree = Path(self.temp.name)
        self.calls: list[list[str]] = []
        self.new_i = {"a": "int x;\n", "b": "int y;\n"}  # what preprocessing writes
        self.new_s = {"a": "same\n", "b": "same\n"}  # what compiling writes
        for name in ("a", "b"):
            self.put(f"build/v1/src/{name}.CPP.d", f"build/v1/src/{name}.CPP.i.ok: \\\n src/{name}.CPP include/T.H\n")
            self.put(f"build/v1/src/{name}.CPP.i", self.new_i[name])
            self.put(f"build/v1/src/{name}.CPP.s", "same\n")
        self.put("build/v1/RageWars.z64", "rom")
        old = time.time() - 100
        for path in (self.tree / "build/v1/src").iterdir():
            os.utime(path, (old, old))

    def put(self, name: str, text: str) -> None:
        path = self.tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def make(self, targets: list[str]) -> tuple[int, str]:
        self.calls.append(targets)
        for target in targets:
            name = Path(target).name.split(".")[0]
            if target.endswith(".i.ok"):
                self.put(target[: -len(".ok")], self.new_i[name])
            else:
                self.put(target[: -len(".o")] + ".s", self.new_s[name])
        return 0, ""

    def test_unchanged_text_proves_every_object_with_one_preprocess_pass_and_no_compile(self) -> None:
        found = objproof.prove(self.tree, ["include/T.H"], self.make)
        self.assertTrue(found["ok"])
        self.assertEqual((found["units"], found["recompiled"]), (2, 0))
        self.assertEqual(self.calls, [["build/v1/src/a.CPP.i.ok", "build/v1/src/b.CPP.i.ok"]])

    def test_a_moved_text_that_compiles_to_the_same_output_is_still_identical(self) -> None:
        self.new_i["a"] = "int  x;\n"
        found = objproof.prove(self.tree, ["include/T.H"], self.make)
        self.assertTrue(found["ok"])
        self.assertEqual(found["recompiled"], 1)
        self.assertEqual(self.calls[1], ["build/v1/src/a.CPP.o"])

    def test_a_changed_compiler_output_refuses_and_names_the_unit(self) -> None:
        self.new_i["b"] = "long y;\n"
        self.new_s["b"] = "different\n"
        found = objproof.prove(self.tree, ["include/T.H"], self.make)
        self.assertFalse(found["ok"])
        self.assertIn("build/v1/src/b.CPP", found["reasons"][0])

    def test_only_headers_that_a_unit_reads_matter(self) -> None:
        found = objproof.prove(self.tree, ["include/Other.H"], self.make)
        self.assertEqual((found["ok"], found["units"], self.calls), (True, 0, []))

    def test_a_source_file_is_not_a_header_round(self) -> None:
        found = objproof.prove(self.tree, ["include/T.H", "src/a.CPP"], self.make)
        self.assertFalse(found["ok"])
        self.assertEqual(self.calls, [])

    def test_a_unit_newer_than_the_rom_leaves_nothing_to_compare_with(self) -> None:
        self.put("build/v1/src/a.CPP.i", "int x;\n")  # written after the ROM was linked
        past = time.time() - 50
        os.utime(self.tree / "build/v1/RageWars.z64", (past, past))
        found = objproof.prove(self.tree, ["include/T.H"], self.make)
        self.assertFalse(found["ok"])
        self.assertEqual(self.calls, [])

    def test_a_failed_make_refuses(self) -> None:
        found = objproof.prove(self.tree, ["include/T.H"], lambda targets: (2, "boom"))
        self.assertFalse(found["ok"])
        self.assertIn("boom", found["reasons"][0])

    def test_no_build_state_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            self.assertFalse(objproof.prove(Path(empty), ["include/T.H"], self.make)["ok"])


if __name__ == "__main__":
    unittest.main()
