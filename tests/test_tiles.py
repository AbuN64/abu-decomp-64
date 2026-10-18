import json
import tempfile
import unittest
from pathlib import Path

from abu_decomp import DecompError
from abu_decomp.config import load_profile
from abu_decomp.stream import layout
from abu_decomp.stream.tiles import up_config

UP = {"run_dir": "run", "output": {"name": "HEADLESS-1", "width": 1920, "height": 1080}}
TILES = {
    "layout": [["pv", "ov"], ["pc", "oc"]],
    "focus": "ov",
    "roles": {
        "pv": {"title": "port video", "command": ["sleep", "1"], "overlay_app_id": "game"},
        "ov": {"title": "oracle video", "command": ["sleep", "1"]},
        "pc": {"title": "port console", "console": {"title": "PORT"}},
        "oc": {"title": "oracle console", "command": ["true"], "console": {"title": "ORACLE"}},
        "port": {"title": "port", "headless": True, "command": ["true"]},
    },
}


class TilesTests(unittest.TestCase):
    def profile(self, up=None, tiles=TILES):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "seat.json"
        path.write_text(json.dumps({"schema_version": 1, "name": "t", "stream": {"up": up or UP}, "tiles": tiles}))
        return load_profile(path)

    def test_one_table_feeds_the_moved_layout_code(self):
        config = up_config(self.profile())
        self.assertEqual(layout.validate(config), (["pv", "pc"], ["ov", "oc"]))
        self.assertEqual(config["focus_role"], "ov")
        self.assertEqual(layout.overlays(config)[0][:2], ("pv", "game"))

    def test_console_tile_without_a_command_runs_decomp_console_for_its_own_id(self):
        config = up_config(self.profile())
        self.assertEqual(config["roles"]["pc"]["command"][-3:], ["console", "--tile", "pc"])
        self.assertEqual(config["roles"]["oc"]["command"], ["true"])
        self.assertIn("--profile", config["roles"]["pc"]["command"])

    def test_a_second_copy_of_the_roles_in_stream_up_is_refused(self):
        for key in ("roles", "tiles", "focus_role"):
            with self.assertRaises(DecompError) as raised:
                up_config(self.profile(up={**UP, key: {}}))
            self.assertIn(key, str(raised.exception))

    def test_unknown_or_missing_table_keys_are_refused(self):
        with self.assertRaises(DecompError):
            up_config(self.profile(tiles={**TILES, "extra": 1}))
        with self.assertRaises(DecompError):
            up_config(self.profile(tiles={"layout": TILES["layout"]}))
