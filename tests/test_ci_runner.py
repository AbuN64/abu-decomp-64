"""The isolated test pool uses workspace storage without relaxing production admission."""

import os
import runpy
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp import slots


class Scratch(unittest.TestCase):
    def test_all_test_pools_share_workspace_storage_and_registered_cleanup(self):
        runner = runpy.run_path(str(Path(__file__).resolve().parents[1] / "ci/run_tests.py"))
        floor = 40.0
        with (
            mock.patch.object(slots, "DISK_FLOOR_GIB", floor),  # the conftest zeroes the floor for unrelated tests
            tempfile.TemporaryDirectory() as workspace,
            mock.patch.dict(os.environ),
            mock.patch.object(tempfile, "tempdir", None),
            mock.patch.object(os, "getcwd", return_value=workspace),
            mock.patch("atexit.register") as register,
        ):
            runner["hermetic_slots"]()
            scratch = Path(tempfile.gettempdir())
            self.addCleanup(shutil.rmtree, scratch, True)
            self.assertEqual(scratch.parent, Path(workspace))
            self.assertEqual(Path(os.environ["TMPDIR"]), scratch)
            self.assertEqual(Path(os.environ["ABU_SLOTS_DIR"]).parent, scratch)
            self.assertEqual(Path(tempfile.mkdtemp(prefix="slots-mem-")).parent, scratch)
            register.assert_called_once_with(shutil.rmtree, str(scratch), ignore_errors=True)
            self.assertEqual(slots.DISK_FLOOR_GIB, floor)
            with (
                mock.patch.object(slots, "disk_free_gib", return_value=29.3),
                self.assertRaises(slots.DiskFloorError),
            ):
                slots.check_disk()


class SourceFirst(unittest.TestCase):
    def test_the_checkout_src_leads_the_import_path_and_the_child_environment(self):
        runner = runpy.run_path(str(Path(__file__).resolve().parents[1] / "ci/run_tests.py"))
        src = str(Path(__file__).resolve().parents[1] / "src")
        with mock.patch.object(sys, "path", ["/installed"]), mock.patch.dict(os.environ, {"PYTHONPATH": "/extra"}):
            runner["source_first"]()
            self.assertEqual(sys.path, [src, "/installed"])
            self.assertEqual(os.environ["PYTHONPATH"], f"{src}{os.pathsep}/extra")
        with mock.patch.dict(os.environ):
            os.environ.pop("PYTHONPATH", None)
            with mock.patch.object(sys, "path", []):
                runner["source_first"]()
            self.assertEqual(os.environ["PYTHONPATH"], src)
