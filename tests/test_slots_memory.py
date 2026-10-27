"""Memory admission of the host pool: a stamp needs the floor plus its measured need, the rest queue, none fail."""

import os
import tempfile
import unittest
from unittest import mock

from abu_decomp import slots


class StampMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="slots-mem-")
        for patcher in (
            mock.patch.dict(os.environ, {"ABU_SLOTS_DIR": self.dir, "ABU_SLOT_HELD": "", "ABU_SLOTS_OFF": ""}),
            mock.patch.object(slots, "capacity", return_value=8),
            mock.patch.object(slots, "MEM_FLOOR_GIB", 4.0),
            mock.patch.object(slots, "STAMP_GIB", 5.0),
            mock.patch.object(slots, "STAMP_JOBS", 2),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def mem(self, total: float, available: float):
        return mock.patch.object(slots, "_mem_gib", side_effect=lambda f: total if f == "MemTotal" else available)

    def test_configure_reads_the_profile_section(self) -> None:
        slots.configure({"floor_gib": 3, "stamp_gib": 6.5, "stamp_jobs": 4, "reserve_cores": 1})
        self.assertEqual((slots.MEM_FLOOR_GIB, slots.STAMP_GIB, slots.STAMP_JOBS), (3.0, 6.5, 4))
        slots.configure(None)  # no section: keeps what was set
        self.assertEqual(slots.STAMP_GIB, 6.5)

    def test_stamp_slots_is_the_smaller_of_cores_and_memory(self) -> None:
        with self.mem(34.0, 30.0):  # (34 - 4) / 5 = 6 by memory, 8 / 2 = 4 by cores
            self.assertEqual(slots.stamp_slots(), 4)
        with self.mem(19.0, 15.0):  # (19 - 4) / 5 = 3 by memory
            self.assertEqual(slots.stamp_slots(), 3)
            self.assertEqual(slots.stamp_weight(), 2)  # 8 // 3
        with self.mem(2.0, 1.0):  # less than one stamp: still one
            self.assertEqual(slots.stamp_slots(), 1)

    def test_an_interactive_stamp_takes_the_whole_capacity_and_batch_stamps_share(self) -> None:
        with self.mem(34.0, 30.0):  # 4 stamps fit by cores: stamp_jobs 2 caps batch only
            self.assertEqual(slots.stamp_weight("batch"), 2)
            self.assertEqual(slots.stamp_weight("background"), 2)
            self.assertEqual(slots.stamp_weight("interactive"), 8)
            self.assertEqual(slots.stamp_weight("owner"), 8)

    def test_a_stamp_waits_for_floor_plus_its_need_then_runs(self) -> None:
        with self.mem(34.0, 8.0), self.assertRaises(TimeoutError):  # 8 < 4 + 5 + nothing promised? 8 < 9
            slots.acquire("batch", 2, "stamp", timeout=0.2, mem_gib=5.0)
        with self.mem(34.0, 9.5):
            grant = slots.acquire("batch", 2, "stamp", timeout=2, mem_gib=5.0)
            grant.release()

    def test_a_fresh_holder_promises_memory_it_has_not_used_yet(self) -> None:
        with self.mem(34.0, 14.0):  # room for two stamps by the figure alone: 4 + 5 <= 14 < 4 + 5 + 5 + 5
            first = slots.acquire("batch", 2, "stamp one", timeout=2, mem_gib=5.0)
            try:
                with self.assertRaises(TimeoutError):  # needs 4 + 5 + 5 promised = 14 + > 14? 14 < 14 is false
                    slots.acquire("batch", 2, "stamp two", timeout=0.2, mem_gib=5.5)
            finally:
                first.release()

    def test_the_default_need_is_per_slot_rss_without_a_stamp_figure(self) -> None:
        with self.mem(34.0, 5.0), self.assertRaises(TimeoutError):  # 4 + 0.75 * 4 = 7 > 5
            slots.acquire("batch", 4, "play", timeout=0.2)


if __name__ == "__main__":
    unittest.main()
