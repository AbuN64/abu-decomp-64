import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from abu_decomp import DecompError
from abu_decomp.cli import main
from abu_decomp.config import load_profile
from abu_decomp.console import tiles


class TileSourceTests(unittest.TestCase):
    def profile(self, roles, console=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        document = {
            "schema_version": 1,
            "name": "t",
            "events": {"path": "feed/events.jsonl"},
            "tiles": {"layout": [["a", "b"], ["c", "d"]], "roles": roles},
        }
        if console is not None:
            document["console"] = console
        path = root / "seat.json"
        path.write_text(json.dumps(document))
        return load_profile(path), root

    def test_sources_replace_the_role_script_heredocs(self):
        spec = {
            "title": "oracle console",
            "console": {
                "title": "ORACLE / MISSION",
                "kinds": ["RUN-FAIL", "PROBE", "MATCH", "HANDOFF", "LAND"],
                "host_log": "oracle.log",
                "sources": {"run_status": "runs/status.json", "boot_plan": "/abs/plan-*/BOOT.md"},
            },
        }
        profile, root = self.profile({"oc": spec})
        options = tiles.tile_sources(profile, "oc")
        self.assertEqual(options["path"], (root / "feed/events.jsonl").resolve())
        self.assertEqual(options["kinds"], {"RUN-FAIL", "PROBE", "MATCH", "HANDOFF", "LAND"})
        self.assertEqual(options["host_log"], (root / "oracle.log").resolve())
        self.assertEqual(options["sources"]["run_status"], str((root / "runs/status.json").resolve()))
        self.assertEqual(options["sources"]["boot_plan"], "/abs/plan-*/BOOT.md")
        self.assertEqual(options["title"], "ORACLE / MISSION")
        self.assertEqual(options["max_lines"], 4000)

    def test_a_tile_may_name_its_own_feed(self):
        profile, root = self.profile(
            {"pc": {"console": {"feed": "port/events.jsonl", "sources": {"events": ["x.jsonl"]}}}}
        )
        options = tiles.tile_sources(profile, "pc")
        self.assertEqual(options["path"], (root / "port/events.jsonl").resolve())
        self.assertEqual(options["sources"]["events"], [str((root / "x.jsonl").resolve())])

    def test_schema1_kind_names_unknown_tiles_and_unknown_keys_are_refused(self):
        profile, _ = self.profile({"oc": {"console": {"kinds": ["PORT-FAIL"]}}, "pc": {}, "xc": {"console": {"x": 1}}})
        with self.assertRaisesRegex(ValueError, "PORT-FAIL is a schema 1 kind name; filter on RUN-FAIL"):
            tiles.tile_sources(profile, "oc")
        for tile in ("pc", "nope", "xc"):
            with self.assertRaises(DecompError):
                tiles.tile_sources(profile, tile)

    def test_run_tile_passes_the_options_to_the_console(self):
        profile, _ = self.profile({"oc": {"console": {}}}, console={"max_lines": 50})
        with patch("abu_decomp.console.app.run_console") as console:
            self.assertEqual(tiles.run_tile(profile, "oc")["tile"], "oc")
        self.assertEqual(console.call_args.kwargs["max_lines"], 50)
        self.assertEqual(console.call_args.kwargs["title"], "OC")

    def test_public_command_reports_an_unknown_tile_as_one_envelope(self):
        _, root = self.profile({"oc": {"console": {}}})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--profile", str(root / "seat.json"), "console", "tile", "nope"])
        envelope = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(envelope["findings"][0]["key"], "console.tile")
