"""The host slot pool (module slots): capacity bound, queueing, owner reserve, crash release, re-entrancy."""

import contextlib
import os
import select
import signal
import tempfile
import threading
import time
import unittest
from unittest import mock

from abu_decomp import slots

BOUND = 60  # seconds a failing wait gives up after; a passing one is woken by its signal and never waits on it


def select_ready(fd: int) -> bool:
    return bool(select.select([fd], [], [], 0)[0])


def child_status(action) -> int:
    """A forked test must exit on failure too, never fall through into the inherited suite and its cleanup."""
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            action()
            code = 0
        finally:
            os._exit(code)  # deliberately no release: verify the kernel drops the child's flocks
    try:
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)
        raise


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
        self.busy = [0.0]  # cores the host measures busy, holders' children and outside batches alike
        self.young = [False]  # whether a holder has not yet had SETTLE seconds to show its CPU
        for name, fake in (("busy_cores", lambda: self.busy[0]), ("_fresh_holder", lambda root: self.young[0])):
            patch = mock.patch.object(slots, name, side_effect=fake)
            patch.start()
            self.addCleanup(patch.stop)

    def waiting(self, count: int) -> None:
        deadline = time.monotonic() + 10  # queued before the next step, whatever the host load
        while len(slots.status()["queue"]) < count and time.monotonic() < deadline:
            time.sleep(0.01)

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

    def test_probing_a_slot_raises_no_event_for_the_waiters(self) -> None:
        """A probe closing a writable descriptor raised IN_CLOSE_WRITE and woke every waiter to probe again."""
        root, tickets = slots._paths()
        held = slots.acquire("batch", 1, "holder")
        self.addCleanup(held.release)
        for path in slots._slot_files(root):
            slots._held(path)  # creates the slot files: a first probe's IN_CREATE is not what this test is about
        waiter = slots._Wait(root, tickets)
        self.addCleanup(waiter.close)
        for path in slots._slot_files(root):
            slots._held(path)
        slots.free_count()
        slots._live_tickets(tickets)
        self.assertFalse(select_ready(waiter.fd), "a lock probe woke the waiters")

    def test_owner_uses_the_reserve_beyond_capacity(self) -> None:
        a = slots.acquire("batch", 2, "fill")
        owner = slots.acquire("owner", 1, "watched", timeout=2)
        self.assertEqual(slots.status()["used"], 3)
        owner.release()
        a.release()

    def parking(self, *names: str, hold: threading.Event | None = None) -> dict[str, threading.Event]:
        """An event per thread name, set when that thread's waiter first goes to sleep: its ticket is queued and the
        pass that read the queue and the pool is over, so the test goes on exactly then, whatever the host load.
        With HOLD, that first sleep lasts until HOLD is set, so the test decides when the parked thread passes again."""
        parked = {name: threading.Event() for name in names}
        sleep = slots._Wait.sleep

        def park(waiter, seconds):
            event = parked.get(threading.current_thread().name)
            if event:
                event.set()
            if event and hold is not None and not hold.is_set():
                hold.wait(BOUND)
                return
            sleep(waiter, seconds)

        patcher = mock.patch.object(slots._Wait, "sleep", park)
        patcher.start()
        self.addCleanup(patcher.stop)
        return parked

    def test_priority_orders_the_queue(self) -> None:
        a = slots.acquire("batch", 2, "fill")
        parked = self.parking("background", "interactive")
        order, threads = [], []
        for klass in ("background", "interactive"):

            def run(klass=klass):
                with slots.hold(klass, 2, klass):  # the whole pool: one runs at a time, so order is the grant order
                    order.append(klass)

            threads.append(threading.Thread(target=run, name=klass))
            threads[-1].start()
            self.assertTrue(parked[klass].wait(BOUND), f"{klass} never queued")  # both queued before the release
        a.release()
        for thread in threads:
            thread.join(BOUND)
        self.assertEqual(order, ["interactive", "background"])

    def test_a_better_ticket_queued_after_the_pass_read_the_queue_is_served_first(self) -> None:
        """The race the load exposed: a background pass read the queue (nobody ahead), then an interactive ticket queued
        and the pool freed before the pass locked the slots; the background took them past the better ticket."""
        a = slots.acquire("batch", 2, "fill")
        resume = threading.Event()  # the interactive waiter stays parked until the background pass has locked the slots
        parked = self.parking("interactive", hold=resume)
        order, threads, queued = [], [], threading.Event()
        real, real_take = slots._can_run, slots._take

        def can_run(*args, **kwargs):
            if threading.current_thread().name == "background" and not queued.is_set():
                queued.set()
                threads.append(threading.Thread(target=run, args=("interactive",), name="interactive"))
                threads[-1].start()
                self.assertTrue(parked["interactive"].wait(BOUND), "interactive never queued")
                a.release()
            return real(*args, **kwargs)

        def take(*args):
            got = real_take(*args)
            if threading.current_thread().name == "background":
                resume.set()
            return got

        def run(klass):
            with slots.hold(klass, 2, klass):
                order.append(klass)

        with mock.patch.object(slots, "_can_run", side_effect=can_run), mock.patch.object(slots, "_take", take):
            background = threading.Thread(target=run, args=("background",), name="background")
            background.start()
            background.join(BOUND)
            threads[0].join(BOUND)
        self.assertEqual(order, ["interactive", "background"])

    def test_hold_is_reentrant_in_a_thread(self) -> None:
        with slots.hold("batch", 2, "outer"):
            with slots.hold("batch", 2, "inner") as nested:
                self.assertIsNone(nested)
            self.assertEqual(slots.status()["used"], 2)
        self.assertEqual(slots.status()["used"], 0)

    def test_dead_holder_releases_its_slot(self) -> None:
        self.assertEqual(child_status(lambda: slots.acquire("batch", 2, "doomed")), 0)
        self.assertEqual(slots.status()["used"], 0)
        slots.acquire("batch", 2, "after", timeout=2).release()

    def test_refused_child_exits_without_running_the_inherited_suite(self) -> None:
        # Real failing slice: a /tmp pool had 29.3 GiB against the unchanged 40 GiB floor.
        with (
            mock.patch.object(slots, "DISK_FLOOR_GIB", 40.0),  # the conftest zeroes the floor for unrelated tests
            mock.patch.object(slots, "disk_free_gib", return_value=29.3),
        ):
            self.assertEqual(child_status(lambda: slots.acquire("batch", 2, "refused")), 1)
        self.assertEqual(slots.status()["used"], 0)
        self.assertEqual(slots.status()["queue"], [])

    def test_a_wide_job_is_not_passed_by_narrower_later_ones(self) -> None:
        """Strict FIFO with reservation: a later weight-1 ticket leaves the free slot the older weight-2 waits for."""
        first = slots.acquire("batch", 1, "holder")
        wide: list = []
        thread = threading.Thread(target=lambda: wide.append(slots.acquire("batch", 2, "wide", timeout=20)))
        thread.start()
        self.waiting(1)
        with self.assertRaises(TimeoutError):
            slots.acquire("batch", 1, "late", timeout=0.5)  # one slot is free and it fits, but the wide job is older
        first.release()
        thread.join(10)
        self.assertEqual(len(wide), 1)
        wide[0].release()

    def test_a_ticket_keeps_its_first_queue_time_across_re_requests(self) -> None:
        """The names batch re-requested after a timeout and its ticket age read 119 s after 44 min."""
        fill = slots.acquire("batch", 2, "fill")
        began = time.time()
        with self.assertRaises(TimeoutError):
            slots.acquire("batch", 2, "names", timeout=0.2)
        order: list[str] = []

        def run(label: str, weight: int) -> None:
            with slots.hold("batch", weight, label):
                order.append(label)
                time.sleep(0.1)

        time.sleep(0.5)
        late = threading.Thread(target=run, args=("late", 1))
        late.start()
        self.waiting(1)
        again = threading.Thread(target=run, args=("names", 2))  # same parent, same label: the same request again
        again.start()
        self.waiting(2)
        queue = {row["label"]: row for row in slots.status()["queue"]}
        self.assertLess(queue["names"]["queued"], began + 0.1)
        self.assertGreaterEqual(queue["names"]["age"], 0)
        self.assertLess(queue["names"]["queued"], queue["late"]["queued"])
        fill.release()
        for thread in (late, again):
            thread.join(10)
        self.assertEqual(order, ["names", "late"])

    def test_measured_busy_cores_gate_a_grant_whatever_the_declared_weight(self) -> None:
        one = slots.acquire("batch", 1, "inside")
        self.busy[0] = 2.0  # the weight-1 holder really runs oracle + port + gdb: 2 cores, the whole capacity
        with self.assertRaises(TimeoutError):
            slots.acquire("batch", 1, "next", timeout=0.3)
        self.busy[0] = 1.0  # one core free beside it
        slots.acquire("batch", 1, "next", timeout=5).release()
        one.release()

    def test_a_request_wider_than_the_free_cores_waits_for_them(self) -> None:
        one = slots.acquire("batch", 1, "inside")
        self.busy[0] = 1.5
        with self.assertRaises(TimeoutError):
            slots.acquire("batch", 1, "next", timeout=0.3)
        one.release()

    def test_no_ticket_is_admitted_beside_a_holder_that_has_not_shown_its_cpu(self) -> None:
        one = slots.acquire("batch", 1, "inside")
        self.young[0] = True
        with self.assertRaises(TimeoutError):
            slots.acquire("batch", 1, "next", timeout=0.3)
        self.young[0] = False
        slots.acquire("batch", 1, "next", timeout=5).release()
        one.release()

    def test_an_empty_pool_still_admits_beside_outside_work(self) -> None:
        self.busy[0] = 11.0
        slots.acquire("batch", 1, "alone", timeout=5).release()

    def test_the_owner_ignores_the_measured_load(self) -> None:
        one = slots.acquire("batch", 1, "inside")
        self.busy[0] = 11.0
        slots.acquire("owner", 1, "watched", timeout=5).release()
        one.release()


