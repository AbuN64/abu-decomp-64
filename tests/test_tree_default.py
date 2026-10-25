"""Every command that acts on a project tree resolves it one way: --project, else the work tree of the cwd."""

import argparse
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp import cli
from abu_decomp.config import load_profile


def git(*args, cwd):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}
    )


class TreeDefault(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.seat, self.lane = root / "seat", root / "lane"
        self.seat.mkdir()
        git("init", "-q", cwd=self.seat)
        git("-c", "user.name=a", "-c", "user.email=a@b", "commit", "-q", "--allow-empty", "-m", "x", cwd=self.seat)
        git("worktree", "add", "-q", "--detach", str(self.lane), cwd=self.seat)
        (root / "host.toml").write_text("")
        section = {"project": "seat", "host": "host.toml", "timeout_seconds": 5}
        (root / "seat.json").write_text(json.dumps({"schema_version": 1, "name": "seat", "tools": section}))
        self.args = argparse.Namespace(profile=root / "seat.json", project=None, host=None, replay=False)
        self.profile = load_profile(root / "seat.json")
        self.elsewhere = root

    def stand_in(self, where):
        patch = mock.patch("pathlib.Path.cwd", return_value=where)
        patch.start()
        self.addCleanup(patch.stop)

    def test_every_tree_command_defaults_to_the_work_tree_of_the_cwd(self):
        self.stand_in(self.lane)
        for tool in ("behave", "arity", "owes", "cleanup"):
            self.assertEqual(cli._own_tree(self.args, tool), self.lane, tool)

    def test_an_explicit_project_wins_and_the_seat_tree_itself_keeps_the_profile(self):
        self.stand_in(self.lane)
        self.args.project = self.seat
        self.assertEqual(cli._own_tree(self.args, "behave"), self.seat)
        self.args.project = None
        self.stand_in(self.seat)
        self.assertIsNone(cli._own_tree(self.args, "owes"))

    def test_a_lane_outside_any_work_tree_is_refused_not_given_the_seat_tree(self):
        self.stand_in(self.elsewhere)
        with mock.patch.dict(os.environ, {"DECOMP_LANE": "L1"}), self.assertRaises(Exception) as caught:
            cli._own_tree(self.args, "cleanup")
        self.assertIn("seat", str(caught.exception))

    def test_cleanup_count_and_behave_hand_the_lane_tree_on(self):
        self.stand_in(self.lane)
        self.args.verb, self.args.dir, self.args.top, self.args.evidence = "count", None, 0, False
        with mock.patch("abu_decomp.cleanup.count", return_value={"folders": {}, "total": 0, "classes": {}}) as count:
            cli._run_cleanup(self.args)
        self.assertEqual(count.call_args.args[0], self.lane)
        self.args.symbol, self.args.base, self.args.verb = None, None, "census"
        with mock.patch("abu_decomp.arity.run", return_value={}) as run:
            cli._run_arity(self.args)
        self.assertEqual(run.call_args.args[3], self.lane)
