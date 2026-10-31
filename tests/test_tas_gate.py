"""The poll gate seen from the package: its datagrams, the held poll read back from the port's log, the supervisor
that leaves a gated run alone, and the original replaying the prefix of a run held at a poll."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp import DecompError, tas


class GateDatagramTests(unittest.TestCase):
    def test_each_verb_is_one_datagram(self):
        self.assertEqual(tas.gate_datagram("hold"), b"hold")
        self.assertEqual(tas.gate_datagram("run"), b"run")
        self.assertEqual(tas.gate_datagram("step"), b"step")
        self.assertEqual(tas.gate_datagram("step", 4), b"step 4")
        self.assertEqual(tas.gate_datagram("seek", 0), b"seek 0")
        self.assertEqual(tas.gate_datagram("seek", 900), b"seek 900")

    def test_a_bad_verb_or_count_is_refused(self):
        for verb, count in (("rewind", None), ("seek", None), ("seek", -1), ("step", 0)):
            with self.subTest(verb=verb, count=count), self.assertRaises(DecompError):
                tas.gate_datagram(verb, count)


class HeldTests(unittest.TestCase):
    def run_with(self, text):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        run = Path(directory.name)
        if text is not None:
            (run / "output.txt").write_text(text)
        return run

    def test_held_is_the_newest_gate_line(self):
        run = self.run_with(
            "boot\nn64c-tas: held at poll 25\nn64c-tas: running from poll 25\nn64c-tas: held at poll 31\n"
        )
        self.assertEqual(tas.held_at(run), 31)

    def test_a_run_that_went_on_is_not_held(self):
        run = self.run_with("n64c-tas: held at poll 25\nn64c-tas: running from poll 25\n")
        self.assertIsNone(tas.held_at(run))

    def test_no_output_or_no_gate_line_is_not_held(self):
        self.assertIsNone(tas.held_at(self.run_with(None)))
        self.assertIsNone(tas.held_at(self.run_with("boot-frame: live input: real time\n")))


class SupervisorTests(unittest.TestCase):
    def test_a_gated_run_is_not_killed_at_the_timeout(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        run = Path(directory.name)
        (run / tas.GATED).write_text("hold\n")
        process = mock.Mock(pid=4242)
        clock = [0.0]  # moves only when the supervisor sleeps: 5 s of held time per pass, against a 1 s timeout
        waits = iter([(0, 0, None), (0, 0, None), (4242, 0, mock.Mock(ru_utime=1.0, ru_stime=0.5))])
        with (
            mock.patch.object(tas.time, "monotonic", lambda: clock[0]),
            mock.patch.object(tas.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + 5.0)),
            mock.patch.object(tas.os, "wait4", lambda pid, flags: next(waits)),
            mock.patch.object(tas.os, "killpg", side_effect=AssertionError("a gated run is never killed")),
            mock.patch.object(tas, "_check_play_signal"),
        ):
            code, cpu = tas._wait(process, 1.0, run)
        self.assertEqual((code, cpu), (0, 1.5))


if __name__ == "__main__":
    unittest.main()
