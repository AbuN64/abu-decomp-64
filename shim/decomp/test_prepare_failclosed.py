# SPDX-License-Identifier: GPL-3.0-or-later
"""Mock the compiler; exercise prepare's failure boundary and real cache IO."""
import argparse
import contextlib
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

parser = argparse.ArgumentParser()
parser.add_argument("--work", type=pathlib.Path, required=True)
args = parser.parse_args()
args.work.mkdir(parents=True, exist_ok=True)
sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("prepare", ROOT / "prepare.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class FailClosedTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=args.work)
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.source = self.root / "8040B45C_37.CPP"
        self.source.write_text("int actual(void) { return 7; }\n")
        self.files = [self.root / name for name in ("unit37.i", "unit37.stubs.c", "unit37.json", "unit37.d")]
        self.flags = ["-std=gnu89", "-DNON_MATCHING"]
        self.cache = self.root / "cache"
        self.stderr = io.StringIO()

    def run_prepare(self, flags=None):
        with contextlib.redirect_stderr(self.stderr):
            return prepare.prepare(sys.executable, self.source, *self.files,
                                   self.flags if flags is None else flags, cache=self.cache)

    def assert_no_outputs(self):
        self.assertFalse(any(path.exists() for path in self.files))

    def test_main_compile_failure_preserves_all_diagnostics_and_caches_nothing(self):
        diagnostic = "included from header.h:2\nsource:3: error: conflicting types for Texture\nsource:1: note: previous declaration\n"
        argv = ["prepare.py", "--compiler", sys.executable, "--source", str(self.source)]
        for option, path in zip(("output", "stubs", "inventory", "depfile"), self.files):
            argv += ["--" + option, str(path)]
        argv += ["--cache", str(self.cache), "--", *self.flags]
        failed = subprocess.CompletedProcess([], 2, "", diagnostic)
        with mock.patch.object(prepare.subprocess, "run", return_value=failed) as compiler:
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stderr(self.stderr):
                self.assertEqual(prepare.main(), 2)
        compiler.assert_called_once()
        command = compiler.call_args.args[0]
        self.assertIn("-DNON_MATCHING", command)
        self.assertIn("-fsyntax-only", command)
        self.assertNotIn("-E", command)
        self.assertIn("unit " + self.source.name, self.stderr.getvalue())
        self.assertIn(str(self.source), self.stderr.getvalue())
        self.assertIn(diagnostic, self.stderr.getvalue())
        self.assert_no_outputs()
        self.assertEqual(list(self.cache.rglob("*")), [])

    def seed_cache(self, inventory):
        cache = prepare.Cache(self.cache, sys.executable, self.source, self.flags, ())
        cache.store({"i": "degraded unit", "stubs": "degraded stops", "inventory": inventory,
                     "stderr": "old fallback warning\n", "deps": [[str(self.source), ""]]}, [str(self.source)])
        return {p: p.read_bytes() for p in self.cache.rglob("*") if p.is_file()}

    def test_poisoned_cache_is_refused_without_replay_recompile_or_rewrite(self):
        cases = [
            {"mode": "matched", "non_matching_errors": ["conflicting types for Texture"]},
            {"mode": "matched", "non_matching_errors": []},
            {"mode": "NON_MATCHING", "non_matching_errors": ["rejected body"]},
            {"non_matching_errors": []},
        ]
        for record in cases:
            with self.subTest(inventory=record):
                before = self.seed_cache(record)
                self.stderr = io.StringIO()
                with mock.patch.object(prepare.subprocess, "run") as compiler:
                    self.assertNotEqual(self.run_prepare(), 0)
                compiler.assert_not_called()
                self.assertIn("refusing cached fallback", self.stderr.getvalue())
                self.assertIn("unit " + self.source.name, self.stderr.getvalue())
                self.assertIn(str(self.source), self.stderr.getvalue())
                self.assertNotIn("old fallback warning", self.stderr.getvalue())
                self.assert_no_outputs()
                self.assertEqual(before, {p: p.read_bytes() for p in self.cache.rglob("*") if p.is_file()})

    def preprocessing(self, command, **kwargs):
        if "-fsyntax-only" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        self.assertIn("-E", command)
        self.files[3].write_text(f"{self.files[0]}: {self.source}\n")
        return subprocess.CompletedProcess(command, 0,
            'extern void n64c_missing_marker_absent(void);\nint actual(void) { return 7; }\n', "")

    def test_success_and_cache_hit_preserve_outputs_flags_inventory_and_dependencies(self):
        with mock.patch.object(prepare.subprocess, "run", side_effect=self.preprocessing) as compiler:
            self.assertEqual(self.run_prepare(), 0)
        self.assertEqual(compiler.call_count, 2)
        for call in compiler.call_args_list:
            self.assertIn("-DNON_MATCHING", call.args[0])
        self.assertEqual(json.loads(self.files[2].read_text()), {
            "missing": ["absent"], "data": [], "mode": "NON_MATCHING",
            "non_matching_errors": [], "source": str(self.source)})
        self.assertIn('return n64c_boot_missing("absent");', self.files[1].read_text())
        self.assertIn("int actual(void) { return 7; }", self.files[0].read_text())
        expected = [path.read_bytes() for path in self.files]
        self.assertEqual(len(list(self.cache.rglob("*.json"))), 1)
        self.assertEqual(len(list(self.cache.rglob("*.z"))), 1)
        for path in self.files:
            path.unlink()
        with mock.patch.object(prepare.subprocess, "run") as compiler:
            self.assertEqual(self.run_prepare(), 0)
        compiler.assert_not_called()
        self.assertEqual([path.read_bytes() for path in self.files], expected)
        self.assertEqual(self.stderr.getvalue(), "")

    def test_explicit_matched_flags_remain_matched_on_success_and_cache_hit(self):
        flags = ["-std=gnu89"]
        with mock.patch.object(prepare.subprocess, "run", side_effect=self.preprocessing) as compiler:
            self.assertEqual(self.run_prepare(flags), 0)
        compiler.assert_called_once()
        self.assertNotIn("-DNON_MATCHING", compiler.call_args.args[0])
        self.assertEqual(json.loads(self.files[2].read_text())["mode"], "matched")
        with mock.patch.object(prepare.subprocess, "run") as compiler:
            self.assertEqual(self.run_prepare(flags), 0)
        compiler.assert_not_called()

    def test_preprocessing_failure_caches_nothing(self):
        results = [subprocess.CompletedProcess([], 0, "", ""),
                   subprocess.CompletedProcess([], 1, "", "source: error: preprocessing failed\n")]
        with mock.patch.object(prepare.subprocess, "run", side_effect=results) as compiler:
            self.assertEqual(self.run_prepare(), 1)
        self.assertEqual(compiler.call_count, 2)
        self.assertIn("source: error: preprocessing failed", self.stderr.getvalue())
        self.assertIn(str(self.source), self.stderr.getvalue())
        self.assert_no_outputs()
        self.assertEqual(list(self.cache.rglob("*")), [])


unittest.main(argv=["test_prepare_failclosed"], verbosity=2)
