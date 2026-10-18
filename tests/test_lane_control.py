import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp.cli import main
from abu_decomp.lanes import control, registry

LADDER = """
ladder = ["sol", "astra", "sonnet", "opus"]
[models]
"gpt-6.1-sol" = "sol"
sonnet = "sonnet"
opus = "opus"
[kind.tooling]
min = "sonnet"
max = "opus"
opus_needs_owner = true
[kind.impl]
min = "sol"
max = "opus"
opus_needs_owner = true
"""


def run(argv):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = main(argv)
    return code, json.loads(output.getvalue())


class LaneControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "models.toml").write_text(LADDER)
        (self.root / "rules.md").write_text("LANE RULES x\n")
        self.feed = self.root / "feed.jsonl"
        profile = {
            "schema_version": 1,
            "name": "seat",
            "lanes": {
                "registry": "lanes.json",
                "ladder": "models.toml",
                "codex": "codex",
                "socket_dir": "sockets",
                "rules": "rules.md",
            },
            "events": {"path": str(self.feed)},
        }
        (self.root / "seat.json").write_text(json.dumps(profile))
        self.seat = ["--profile", str(self.root / "seat.json")]
        self.units: set[str] = set()
        self.started: list[list[str]] = []
        self.stopped: list[str] = []
        for _target, value in (
            (registry, mock.patch.object(registry, "active_units", lambda: set(self.units))),
            (
                control,
                mock.patch.object(control, "start_unit", lambda command, directory: self.started.append(command)),
            ),
            (control, mock.patch.object(control, "stop_unit", lambda unit: self.stopped.append(unit))),
        ):
            value.start()
            self.addCleanup(value.stop)

    def lane_dir(self, name):
        directory = self.root / name
        directory.mkdir()
        (directory / "brief.md").write_text("TASK\n")
        return directory

    def launch(self, name, *owns, kind="tooling", model="sonnet", extra=()):
        directory = self.lane_dir(name) if not (self.root / name).exists() else self.root / name
        argv = [
            *self.seat, "lane", "launch", "--name", name, "--kind", kind, "--model", model, "--effort", "high",
            "--track", "official", "--dir", str(directory), "--cwd", str(self.root), "--wall", "600",
            "--continues", "2", "--done-when", "result", "--fanout", "disabled", "--max-agents", "0", *extra,
        ]  # fmt: skip
        for path in owns:
            argv += ["--owns", path]
        code, envelope = run(argv)
        if code == 0:
            self.units.add(f"lane-{name}")
        return code, envelope

    def registry_digest(self):
        return hashlib.sha256((self.root / "lanes.json").read_bytes()).hexdigest()

    def test_launch_records_the_lane_and_starts_a_unit_with_the_ladder(self):
        code, envelope = self.launch("T1", "/work/tool")
        self.assertEqual(code, 0, envelope)
        command = self.started[0]
        self.assertEqual(command[:3], ["systemd-run", "--user", "--collect"])
        for flag, value in (("--unit=lane-T1", None), ("--kind", "tooling"), ("--model", "sonnet")):
            self.assertIn(flag, command)
            if value:
                self.assertEqual(command[command.index(flag) + 1], value)
        self.assertEqual(command[command.index("--ladder") + 1], str(self.root / "models.toml"))
        self.assertIn("LANE RULES", (self.root / "T1" / "brief.md").read_text())
        row = json.loads((self.root / "lanes.json").read_text())["lanes"][0]
        self.assertEqual(
            (row["name"], row["state"], row["owns"], row["rung"]), ("T1", "live", ["/work/tool"], "sonnet")
        )
        self.assertEqual(json.loads(self.feed.read_text())["lane"]["name"], "T1")

    def test_ladder_refusals_and_owner_override(self):
        for kind, model in (("tooling", "gpt-6.1-sol"), ("tooling", "opus"), ("impl", "opus")):
            with self.subTest(kind=kind, model=model):
                code, envelope = self.launch(f"L-{kind}-{model}", "/work/x", kind=kind, model=model)
                self.assertEqual((code, envelope["findings"][0]["key"]), (1, "lanes.ladder"))
        self.assertEqual(self.started, [])
        code, _ = self.launch(
            "L-ok", "/work/y", kind="tooling", model="opus", extra=("--owner-override", "owner 10-18")
        )
        self.assertEqual(code, 0)
        row = json.loads((self.root / "lanes.json").read_text())["lanes"][0]
        self.assertEqual(row["owner_override"], "owner 10-18")

    def test_overlap_guard_refuses_shared_paths_relative_paths_same_name_and_active_unit(self):
        self.assertEqual(self.launch("A", "/work/decomp/src/game")[0], 0)
        for name, path in (("B", "/work/decomp/src"), ("C", "/work/decomp/src/game/x"), ("D", "rel/path")):
            with self.subTest(name=name):
                code, envelope = self.launch(name, path)
                self.assertEqual((code, envelope["findings"][0]["key"]), (1, "lanes.overlap"))
        code, envelope = self.launch("A", "/work/other")
        self.assertIn("already live", envelope["findings"][0]["message"])
        self.units.add("lane-OLD")  # an old-launched lane has no registry row but its unit is active
        code, envelope = self.launch("OLD", "/work/other")
        self.assertIn("unit lane-OLD is already active", envelope["findings"][0]["message"])
        code, envelope = run([*self.seat, "lane", "launch", "--name", "N", "--kind", "tooling", "--model", "sonnet",
                              "--effort", "high", "--track", "official", "--dir", str(self.lane_dir("N")),
                              "--cwd", str(self.root), "--wall", "5", "--continues", "0", "--done-when", "result",
                              "--fanout", "disabled", "--max-agents", "0"])  # fmt: skip
        self.assertNotEqual(code, 0)  # owns is required
        self.assertEqual(len(self.started), 1)

    def test_only_launch_and_end_write_the_registry(self):
        self.launch("A", "/work/a")
        before = self.registry_digest()
        transcript = self.root / "A" / "transcript.jsonl"
        transcript.write_text("")
        run([*self.seat, "lane", "steer", "--name", "A", str(self.root / "missing.txt")])
        self.assertEqual(self.registry_digest(), before)
        code, envelope = run([*self.seat, "lane", "end", "--name", "A", "--reason", "done"])
        self.assertEqual((code, envelope["findings"][0]["key"]), (1, "lanes.live"))
        self.assertEqual(self.registry_digest(), before)
        code, _ = run([*self.seat, "lane", "end", "--name", "A", "--reason", "done", "--stop"])
        self.assertEqual((code, self.stopped), (0, ["lane-A"]))
        self.assertEqual(json.loads((self.root / "lanes.json").read_text())["lanes"][0]["state"], "ended")
        self.units.clear()
        self.assertEqual(self.launch("B", "/work/a")[0], 0)  # an ended lane frees its paths

    def test_a_dead_unit_does_not_block_a_launch(self):
        self.launch("A", "/work/a")
        self.units.clear()
        self.assertEqual(self.launch("B", "/work/a")[0], 0)
        rows = json.loads((self.root / "lanes.json").read_text())["lanes"]
        self.assertEqual([row["state"] for row in rows], ["ended", "live"])


if __name__ == "__main__":
    unittest.main()
