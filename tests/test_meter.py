from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

from abu_decomp import meter

BURN = "import time; end = time.process_time() + 0.4\nwhile time.process_time() < end: pass"


class OrphanCpuTests(unittest.TestCase):
    def setUp(self) -> None:
        if not meter.adopt():
            self.skipTest("the kernel has no child subreaper")

    def test_an_orphan_that_used_cpu_after_its_parent_left_is_counted(self) -> None:
        before = meter.cpu()
        # the child starts a grandchild that burns CPU and exits at once: the grandchild is an orphan below us
        code = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {BURN!r}])"
        subprocess.run([sys.executable, "-c", code], check=True)
        end = time.monotonic() + 10
        while meter.cpu() - before < 0.3 and time.monotonic() < end:
            time.sleep(0.05)
        self.assertGreaterEqual(meter.cpu() - before, 0.3)

    def test_kill_descendants_ends_a_worker_the_tool_left_running(self) -> None:
        code = "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])"
        subprocess.run([sys.executable, "-c", code], check=True)
        deadline = time.monotonic() + 5
        while not meter.descendants() and time.monotonic() < deadline:
            time.sleep(0.02)
        mine = meter.descendants()
        self.assertTrue(mine)
        self.assertEqual(meter.kill_descendants(), len(mine))
        self.assertEqual([pid for pid in mine if os.path.exists(f"/proc/{pid}")], [])


if __name__ == "__main__":
    unittest.main()
