import unittest

from abu_decomp.tas_watch import Watcher


def state(**kw):
    base = {"run": "/r/1", "running": True, "polls": 5, "exit": None, "stopped": {}, "stacks": [], "marks": {"count": 0}}
    return {**base, **kw}


class WatcherTests(unittest.TestCase):
    def test_first_state_is_baseline(self):
        self.assertEqual(Watcher().feed(state(), 0), [])

    def test_start_then_stop_with_stacks_once(self):
        w = Watcher()
        w.feed(state(run=None, running=False), 0)
        self.assertEqual([e["event"] for e in w.feed(state(), 1)], ["start"])
        held = state(running=False, polls=9, stopped={"outcome": "stop", "subject": "s"}, stacks=["/r/1/a.txt"])
        (event,) = w.feed(held, 2)
        self.assertEqual((event["event"], event["subject"], event["polls"], event["stacks"]), ("stop", "s", 9, ["/r/1/a.txt"]))
        self.assertEqual(w.feed(held, 3), [])

    def test_crash_and_end(self):
        w = Watcher()
        w.feed(state(), 0)
        crash = state(running=False, stopped={"outcome": "crash", "subject": "host-fault-segv"})
        self.assertEqual(w.feed(crash, 1)[0]["event"], "crash")
        (end,) = w.feed({**crash, "exit": -15, "stopped": {}}, 2)
        self.assertEqual((end["event"], end["exit"]), ("end", -15))

    def test_hang_after_idle_polls_once_and_rearms(self):
        w = Watcher(hang_seconds=15)
        w.feed(state(), 0)
        self.assertEqual(w.feed(state(), 14), [])
        (hang,) = w.feed(state(), 15)
        self.assertEqual(hang["event"], "hang")
        self.assertEqual(w.feed(state(), 40), [])
        self.assertEqual(w.feed(state(polls=6), 41), [])
        self.assertEqual(w.feed(state(polls=6), 56)[0]["event"], "hang")

    def test_stopped_run_is_not_a_hang(self):
        w = Watcher(hang_seconds=15)
        w.feed(state(), 0)
        w.feed(state(running=False, stopped={"outcome": "stop", "subject": "s"}), 1)
        self.assertEqual(w.feed(state(running=False, stopped={"outcome": "stop", "subject": "s"}), 60), [])

    def test_mark(self):
        w = Watcher()
        w.feed(state(), 0)
        last = {"poll": 7, "note": "odd", "frame": "/r/1/marks/poll-7.jpg", "movie": "weird-x"}
        (event,) = w.feed(state(marks={"count": 1, "last": last}), 1)
        self.assertEqual((event["event"], event["poll"], event["note"], event["movie"]), ("mark", 7, "odd", "weird-x"))
        self.assertEqual(w.feed(state(marks={"count": 1, "last": last}), 2), [])

    def test_new_run_resets_marks(self):
        w = Watcher()
        w.feed(state(marks={"count": 2, "last": {"poll": 1}}), 0)
        events = w.feed(state(run="/r/2", polls=0, marks={"count": 0, "last": None}), 1)
        self.assertEqual([e["event"] for e in events], ["start"])


if __name__ == "__main__":
    unittest.main()
