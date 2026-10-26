"""Recorded observations survive claims and bake as the Runner's actual case-row contract."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from abu_decomp import claims, tas_bake
from abu_decomp.behave import prototype


def signature(declaration="s32 f(s32 x)"):
    return prototype.parse_declaration(declaration + ";", "f").describe()


def case(value=1, verdict="same", ret=7):
    return {
        "name": f"c{value}",
        "verdict": verdict,
        "input": {"registers": {"a0": value}, "stack": []},
        "original": {"status": "ok", "ret": {"v0": ret, "v1": 0, "f0": 0}},
        "candidate": {"status": "ok", "ret": {"v0": ret, "v1": 0, "f0": 0}},
    }


class Recording(unittest.TestCase):
    def test_samples_include_matching_cases_after_many_differences(self):
        rows = [case(i, "differs") for i in range(30)] + [case(100 + i) for i in range(30)]
        document = {"prototype": signature(), "cases_detail": rows, "verdict": "differs"}
        before = copy.deepcopy(document)
        found = claims.behave_detail(document)
        self.assertGreaterEqual(sum(c["verdict"] == "same" for c in found["cases_detail"]), 3)
        self.assertGreaterEqual(sum(c["verdict"] == "differs" for c in found["cases_detail"]), 3)
        self.assertEqual(found["prototype"], signature())
        self.assertEqual(found, claims.behave_detail(document))
        self.assertEqual(document, before)
        self.assertFalse(claims.outcome("behave", True, document)["passed"])

    def test_budget_counts_serialized_bytes_and_skips_oversized_cases(self):
        oversized = dict(case(), reason="雪" * 9000)
        rows = [oversized] + [dict(case(i), reason="é雪" * 25) for i in range(30)]
        found = claims.behave_detail({"cases_detail": rows})["cases_detail"]
        self.assertGreaterEqual(len(found), 3)
        self.assertNotIn(oversized, found)
        self.assertLessEqual(len(json.dumps(found).encode()), claims.DETAIL_BYTES)

    def test_describe_is_retained_without_becoming_a_comparison(self):
        row = dict(case(), verdict="described", candidate=None)
        found = claims.outcome("behave", True, {"mode": "describe", "verdict": "described", "cases_detail": [row]})
        self.assertEqual(found["cases_detail"], [row])
        self.assertFalse(found["passed"])
        self.assertFalse(found["benign"])


class Decoding(unittest.TestCase):
    def test_signed_values_and_matching_returns(self):
        row = tas_bake.decode_case("f", signature(), case(0xFFFFFFFF, ret=0xFFFFFFFE))
        self.assertEqual(row["text"], "f(a0=-1) -> v0=-2")
        self.assertIs(row["port"], True)
        self.assertIs(row["oracle"], True)
        self.assertTrue(row["signature_known"])

    def test_native_capture_register_names_and_missing_registers(self):
        raw = case()
        raw["input"]["registers"] = {"r4": "0xffffffff"}
        self.assertIn("a0=-1", tas_bake.decode_case("f", signature(), raw)["text"])
        raw["input"]["registers"] = {}
        self.assertIn("a0=?", tas_bake.decode_case("f", signature(), raw)["text"])

    def test_float_double_and_aligned_stack_slots(self):
        raw = case()
        raw["input"] = {
            "registers": {"f12": 0x3FC00000, "f14": 0x4004000000000000},
            "stack": [0x80000001, 0, 0xFFFFFFFF, 0xFFFFFFFE],
        }
        raw["original"]["ret"]["f0"] = raw["candidate"]["ret"]["f0"] = 0xBFC00000
        row = tas_bake.decode_case("f", signature("f32 f(f32 x, f64 y, void *p, s64 z)"), raw)
        self.assertEqual(row["text"], "f(f12=1.5, f14=2.5, stack:16=0x80000001, stack:24:stack:28=-2) -> f0=-1.5")

    def test_wide_integer_pair_and_unsigned_return(self):
        raw = case()
        raw["input"]["registers"] = {"a0": 9, "a2": 0xFFFFFFFF, "a3": 0xFFFFFFFE}
        for side in ("original", "candidate"):
            raw[side]["ret"].update(v0=0xFFFFFFFF, v1=0xFFFFFFFF)
        row = tas_bake.decode_case("f", signature("u64 f(u8 first, s64 pair)"), raw)
        self.assertEqual(row["text"], "f(a0=9, a2:a3=-2) -> v0:v1=18446744073709551615")

    def test_narrow_types_are_masked_before_sign_extension(self):
        raw = case(0xFFFFFF80, ret=0xFFFF00FF)
        row = tas_bake.decode_case("f", signature("u8 f(s8 x)"), raw)
        self.assertEqual(row["text"], "f(a0=-128) -> v0=255")

    def test_double_return_and_float_after_integer_uses_gpr_bits(self):
        raw = case()
        raw["input"]["registers"] = {"a0": 1, "a1": 0x80000000}
        for side in ("original", "candidate"):
            raw[side]["ret"]["f0"] = 0x4004000000000000
        row = tas_bake.decode_case("f", signature("f64 f(s32 n, f32 x)"), raw)
        self.assertEqual(row["text"], "f(a0=1, a1=-0.0) -> f0=2.5")

    def test_describe_marks_only_observed_original(self):
        raw = dict(case(), verdict="described", candidate=None)
        row = tas_bake.decode_case("f", signature(), raw)
        self.assertIsNone(row["port"])
        self.assertTrue(row["oracle"])
        self.assertEqual(row["text"], "f(a0=1) -> original v0=7; port not run")

    def test_difference_marks_port_false_even_when_returns_match(self):
        raw = dict(case(verdict="differs"), difference={"kind": "store"}, reason="different memory effect")
        row = tas_bake.decode_case("f", signature(), raw)
        self.assertFalse(row["port"])
        self.assertTrue(row["oracle"])
        self.assertEqual(row["difference"], {"kind": "store"})
        self.assertEqual(row["reason"], "different memory effect")

    def test_different_returns_show_both_values(self):
        raw = case(verdict="differs")
        raw["candidate"]["ret"]["v0"] = 8
        row = tas_bake.decode_case("f", signature(), raw)
        self.assertEqual(row["text"], "f(a0=1) -> original v0=7; port v0=8")

    def test_failure_never_shows_an_unreturned_register_as_a_result(self):
        raw = case(verdict="untestable")
        raw["original"]["status"] = "fatal"
        row = tas_bake.decode_case("f", signature(), raw)
        self.assertFalse(row["oracle"])
        self.assertIsNone(row["port"])
        self.assertIsNone(row["outputs"]["oracle"]["return"])
        self.assertIn("original fatal", row["text"])

    def test_missing_prototype_is_explicitly_unknown_not_void(self):
        for proto in (None, prototype.inferred("f").describe()):
            row = tas_bake.decode_case("f", proto, case())
            self.assertFalse(row["signature_known"])
            self.assertIn("v0=0x00000007", row["text"])
            self.assertIn("a1=?", row["text"])

    def test_void_has_no_manufactured_return_value(self):
        row = tas_bake.decode_case("f", signature("void f(void)"), case())
        self.assertEqual(row["text"], "f() -> void")
        self.assertEqual(row["arguments"], [])

    def test_old_difference_only_records_do_not_fabricate_io_rows(self):
        self.assertIsNone(tas_bake.decode_case("f", signature(), {"verdict": "differs", "inputs": {"x": "zero"}}))

    def test_invalid_register_is_unknown_not_zero(self):
        row = tas_bake.decode_case("f", signature(), case("bad hex"))
        self.assertIn("a0=?", row["text"])


class Pipeline(unittest.TestCase):
    def test_recorder_to_actual_fn_json_contract_and_repeat_folding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = {
                "function": "f",
                "mode": "compare",
                "verdict": "same",
                "prototype": signature(),
                "cases_detail": [case(1), case(1), case(2), case(3)],
            }
            with mock.patch.object(claims, "state_dir", return_value=root):
                claims.record(None, "a" * 32, "behave", True, record, ["behave", "--function", "f"])
            inputs = SimpleNamespace(invocations=[str(root)], table={}, claims=None)
            data = tas_bake.fn_files(
                inputs, ["src/f.c"], [["f", 0, 12, "exact", 0, 1, False]], {"source": {}, "gutter": {}, "xref": {}}
            )
            rows = json.loads(data["f"])["behave"]
            self.assertEqual(len(rows), 3)
            self.assertEqual([r["count"] for r in rows], [2, 1, 1])
            self.assertEqual(rows[0]["text"], "f(a0=1) -> v0=7")
            self.assertTrue(all(r["port"] and r["oracle"] for r in rows))
            self.assertTrue(all(r["invocation"] == "a" * 32 for r in rows))
            self.assertNotIn("cases", rows[0])  # hard cutover: no run envelopes for the caseRow renderer
            self.assertEqual(
                data,
                tas_bake.fn_files(
                    inputs, ["src/f.c"], [["f", 0, 12, "exact", 0, 1, False]], {"source": {}, "gutter": {}, "xref": {}}
                ),
            )

    def test_run_and_case_bounds_are_preserved(self):
        record = {"prototype": signature(), "cases_detail": [case(i) for i in range(30)]}
        with mock.patch.object(tas_bake.tas_fn, "behave_index", return_value={"f": [record] * 20, "g": [record]}):
            rows = tas_bake.behave_rows(SimpleNamespace(invocations=[]), {"f"})
        self.assertEqual(set(rows), {"f"})
        self.assertEqual(len(rows["f"]), tas_bake.MAX_BEHAVE * tas_bake.MAX_CASES)
