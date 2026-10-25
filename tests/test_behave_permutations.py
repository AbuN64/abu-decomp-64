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

    def test_a_dereferenced_pointer_is_null_only_on_its_own(self):
        """The original read just behind r4 in every real case: NULL combined with other inputs only faults it."""
        read = {"status": "ok", "reads": [[0x80000040, 4], [0x80000108, 4]]}
        self.results = [dict(read), dict(read)]
        cases, _ = self.generate(budget=200)
        nulls = [c["labels"]["generated"] for c in cases if "ptr=0x0" in c["labels"]["generated"]]
        self.assertEqual(sorted(set(nulls)), ["ptr=0x0"])
        self.assertTrue(any("n=" in c["labels"]["generated"] for c in cases))

    def test_an_unread_pointer_still_pairs_with_null(self):
        cases, _ = self.generate(budget=200)
        self.assertTrue(any("ptr=0x0" in c["labels"]["generated"] and "n=" in c["labels"]["generated"] for c in cases))

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


class UntestableReasonTests(unittest.TestCase):
    def test_reasons_are_counted_with_addresses_blanked_most_frequent_first(self):
        from abu_decomp.behave import run

        rows = [
            {"verdict": "untestable", "reason": f"original unmapped: unmapped 0000000{i} 4 last 803FFE74"}
            for i in range(3)
        ]
        rows += [{"verdict": "untestable", "reason": "original fatal: x"}, {"verdict": "same", "reason": None}]
        got = run.untestable_by_reason(rows, [{"n": i} for i in range(5)])
        self.assertEqual(
            [(g["reason"], g["cases"]) for g in got],
            [("original unmapped: unmapped N N last N", 3), ("original fatal: x", 1)],
        )
        self.assertEqual(got[0]["example"], {"n": 0})


class FiniteFillerTests(unittest.TestCase):
    def test_a_nan_hit_function_gets_ordinary_floats_at_its_swept_offsets(self):
        import random
        import struct

        from abu_decomp.behave import inputs

        data = inputs.fill(random.Random("s"), 64, [(0, 4), (4, 4), (8, 4), (12, 4)], 3, [0x80001000], finite=True)
        words = struct.unpack(">16I", data)
        for w in words[:4]:
            self.assertFalse(inputs.is_nan_word(w))
            self.assertIn(w, inputs._NORMAL_FLOATS + [0x80001000, 0, 1, 0xFFFFFFFF, 0x7FFFFFFF, 0x80000000, 2, 16][:12])
        self.assertTrue(sum(w in inputs._NORMAL_FLOATS for w in words) >= 8)
