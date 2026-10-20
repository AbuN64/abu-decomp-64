from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

from abu_decomp import meter

SPIN = "import time\nend = time.process_time() + {seconds}\nwhile time.process_time() < end: pass\n{after}"


class MeterTests(unittest.TestCase):
    def test_a_running_child_counts_before_anyone_waits_for_it(self) -> None:
        before = meter.cpu()
        child = subprocess.Popen([sys.executable, "-c", SPIN.format(seconds=0.5, after="time.sleep(30)")])
        try:
            end = time.monotonic() + 20
            while meter.cpu() - before < 0.4 and time.monotonic() < end:
                time.sleep(0.05)
            self.assertGreaterEqual(meter.cpu() - before, 0.4)
        finally:
            child.kill()
            child.wait()

    def test_the_workers_a_stopped_tool_left_behind_are_read_and_ended(self) -> None:
        marker = f"ABU_DECOMP_CALL={os.getpid()}.{time.time_ns()}"
        worker = SPIN.format(seconds=0.5, after="time.sleep(60)")
        tool = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {worker!r}])"
        subprocess.run(
            [sys.executable, "-c", tool], check=True, env={**os.environ, "ABU_DECOMP_CALL": marker.split("=")[1]}
        )
        deadline = time.monotonic() + 20
        spent = 0.0
        while (
            spent < 0.4 and time.monotonic() < deadline
        ):  # the worker is an orphan now; wait until it has used its CPU
            time.sleep(0.1)
            rows = meter._table()
            spent = sum(rows[pid][2] for pid in rows if pid != os.getpid() and self.marked(pid, marker))
        self.assertGreaterEqual(meter.stop_marked(marker.encode()), 0.4)
        self.assertFalse([pid for pid in meter._table() if self.marked(pid, marker) and not meter._zombie(pid)])

    @staticmethod
    def marked(pid: int, marker: str) -> bool:
        try:
            with open(f"/proc/{pid}/environ", "rb") as handle:
                return marker.encode() in handle.read().split(b"\0")
        except OSError:
            return False


if __name__ == "__main__":
    unittest.main()
