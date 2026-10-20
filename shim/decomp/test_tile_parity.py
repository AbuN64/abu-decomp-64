#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for tile_parity.py (seconds, no boot): census parsing, verdict, seat drift."""
import json
import pathlib
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True  # the cycle snapshot refuses stray .pyc files

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import tile_parity as tp


class TileParity(unittest.TestCase):
    def test_census_prefers_end_line(self):
        text = "boot-step: census 75/932 hit, 3 watched\nboot-diag: end 4 failures (layout 4), census 252/932, last f\n"
        self.assertEqual(tp.census(text), 252)

    def test_census_falls_back_to_last_step_then_zero(self):
        self.assertEqual(tp.census("boot-step: census 10/932 hit\nboot-step: census 75/932 hit, x\n"), 75)
        self.assertEqual(tp.census("nothing"), 0)

    def test_verdict(self):
        self.assertTrue(tp.verdict(252, 252)["ok"])
        self.assertFalse(tp.verdict(252, 75)["ok"])

    def seat(self, command):
        path = pathlib.Path(tempfile.mkdtemp()) / "seat.json"
        path.write_text(json.dumps({"supervisor": {"roles": {"port": {"command": command}}}}))
        return path

    def test_seat_args_match_and_drift(self):
        same = self.seat(["{release}/rage_decomp_boot", "--window", "{release}/data/ragewars.z64", "284", "222", "640", "480"])
        self.assertEqual(tp.seat_args(same, "x"), tp.TILE_ARGS)
        moved = self.seat(["{release}/rage_decomp_boot", "--window", "{release}/data/ragewars.z64", "284", "222", "800", "600"])
        self.assertNotEqual(tp.seat_args(moved, "x"), tp.TILE_ARGS)


if __name__ == "__main__":
    unittest.main()
