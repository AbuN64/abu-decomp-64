"""A holder is counted for what it can burn: the thread and its children are pinned to the cores of the slots held.
Regression slice (ragewars 10-27 19:4xZ): a weight-2 `tas play` whose two n64host processes ran at 294% each drove
busy_cores to 11/12 with 2 of 10 slots used, so critical-path batch stamps waited on the CPU gate."""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from abu_decomp import slots

HOST_CORES = 12
CAPACITY = 10
N64HOST = [2.94, 2.94]  # cores each n64host process burns when nothing confines it
OUTSIDE = 11.0 - sum(N64HOST)  # the rest of the host's measured busy cores in the slice


class BurnTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="slots-")
        env = mock.patch.dict(os.environ, {"ABU_SLOTS_DIR": self.dir, "ABU_SLOT_HELD": "", "ABU_SLOTS_OFF": ""})
        env.start()
        self.addCleanup(env.stop)
        self.pinned: list[set[int]] = []  # what the pool pinned the holder to; empty: nothing confines the tree
        for name, fake in (
            ("cores", lambda: HOST_CORES),
            ("capacity", lambda: CAPACITY),
            ("_mem_gib", lambda field: 50.0),
            ("_fresh_holder", lambda root: False),
            ("busy_cores", self.busy),
            ("_pin", self.pin),
        ):
            patch = mock.patch.object(slots, name, side_effect=fake)
            patch.start()
            self.addCleanup(patch.stop)
        restore = mock.patch.object(os, "sched_setaffinity")
        restore.start()
        self.addCleanup(restore.stop)

    def pin(self, cpus: set[int]) -> set[int]:
        self.pinned.append(set(cpus))
        return set(range(HOST_CORES))

    def busy(self) -> float:
        """Host busy cores: the outside load plus what the holder's processes can burn (their demand, within the cores
        they may run on)."""
        allowed = len(self.pinned[-1]) if self.pinned else HOST_CORES
        return OUTSIDE + min(sum(N64HOST), allowed)

    def test_a_weight_2_play_burns_its_2_cores_and_a_batch_stamp_is_admitted(self) -> None:
        with slots.hold("background", 2, "play tas"):
            self.assertEqual(len(self.pinned[-1]), 2)
            self.assertAlmostEqual(self.busy(), OUTSIDE + 2)
            slots.acquire("batch", 2, "critical-path stamp", timeout=5).release()

    def test_the_holder_is_pinned_to_its_own_slots_cores_not_its_neighbours(self) -> None:
        first = slots.acquire("batch", 2, "first")
        self.pinned.append({0})  # the first holder confined to its core: nothing else burns
        with slots.hold("batch", 2, "second"):
            self.assertEqual(self.pinned[-1], {2, 3})  # slot numbers; the pin maps them onto cores
            self.assertEqual(first.numbers, {0, 1})
        first.release()


class PinTest(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "sched_setaffinity") and len(os.sched_getaffinity(0)) >= 2, "needs 2 cores")
    def test_a_child_spawned_while_pinned_runs_on_those_cores_only(self) -> None:
        before = os.sched_getaffinity(0)
        mine = min(before)
        try:
            self.assertEqual(slots._pin({0}), before)
            out = subprocess.run(
                [sys.executable, "-c", "import os;print(sorted(os.sched_getaffinity(0)))"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertEqual(out.strip(), f"[{mine}]")
        finally:
            os.sched_setaffinity(0, before)
        self.assertEqual(os.sched_getaffinity(0), before)

    def test_slots_map_to_the_cores_the_process_may_use_not_to_core_numbers(self) -> None:
        rig = set(range(1, 13))  # the rig leaves core 0 (and 13-15) to its VMs: slot-00 must not map to a missing core
        self.assertEqual(slots._cores_for({0, 1}, rig), {1, 2})
        self.assertEqual(slots._cores_for({10, 11}, rig), {11, 12})
        self.assertEqual(len(slots._cores_for({0, 1, 2}, {4, 9})), 2)  # more slots than cores wraps, never fails


if __name__ == "__main__":
    unittest.main()
