import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp import tas_fault

THREADS = """Thread 3 (Thread 0xf7c00 (LWP 4150) "worker"):
#0  0x08258e53 in func_8041F2A0_us_rev1 ()
#1  <signal handler called>
#2  0x0812f313 in func_80208000_de ()
#3  0x08124630 in func_802097E8_de ()

Thread 1 (Thread 0xf7d00 (LWP 4000) "main"):
#0  0x0818400 in n64c_boot_stopped ()
"""
SYMBOLS = ([0x0812F000, 0x08124000, 0x08258000], ["func_80208000_de", "func_802097E8_de", "func_8041F2A0_us_rev1"])


class FaultTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run_dir = Path(directory.name)
        patcher = mock.patch("abu_decomp.tas_symbolize.symbols", return_value=(sorted(SYMBOLS[0]), self.order()))
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def order():
        return [n for _, n in sorted(zip(*SYMBOLS, strict=True))]

    def test_the_thread_holding_the_signal_frame_is_selected_with_the_frames_under_it(self):
        got = tas_fault.from_threads(THREADS)
        self.assertEqual((got["tid"], got["eip"], got["frames"]), (4150, "0x0812f313", ["0x08124630"]))
        self.assertIsNone(tas_fault.from_threads("Thread 1 (LWP 1):\n#0  0x08100000 in main ()\n"))

    def test_a_shim_record_is_named_and_its_stack_file_starts_at_the_fault(self):
        record = {"signal": 11, "si_addr": "0x00000010", "access": "write", "tid": 4150, "eip": "0x0812f313"}
        (self.run_dir / "fault-frame.json").write_text(json.dumps({**record, "frames": ["0x08124630"]}))
        got = tas_fault.capture(self.run_dir, "/p/port")
        self.assertEqual((got["source"], got["access"], got["si_addr"]), ("shim", "write", "0x00000010"))
        self.assertEqual((got["at"], got["callers"]), ("func_80208000_de+0x313", ["func_802097E8_de+0x630"]))
        self.assertEqual(
            (self.run_dir / "failure-fault.txt").read_text(), "#0  func_80208000_de+0x313\n#1  func_802097E8_de+0x630\n"
        )

    def test_without_a_shim_record_the_thread_listing_names_the_fault(self):
        (self.run_dir / "failure-threads.txt").write_text(THREADS)
        got = tas_fault.capture(self.run_dir, "/p/port")
        self.assertEqual((got["source"], got["tid"], got["at"]), ("gdb", 4150, "func_80208000_de+0x313"))

    def test_a_run_with_no_fault_leaves_nothing(self):
        self.assertIsNone(tas_fault.capture(self.run_dir, "/p/port"))
        self.assertFalse((self.run_dir / "failure-fault.txt").exists())


if __name__ == "__main__":
    unittest.main()
