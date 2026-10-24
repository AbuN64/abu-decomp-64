"""The phase timelapse: identical frames skipped, mp4 written, empty folder refused."""

import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path

from abu_decomp import DecompError, tas_timelapse


def png(shade: int, size: int = 8) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"".join(b"\x00" + bytes([shade, 0, 255 - shade]) * size for _ in range(size))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class TimelapseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name)
        self.folder = self.runs / "phase" / "frames" / "v"
        self.folder.mkdir(parents=True)

    def put(self, shades):
        for index, shade in enumerate(shades):
            (self.folder / f"2026010{index}T000000Z.png").write_bytes(png(shade))

    def test_identical_consecutive_frames_are_skipped(self):
        self.put([10, 10, 20, 20, 10])
        files, dropped = tas_timelapse.frames(self.folder)
        self.assertEqual(len(files), 3)
        self.assertEqual(dropped, 2)

    def test_listing_names_the_last_frame_twice(self):
        files = [Path("/a.png"), Path("/b.png")]
        lines = tas_timelapse.listing(files, 10).splitlines()
        self.assertEqual(lines[-1], "file '/b.png'")
        self.assertEqual(lines.count("duration 0.100000"), 2)

    def test_no_frames_is_refused(self):
        with self.assertRaises(DecompError) as raised:
            tas_timelapse.make(self.runs, "v")
        self.assertEqual(raised.exception.key, "phase.frames")

    def test_bad_fps_is_refused(self):
        with self.assertRaises(DecompError):
            tas_timelapse.make(self.runs, "v", 0)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
    def test_mp4_plays(self):
        self.put([10, 10, 60, 120])
        result = tas_timelapse.make(self.runs, "v", 4)
        self.assertEqual(result["frames"], 3)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", result["mp4"]],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertAlmostEqual(float(probe.stdout), 3 / 4, places=3)  # 3 frames at 4 fps, no extra frame
        self.assertFalse((self.runs / "phase" / "v.list").exists())


if __name__ == "__main__":
    unittest.main()
