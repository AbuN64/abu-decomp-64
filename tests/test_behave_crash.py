import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp.behave import crash, inputs, prototype


def proto(*types):
    return prototype.Prototype(
        "Draw",
        "void",
        "void",
        tuple(prototype.Param(f"p{i}", ctype, kind) for i, (ctype, kind) in enumerate(types)),
        False,
        "decl.h:1",
    )


class CrashCases(unittest.TestCase):
    def test_native_words_become_o32_float_double_and_stack_arguments(self):
        signature = proto(("float", "float"), ("double", "double"), ("int", "int32"))
        baseline = inputs.plan(signature, 1, 0, inputs.Layout())[0][0]
        before = copy.deepcopy(baseline)
        record = {"words": [0x3F000000, 0, 0x40040000, 7], "movie": "crash", "at": "fault"}
        entry, evidence = crash.case(signature, baseline, record)
        self.assertEqual(entry["registers"]["f12"], 0x3F000000)
        self.assertEqual(entry["registers"]["f14"], 0x4004000000000000)
        self.assertEqual(entry["stack"], [7])
        self.assertEqual(evidence["arguments"], ["p0", "p1", "p2"])
        self.assertEqual(baseline, before)
        self.assertTrue(all("crash" in c["labels"] for c in inputs.around(entry, signature, 4)))

    def test_nonnull_host_pointers_keep_synthetic_memory_and_are_named(self):
        signature = proto(("int*", "pointer"), ("int", "int32"))
        baseline = inputs.plan(signature, 1, 0, inputs.Layout())[0][0]
        entry, evidence = crash.case(signature, baseline, {"words": [0x08100000, 0], "movie": "zero-height"})
        self.assertEqual(entry["registers"]["a0"], baseline["registers"]["a0"])
        self.assertEqual(entry["registers"]["a1"], 0)
        self.assertEqual(evidence["synthetic_pointers"], ["p0"])
        self.assertIn("synthetic", evidence["memory"])
        self.assertEqual(len(inputs.around(entry, signature, 2)), 2)

    def test_incomplete_prototype_and_short_capture_refuse_instead_of_guessing(self):
        signature = proto(("int", "int32"))
        self.assertIsNone(crash.case(prototype.inferred("Draw"), {}, {"words": [1]})[0])
        self.assertIsNone(crash.case(signature, {}, {"words": []})[0])

    def test_latest_matching_crash_is_selected_without_claiming_oracle_reach(self):
        with tempfile.TemporaryDirectory() as folder:
            rows = [
                (
                    Path(folder),
                    {
                        "outcome": "crash",
                        "movie": "crash",
                        "polls_reached": 42,
                        "fault": {"arguments": {"function": "Draw", "abi": "i386-cdecl", "words": [0]}},
                    },
                ),
                (Path(folder), {"outcome": "ok", "movie": "later"}),
            ]
            with (
                mock.patch.object(crash.tas, "settings", return_value={"runs": Path(folder)}),
                mock.patch.object(crash.tas, "run_rows", return_value=rows),
            ):
                record, why = crash.arguments(None, "Draw")
                self.assertEqual((record["movie"], record["poll"]), ("crash", 42))
                self.assertIn("crash argument slots", why)
                self.assertIsNone(crash.arguments(None, "Another")[0])