class BusyCoresTest(unittest.TestCase):
    def setUp(self) -> None:
        patch = mock.patch.object(slots, "_reading", None)
        patch.start()
        self.addCleanup(patch.stop)

    def test_busy_cores_are_the_host_busy_share_of_its_cores(self) -> None:
        with (
            mock.patch.object(slots, "_stat", side_effect=[(0, 0), (600, 1200)]),
            mock.patch.object(slots, "cores", return_value=12),
            mock.patch.object(slots.time, "sleep"),
            mock.patch.object(slots.time, "monotonic", side_effect=[10.0, 10.5]),
        ):
            self.assertAlmostEqual(slots.busy_cores(), 6.0)  # half the ticks of a 12-core host were busy

    def test_a_recent_reading_is_reused_and_a_later_one_spans_from_it(self) -> None:
        with (
            mock.patch.object(slots, "_stat", side_effect=[(0, 0), (600, 1200), (1200, 1800)]),
            mock.patch.object(slots, "cores", return_value=12),
            mock.patch.object(slots.time, "sleep"),
            mock.patch.object(slots.time, "monotonic", side_effect=[10.0, 10.5, 10.9, 12.0, 12.0]),
        ):
            first = slots.busy_cores()
            self.assertEqual(slots.busy_cores(), first)  # 0.4 s later: reused, nothing read
            self.assertAlmostEqual(slots.busy_cores(), 12.0)  # 600 busy of the next 600 ticks

    def test_status_reports_the_measured_busy_cores_and_each_waiter_s_true_wait(self) -> None:
        with (
            mock.patch.dict(os.environ, {"ABU_SLOTS_DIR": tempfile.mkdtemp(prefix="slots-")}),
            mock.patch.object(slots, "busy_cores", return_value=3.25),
        ):
            self.assertEqual(slots.status()["busy_cores"], 3.2)


class PlayClassTest(unittest.TestCase):
    def klass(self, name: str | None, **kwargs) -> str:
        from abu_decomp import tas, tas_bake

        taken = []

        @contextlib.contextmanager
        def fake(klass, weight, label, **kw):
            taken.append(klass)
            yield None

        with (
            mock.patch.object(slots, "hold", fake),
            mock.patch.object(tas_bake, "movie_hook"),
            mock.patch.object(tas, "slots", slots),
        ):
            tas._admitted(lambda profile, name, **k: None)(None, name, **kwargs)
        return taken[0]

    def test_only_a_person_s_watched_play_is_interactive(self) -> None:
        self.assertEqual(self.klass("movie"), "background")  # an automated turbo play of a corpus or lane batch
        self.assertEqual(self.klass("movie", speed=0, oracle=True), "background")
        self.assertEqual(self.klass("movie", speed=1), "interactive")  # real time: someone is watching
        self.assertEqual(self.klass("movie", slot_class="background", speed=1), "background")  # play_all says so
        self.assertEqual(self.klass(None), "owner")


if __name__ == "__main__":
    unittest.main()
