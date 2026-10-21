import json
import tempfile
import unittest
from pathlib import Path

from abu_decomp import DecompError, tas_serve
from abu_decomp.config import load_profile


class ServeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "game.z64").write_bytes(b"rom")
        self.seat = self.root / "seat.json"

    def profile(self, **tas):
        table = {
            "movies": "movies",
            "runs": "runs",
            "port": "port",
            "rom": "game.z64",
            "args": ["--window", "640x480"],
            **tas,
        }
        self.seat.write_text(json.dumps({"schema_version": 1, "name": "t", "tas": table}))
        return load_profile(self.seat)

    def test_keysym_maps_only_bound_keys(self):
        self.assertEqual(tas_serve.keysym("ArrowDown"), "Down")
        self.assertEqual(tas_serve.keysym("Enter"), "Return")
        self.assertEqual(tas_serve.keysym("X"), "x")
        self.assertIsNone(tas_serve.keysym("F5"))
        self.assertIsNone(tas_serve.keysym("1"))

    def test_session_reads_serve_settings_and_window(self):
        session = tas_serve.Session(self.profile(serve={"listen": "0.0.0.0:9000", "display": ":5"}))
        self.assertEqual((session.listen, session.display, session.size), ("0.0.0.0:9000", ":5", "640x480"))
        with self.assertRaises(DecompError):
            tas_serve.Session(self.profile(serve={"port": 1}))

    def test_window_is_required_and_save_needs_a_run(self):
        with self.assertRaises(DecompError):
            tas_serve.window_size(["--rom", "x"])
        session = tas_serve.Session(self.profile())
        with self.assertRaises(DecompError):
            session.save("m", "")
        state = session.state()
        self.assertEqual((state["running"], state["polls"], state["movies"]), (False, 0, []))


if __name__ == "__main__":
    unittest.main()
