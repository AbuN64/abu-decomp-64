import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from abu_decomp import tools
from abu_decomp.config import load_profile


class SeatTreeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "seat").mkdir()
        (root / "mine").mkdir()
        (root / "host.toml").write_text("")
        table = {"project": "seat", "host": "host.toml", "timeout_seconds": 5}
        (root / "seat.json").write_text(json.dumps({"schema_version": 1, "name": "s", "tools": table}))
        self.profile = load_profile(root / "seat.json")
        self.mine = root / "mine"
        done = SimpleNamespace(
            returncode=0, stdout=json.dumps({**dict.fromkeys(tools.ENVELOPE, True), "findings": []}).encode()
        )
        for target, kwargs in (
            ("abu_decomp.tools.subprocess.run", {"return_value": done}),
            ("abu_decomp.tools.verify_dependencies", {"return_value": {}}),
            ("abu_decomp.tools.require_checkouts", {}),
            ("abu_decomp.tools.pin_findings", {"return_value": []}),
        ):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.run_ = tools.subprocess.run

    def call(self, args, project=None, lane="L1"):
        env = {"DECOMP_LANE": lane} if lane else {}
        with mock.patch.dict("os.environ", env, clear=not lane):
            return tools.invoke("unbake", self.profile, args, project=project)

    def test_a_lanes_writing_verb_without_project_is_refused_naming_the_seat_tree(self):
        for args in (["sync"], ["setup"], ["structure", "apply", "x"], ["name", "submit", "x"], ["land"]):
            with self.assertRaises(tools.DependencyError) as caught:
                self.call(args)
            self.assertEqual(caught.exception.key, "dependency.seat_tree")
            self.assertIn("seat", caught.exception.detail)
            self.assertIn("--project", caught.exception.detail)
        self.run_.assert_not_called()

    def test_a_named_tree_runs(self):
        self.assertTrue(self.call(["sync"], project=self.mine).ok)
        self.run_.assert_called_once()

    def test_read_verbs_and_non_lane_callers_keep_the_default(self):
        self.assertTrue(self.call(["verify"]).ok)
        self.assertTrue(self.call(["sync", "--check"]).ok)
        self.assertTrue(self.call(["sync"], lane=None).ok)


if __name__ == "__main__":
    unittest.main()
