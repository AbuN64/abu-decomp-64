"""Disk floor of the host pool: a new heavy job below the floor is refused before it queues; the owner's play is not."""

import os
import tempfile
import unittest
from unittest import mock

from abu_decomp import slots


class DiskFloorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="slots-disk-")
        for patcher in (
            mock.patch.dict(os.environ, {"ABU_SLOTS_DIR": self.dir, "ABU_SLOT_HELD": "", "ABU_SLOTS_OFF": ""}),
            mock.patch.object(slots, "capacity", return_value=8),
            mock.patch.object(slots, "DISK_FLOOR_GIB", 40.0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def free(self, gib: float):
        return mock.patch.object(slots, "disk_free_gib", return_value=gib)

    def test_configure_reads_the_floor(self) -> None:
        slots.configure({"disk_floor_gib": 55})
        self.assertEqual(slots.DISK_FLOOR_GIB, 55.0)

    def test_refused_below_the_floor_without_queueing(self) -> None:
        with self.free(12.5), self.assertRaises(slots.DiskFloorError) as caught:
            slots.acquire("batch", 1, "stamp build")
        self.assertEqual(caught.exception.key, "slots.disk_floor")
        self.assertIn("12.5 GiB free", caught.exception.detail)
        self.assertIn("ENOSPC", caught.exception.detail)
        self.assertEqual(list((slots.pool_dir() / "tickets").iterdir()), [])  # nothing queued

    def test_hold_refuses_every_class_but_owner(self) -> None:
        with self.free(1.0):
            for klass in ("interactive", "batch", "background"):
                with self.assertRaises(slots.DiskFloorError), slots.hold(klass, 1, "x"):
                    pass
            with slots.hold("owner", 1, "watched play") as grant:
                self.assertIsNotNone(grant)

    def test_admitted_above_the_floor(self) -> None:
        with self.free(60.0), slots.hold("batch", 1, "x") as grant:
            self.assertIsNotNone(grant)

    def test_run_prints_a_refusal_and_does_not_start_the_child(self) -> None:
        with self.free(3.0), mock.patch.object(slots.subprocess, "call") as call, mock.patch("sys.stderr") as err:
            self.assertEqual(slots.main(["run", "--", "true"]), 1)
        call.assert_not_called()
        self.assertIn("REFUSE slots.disk_floor", "".join(c.args[0] for c in err.write.call_args_list))
