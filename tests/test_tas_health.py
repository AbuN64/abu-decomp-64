import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from abu_decomp import tas, tas_health, tas_symbolize


class HealthTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.units = root / "units"
        (self.units / "MENU").mkdir(parents=True)
        (self.units / "MENU" / "a.CPP.failed").write_text("src/MENU/a.CPP:5 #error nope\nmore\n")
        self.project = root / "proj"
        (self.project / "src" / "MENU").mkdir(parents=True)
        (self.project / "include").mkdir()
        (self.project / "src" / "MENU" / "a.CPP").write_text("void func_80411EEC_de(int a)\n{\n}\n")
        self.source = root / "a.CPP"
        self.source.write_text("x")
        self.listing = root / "sources.txt"
        self.listing.write_text(f"{self.source}\n")
        self.port = root / "port"
        self.port.write_text("bin")
        self.cfg = {
            "port": self.port,
            "build": {"units": self.units, "sources": self.listing, "rebuild": "make port"},
        }
        self.profile = SimpleNamespace(source_file=Path("/p/w.json"))
        patch = mock.patch("abu_decomp.tas_triage._project", return_value=self.project)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch("abu_decomp.arity.notes", return_value=[])
        patch.start()
        self.addCleanup(patch.stop)

    def old_binary(self):
        os.utime(self.port, (1, 1))

    def test_failed_unit_is_listed_with_its_first_error(self):
        got = tas_health.read(self.cfg)
        self.assertEqual(got["failed"], [{"unit": "MENU/a.CPP", "error": "src/MENU/a.CPP:5 #error nope"}])
        self.assertEqual(got["summary"], "build: 1 units failed")
        self.assertFalse(got["stale"])

    def test_binary_older_than_a_source_is_stale(self):
        self.old_binary()
        got = tas_health.read(self.cfg)
        self.assertTrue(got["stale"])
        self.assertEqual(got["summary"], "build: 1 units failed; stale: binary older than source")

    def test_unconfigured_build_reads_empty(self):
        self.assertEqual(tas_health.read({"port": self.port}), {})
        self.assertEqual(tas_health.header({}), {})

    def test_a_stop_in_a_failed_unit_is_never_unknown(self):
        health = tas_health.read(self.cfg)
        got = tas_symbolize.claim(self.profile, {"outcome": "stop", "subject": "func_80411EEC_de"}, [], health)
        self.assertEqual(got["kind"], "unit failed to compile")
        self.assertEqual(got["owner"], "func_80411EEC_de at src/MENU/a.CPP:1")
        self.assertIn("src/MENU/a.CPP:5 #error nope", got["next"])
        self.assertIn("build: 1 units failed", got["claim"])

    def test_a_stale_binary_names_the_rebuild_command(self):
        self.old_binary()
        (self.units / "MENU" / "a.CPP.failed").unlink()
        health = tas_health.read(self.cfg)
        got = tas_symbolize.claim(self.profile, {"outcome": "stop", "subject": "func_80999999_de"}, [], health)
        self.assertEqual(got["kind"], "stale binary")
        self.assertEqual(got["next"], "make port")
        self.assertTrue(got["stale"])

    def test_healthy_build_leaves_the_claim_alone(self):
        (self.units / "MENU" / "a.CPP.failed").unlink()
        health = tas_health.read(self.cfg)
        got = tas_symbolize.claim(self.profile, {"outcome": "stop", "subject": "func_80999999_de"}, [], health)
        self.assertEqual(got["kind"], "unknown")
        self.assertNotIn("build", got)


class CompareTests(unittest.TestCase):
    def test_compare(self):
        ok = {"outcome": "ok", "polls_reached": 100}
        stop = {"outcome": "stop", "polls_reached": 50}
        self.assertEqual(tas.compare(None, ok), "new")
        self.assertEqual(tas.compare(ok, ok), "same")
        self.assertEqual(tas.compare(ok, stop), "worse")
        self.assertEqual(tas.compare(stop, ok), "better")
        self.assertEqual(tas.compare(stop, {**stop, "polls_reached": 10}), "worse")
        self.assertEqual(tas.compare(stop, {**stop, "polls_reached": 60}), "better")

    def test_play_all_rows_exit_and_standing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "m").mkdir()
            for name in ("a", "b"):
                (root / "m" / f"{name}.tas").write_text("# tas 1\n")
            cfg = {"movies": root / "m", "runs": root / "r", "build": {}}
            results = {
                "a": {"movie": "a", "outcome": "ok", "polls": 9, "polls_reached": 9},
                "b": {"movie": "b", "outcome": "stop", "subject": "f", "polls": 9, "polls_reached": 3, "next": "n"},
            }
            with (
                mock.patch.object(tas, "settings", return_value=cfg),
                mock.patch.object(tas, "play", side_effect=lambda p, n, **k: results[n]),
            ):
                out, code = tas.play_all(None)
                self.assertEqual((code, [r["vs_recorded"] for r in out["movies"]]), (0, ["new", "new"]))
                results["a"] = {"movie": "a", "outcome": "stop", "subject": "g", "polls": 9, "polls_reached": 4}
                out, code = tas.play_all(None)
                self.assertEqual((code, out["worse"]), (1, ["a"]))
                self.assertEqual(out["movies"][0]["recorded"]["outcome"], "ok")
                self.assertIn("worse", tas.table(out))
