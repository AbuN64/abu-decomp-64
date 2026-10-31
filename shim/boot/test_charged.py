# SPDX-License-Identifier: GPL-3.0-or-later
"""The generated charged table: original code outside the SDK range, never native code, and the declared work scale."""
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("boot_charged", ROOT / "charged.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ChargedTable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name)
        facts = self.path / "source/versions/test"
        facts.mkdir(parents=True)
        (facts / "symbol_addrs.txt").write_text(
            "alpha = 0x80000010; // type:func\nbeta = 0x80000020; // type:func\nD_data = 0x80000030; //\n"
            "shimmed = 0x80000050; // type:func\nnative_api = 0x80000080; // type:func\n"
            "osLib = 0x802B0000; // type:func\n")
        project = {"version": "test", "sources": [],
                   "bindings": {"shimmed": "host_shim", "game_alias": "alpha"}, "stops": {}}
        (self.path / "input.json").write_text(json.dumps(project))

    def generate(self, scale="4702.010109321735"):
        with patch.object(module, "native_functions", return_value={"host_shim", "native_api"}):
            module.generate(self.path / "input.json", self.path / "source", scale, self.path / "out", self.path / "facts")
        return (self.path / "out/charged_table.c").read_text()

    def test_charged_set_is_original_code_the_port_compiles(self):
        # One tick per entry of an original function, never of the SDK range (osLib), of a bound function (shimmed)
        # or of one the shim defines under its original name (native_api); data symbols are not functions.
        table = self.generate()
        self.assertIn('n64c_charged_names[] = {\n    "alpha",\n    "beta",\n};', table)
        self.assertIn("n64c_charged_count = 2;", table)
        for name in ("shimmed", "native_api", "osLib", "D_data"):
            self.assertNotIn(f'"{name}"', table)

    def test_the_declared_scale_is_compiled_in_verbatim(self):
        self.assertIn('n64c_guest_ns_per_entry[] = "4702.010109321735";', self.generate())

    def test_a_missing_or_unusable_scale_refuses(self):
        for scale in ("", "0", "0.0", "-1", "1e3", "fast", "4702,5"):
            with self.subTest(scale=scale), self.assertRaises(ValueError):
                self.generate(scale)
        self.assertFalse((self.path / "out/charged_table.c").exists())

    def test_nothing_to_charge_refuses(self):
        (self.path / "source/versions/test/symbol_addrs.txt").write_text("osLib = 0x802B0000; // type:func\n")
        with self.assertRaises(ValueError):
            self.generate()


class PortDirArgument(unittest.TestCase):
    """charged.py reads the port's sched_glue.c from the explicit --port-dir: no environment, no inference."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name)
        facts = self.path / "source/versions/test"
        facts.mkdir(parents=True)
        (facts / "symbol_addrs.txt").write_text(
            "alpha = 0x80000010; // type:func\nglue_only = 0x80000020; // type:func\n")
        (self.path / "manifest").mkdir()
        (self.path / "manifest/input.json").write_text(json.dumps({"version": "test", "bindings": {}}))

    def run_cli(self, *port_dir, env=None):
        return subprocess.run(
            [sys.executable, "-I", str(ROOT / "charged.py"), "--manifest", str(self.path / "manifest/input.json"),
             "--source", str(self.path / "source"), "--ns-per-entry", "4702.01", "--output", str(self.path / "out"),
             *port_dir], capture_output=True, text=True, env=env)

    def test_the_given_dir_supplies_sched_glue(self):
        facts = self.path / "facts"
        facts.mkdir()
        (facts / "sched_glue.c").write_text("int glue_only(void) { return 0; }\n")
        result = self.run_cli("--port-dir", str(facts))
        self.assertEqual(result.returncode, 0, result.stderr)
        table = (self.path / "out/charged_table.c").read_text()
        self.assertIn('"alpha"', table)
        self.assertNotIn('"glue_only"', table)  # defined natively by the port's glue: not charged

    def test_a_missing_sched_glue_names_the_full_path(self):
        facts = self.path / "facts"
        facts.mkdir()
        result = self.run_cli("--port-dir", str(facts))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(facts / "sched_glue.c"), result.stderr)
        self.assertFalse((self.path / "out/charged_table.c").exists())

    def test_the_port_dir_is_required_and_the_environment_does_not_stand_in(self):
        facts = self.path / "facts"
        facts.mkdir()
        (facts / "sched_glue.c").write_text("int glue_only(void) { return 0; }\n")
        result = self.run_cli(env={"N64C_PORT_DIR": str(facts), "PATH": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--port-dir", result.stderr)

    def test_the_manifest_directory_is_not_a_port_dir(self):
        (self.path / "manifest/sched_glue.c").write_text("int glue_only(void) { return 0; }\n")
        result = self.run_cli("--port-dir", str(self.path / "facts"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(self.path / "facts/sched_glue.c"), result.stderr)


if __name__ == "__main__":
    unittest.main()
