"""The host slot pool (module slots): capacity bound, queueing, owner reserve, crash release, re-entrancy."""

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from abu_decomp import slots


class SlotsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="slots-")
        patcher = mock.patch.dict(os.environ, {"ABU_SLOTS_DIR": self.dir, "ABU_SLOT_HELD": "", "ABU_SLOTS_OFF": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        cap = mock.patch.object(slots, "capacity", return_value=2)
        cap.start()
        self.addCleanup(cap.stop)
        mem = mock.patch.object(slots, "_mem_gib", return_value=50.0)
        mem.start()
        self.addCleanup(mem.stop)

    def test_pool_is_bounded_and_the_waiter_wakes_on_release(self) -> None:
        a = slots.acquire("batch", 1, "a")
        b = slots.acquire("batch", 1, "b")
        self.assertEqual(slots.status()["used"], 2)
        with self.assertRaises(TimeoutError):
            slots.acquire("batch", 1, "c", timeout=0.2)
        got = []
        thread = threading.Thread(target=lambda: got.append(slots.acquire("batch", 1, "c", timeout=20)))
        thread.start()
        time.sleep(0.3)
        self.assertEqual(len(slots.status()["queue"]), 1)
        a.release()
        thread.join(10)
        self.assertEqual(len(got), 1)
        b.release()
        got[0].release()
        self.assertEqual(slots.status()["used"], 0)

    def test_owner_uses_the_reserve_beyond_capacity(self) -> None:
        a = slots.acquire("batch", 2, "fill")
        owner = slots.acquire("owner", 1, "watched", timeout=2)
        self.assertEqual(slots.status()["used"], 3)
        owner.release()
        a.release()

    def test_priority_orders_the_queue(self) -> None:
        a = slots.acquire("batch", 2, "fill")
        order, threads = [], []
        for klass in ("background", "interactive"):

            def run(klass=klass):
                with slots.hold(klass, 1, klass):
                    order.append(klass)
                    time.sleep(0.2)

            threads.append(threading.Thread(target=run))
            threads[-1].start()
            # both waiters queued before the release, whatever the host load (a fixed sleep raced under load, PF9)
            deadline = time.monotonic() + 10
            while len(slots.status()["queue"]) < len(threads) and time.monotonic() < deadline:
                time.sleep(0.01)
        a.release()
        for thread in threads:
            thread.join(10)
        self.assertEqual(order[0], "interactive")

    def test_hold_is_reentrant_in_a_thread(self) -> None:
        with slots.hold("batch", 2, "outer"):
            with slots.hold("batch", 2, "inner") as nested:
                self.assertIsNone(nested)
            self.assertEqual(slots.status()["used"], 2)
        self.assertEqual(slots.status()["used"], 0)

    def test_dead_holder_releases_its_slot(self) -> None:
        pid = os.fork()
        if pid == 0:
            slots.acquire("batch", 2, "doomed")
            os._exit(0)  # no release: the kernel drops the flocks
        os.waitpid(pid, 0)
        self.assertEqual(slots.status()["used"], 0)
        slots.acquire("batch", 2, "after", timeout=2).release()


if __name__ == "__main__":
    unittest.main()
