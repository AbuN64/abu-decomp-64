"""Ledger grammar and fail-closed oracle verdicts; no game processes."""

import tempfile
import unittest
from pathlib import Path

from abu_decomp import tas, tas_oracle, tas_reviews


class ReviewTests(unittest.TestCase):
    def test_range_requires_the_whole_symbol(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lockstep-ignore.tsv"
            path.write_text("# measured reviews\n0x80000300-0x80000400\tNative boot globals\n")
            ledger = tas_reviews.read(path)
            self.assertEqual(tas_reviews.cause(ledger, "boot", 0x80000300, 4), "Native boot globals")
            self.assertIsNone(tas_reviews.cause(ledger, "boot", 0x800003FF, 4))

    def test_invalid_rows_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lockstep-ignore.tsv"
            for text in (
                "thing",
                "thing\t",
                "*\tblanket",
                "thing\tcause\nthing\tduplicate",
                "0x20-0x10\tcause",
                "0x10-0x30\tcause\n0x20-0x40\toverlap",
            ):
                with self.subTest(text=text):
                    path.write_text(text)
                    with self.assertRaises(ValueError):
                        tas_reviews.read(path)

    def test_missing_ledger_reviews_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(tas_reviews.read(Path(directory) / "absent"), ({}, []))

    def test_oracle_verdict_requires_a_real_zero_unreviewed_comparison(self):
        record = {
            "outcome": "ok",
            "oracle": {"outcome": "ok"},
            "polls": 728,
            "state_diff": {
                "polls_compared": 728,
                "divergent": 0,
                "reviewed_symbols": 46,
                "coverage": {"complete": True, "port_last": 728, "oracle_last": 728},
            },
        }
        self.assertEqual(tas_oracle.verdict(record), "ok")
        for state in (
            {"polls_compared": 728, "divergent": 1},
            {"polls_compared": 0, "divergent": 0},
            {"error": "malformed ledger"},
            {},
        ):
            self.assertEqual(tas_oracle.verdict({**record, "state_diff": state}), "fail")
        self.assertEqual(tas_oracle.verdict({**record, "oracle": {"outcome": "crash"}}), "fail")

    def test_batch_rejects_unreviewed_state_even_without_a_standing(self):
        row = {"outcome": "ok", "polls_reached": 728, "oracle": {"verdict": "fail"}}
        self.assertEqual(tas.compare(None, row), "worse")
        self.assertEqual(tas.compare({"outcome": "ok", "polls_reached": 728}, row), "worse")
