"""Hard cutover: completion, continuation and lifecycle read only RESULT.md."""

import ast
import os
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from abu_decomp.lanes import relaunch


def runner_functions():
    # The runner is a script; compile its real functions without starting app-server.
    source = ast.parse(Path(find_spec("abu_decomp.lanes.runner").origin).read_text())
    names = {"completion", "handoff_only_result", "is_done", "lifecycle_text"}
    functions = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in names]
    module = ast.Module(body=functions, type_ignores=[])
    namespace = {
        "os": os,
        "context_cap": SimpleNamespace(requested=False),
        "resumes": 0,
        "record": mock.Mock(),
        "successor_text": lambda: "successor",
    }
    exec(compile(module, find_spec("abu_decomp.lanes.runner").origin, "exec"), namespace)
    return namespace


class ResultContractTests(unittest.TestCase):
    def setUp(self):
        # Resolve only the module spec: importing runner executes the lane CLI.
        self.namespace = runner_functions()
        self.namespace["a"] = SimpleNamespace(dir="/lane", done_when="result")

    def test_completion_reads_canonical_file_and_ignores_legacy_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.namespace["a"].dir = temporary
            self.assertFalse(self.namespace["completion"]())
            (root / "RESULT").write_text("DONE\nlegacy only")
            self.assertFalse(self.namespace["completion"]())
            (root / "RESULT.md").write_text("")
            self.assertFalse(self.namespace["completion"]())
            (root / "RESULT.md").write_text("DONE\ncanonical")
            self.assertTrue(self.namespace["completion"]())

    def test_continuation_never_falls_back_to_legacy_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy, canonical = root / "RESULT", root / "RESULT.md"
            legacy.write_text("DONE\n")
            self.assertEqual(relaunch.result_text(root), "")
            canonical.write_text("partial")
            self.assertEqual(relaunch.result_text(root), "partial")
            canonical.write_text("")
            self.assertEqual(relaunch.result_text(root), "")
            canonical.unlink()
            legacy.unlink()
            canonical.write_text("DONE\n")
            legacy.symlink_to(canonical.name)
            self.assertEqual(relaunch.result_text(root), "DONE\n")
            canonical.unlink()
            self.assertEqual(relaunch.result_text(root), "")

    def test_identical_rule_reads_only_canonical_result(self):
        self.namespace["a"].done_when = "identical"
        with (
            mock.patch("builtins.open", mock.mock_open(read_data="function identical to reference")) as opened,
            mock.patch("os.path.exists", return_value=True),
        ):
            self.assertTrue(self.namespace["completion"]())
        opened.assert_called_once_with("/lane/RESULT.md")

    def test_handoff_archives_only_canonical_result(self):
        self.namespace["context_cap"].requested = True
        with (
            mock.patch("builtins.open", mock.mock_open(read_data="HANDOFF.md")),
            mock.patch("os.path.exists", return_value=True),
            mock.patch("os.replace") as rename,
        ):
            self.assertFalse(self.namespace["completion"]())
        rename.assert_called_once_with("/lane/RESULT.md", "/lane/HANDOFF-RESULT-1.txt")

    def test_generated_contracts_name_only_result_md(self):
        text = relaunch.DONE_RULE + self.namespace["lifecycle_text"]()
        self.assertIn("writes RESULT.md", text)
        self.assertNotRegex(text, r"RESULT(?!\.md)\b")
