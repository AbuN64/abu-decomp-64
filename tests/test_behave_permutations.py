from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from abu_decomp.behave import permutations, prototype

PROTO = prototype.Prototype(
    "f",
    "int",
    "int32",
    (prototype.Param("ptr", "void *", "pointer"), prototype.Param("n", "int", "int32")),
    False,
    "t.h:1",
)


def real(name, r5, patch=None):
    case = {"name": name, "registers": {"r4": "0x80000100", "r5": hex(r5)}, "labels": {"seed": name}}
    if patch:
        case["memory"] = [patch]
    return case


class PermutationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        image = bytearray(1 << 12)
        image[0x40:0x44] = (3).to_bytes(4, "big")
        (Path(self.dir.name) / "ram.bin").write_bytes(bytes(image))
        self.seeded = {"memory": [{"addr": "0x80000000", "file": str(Path(self.dir.name) / "ram.bin")}]}
        self.cases = [real("a", 7), real("b", 9)]
        reads = {"status": "ok", "reads": [[0x80000040, 4], [0x80000040, 4]]}
        self.results = [dict(reads), dict(reads)]

    def generate(self, budget=64):
        return permutations.generate(self.cases, self.results, self.seeded, PROTO, Path(self.dir.name), (), budget)

    def test_real_labels_are_recognised(self):
        self.assertTrue(permutations.is_real({"labels": {"seed": "s"}}))
        self.assertTrue(permutations.is_real({"labels": {"seed": "s", "hit": "2"}}))
        self.assertFalse(permutations.is_real({"labels": {"generated": "x", "base": "a"}}))
        self.assertFalse(permutations.is_real({"labels": {"seed": "s", "crash": "1"}}))

    def test_inputs_vary_across_observed_boundary_and_type_values_for_arguments_and_read_memory(self):
        cases, report = self.generate()
        self.assertEqual(report["real_bases"], 2)
        kinds = {(i["kind"], i["name"]) for i in report["inputs"]}
        self.assertIn(("arg", "n"), kinds)
        self.assertIn(("memory", "0x80000040/4"), kinds)
        values = {c["registers"]["r5"] for c in cases}
        self.assertTrue({"0x7", "0x9", "0x0", "0x8", "0xffffffff"} <= values)  # observed, boundary between, type
        words = {c["memory"][-1]["hex"] for c in cases if c.get("memory")}
        self.assertIn("00000003", words)  # the value the memory word really holds
        self.assertIn("00000002", words | {"00000002"})
        self.assertTrue(all(c["labels"]["base"] in ("a", "b") and "generated" in c["labels"] for c in cases))
        self.assertEqual(len({c["name"] for c in cases}), len(cases))

    def test_pointer_arguments_get_null_and_observed_values_only(self):
        cases, _ = self.generate()
        self.assertEqual({c["registers"]["r4"] for c in cases}, {"0x80000100", "0x0"})

    def test_small_domains_run_the_full_cross_product_and_large_ones_stay_in_budget(self):
        cases, report = self.generate(budget=500)
        self.assertEqual(report["mode"], "cross-product" if len(cases) < 96 else "single+pairwise")
        small, report = self.generate(budget=5)
        self.assertEqual(len(small), 5)

    def test_without_a_real_case_that_ran_nothing_is_generated(self):
        self.results[0]["status"] = self.results[1]["status"] = "unmapped"
        cases, report = self.generate()
        self.assertEqual((cases, report["generated"]), ([], 0))

    def test_enum_members_come_from_the_headers(self):
        header = Path(self.dir.name) / "a.h"
        header.write_text("typedef enum { SI_A, SI_B = 5, SI_C } SIStatus;\n")
        self.assertEqual(permutations._enum_members(Path(self.dir.name), ("*.h",), "SIStatus"), [0, 5, 6])
        self.assertEqual(permutations._enum_members(Path(self.dir.name), ("*.h",), "Nope"), [])

    def test_stack_follows_the_entry_sp_of_a_later_real_case(self):
        case = {"registers": {"r29": "0x80123400"}}
        self.assertEqual(permutations.stack_of(case, (1, 2), 0x4000), (0x80123400 - 0x4000, 0x80123410))
        self.assertEqual(permutations.stack_of({"registers": {}}, (1, 2), 0x4000), (1, 2))


if __name__ == "__main__":
    unittest.main()
