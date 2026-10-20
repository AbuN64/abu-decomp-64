"""Profile-to-IPC contract tests; no compositor, game or subprocess is started."""

import copy
import json
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from abu_decomp import DecompError
from abu_decomp.config import load_profile
from abu_decomp.stream import apply, layout, tiles

SEAT = {
    "schema_version": 1,
    "name": "group-test",
    "stream": {"up": {"run_dir": "run", "output": {"name": "HEADLESS-1", "width": 1920, "height": 1080}}},
    "tiles": {
        "layout": [["video", "reference-video", "activity"], ["console", "reference-console", "activity"]],
        "focus": "video",
        "column_weights": [2, 1, 1],
        "row_weights": [10, 1],
        "groups": {"reference": {"enabled": False, "roles": ["reference", "reference-video", "reference-console"]}},
        "roles": {
            "video": {"overlay_app_id": "game", "pixel_size": [320, 240], "hud": {"console": "console", "height": 0}},
            "console": {"console": {}},
            "activity": {"console": {}},
            "reference": {"headless": True, "command": ["expensive-app"]},
            "reference-video": {"overlay_app_id": "reference-game", "hud": {"console": "reference-console"}},
            "reference-console": {"console": {}},
        },
    },
}


class GroupTests(unittest.TestCase):
    def profile(self, seat=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "seat.json"
        path.write_text(json.dumps(SEAT if seat is None else seat))
        return load_profile(path)

    def test_disabled_group_removes_app_and_both_terminals_before_command_generation(self):
        with patch.object(tiles, "console_command", return_value=["console"]) as command:
            up = tiles.up_config(self.profile())
        self.assertEqual(set(up["roles"]), {"video", "console", "activity"})
        self.assertEqual({call.args[1] for call in command.call_args_list}, {"video", "console", "activity"})
        self.assertEqual(up["tiles"], [["video", "activity"], ["console", "activity"]])
        self.assertEqual(up["column_weights"], [2, 1])
        self.assertEqual(up["row_weights"], [10, 1])
        self.assertEqual(up["focus_role"], "video")
        self.assertEqual(layout.overlays(up, (0, 0, 1920, 1056)), [("video", "game", 0, 0, 1280, 960)])

    def test_enabled_group_keeps_its_app_commands_and_grid(self):
        seat = copy.deepcopy(SEAT)
        seat["tiles"]["groups"]["reference"]["enabled"] = True
        up = tiles.up_config(self.profile(seat))
        self.assertEqual(up["tiles"], SEAT["tiles"]["layout"])
        self.assertEqual(up["roles"]["reference"]["command"], ["expensive-app"])
        self.assertEqual(up["column_weights"], [2, 1, 1])

    def test_one_surviving_column_and_row_are_supported(self):
        seat = copy.deepcopy(SEAT)
        seat["tiles"]["groups"]["activity"] = {"enabled": False, "roles": ["activity"]}
        up = tiles.up_config(self.profile(seat))
        self.assertEqual(layout.validate(up), (["video", "console"],))
        seat["tiles"]["groups"]["console"] = {"enabled": False, "roles": ["console"]}
        up = tiles.up_config(self.profile(seat))
        self.assertEqual(up["tiles"], [["video"]])
        self.assertEqual(up["row_weights"], [10])

    def test_partial_holes_and_disabled_focus_fail(self):
        for member in ("video", "console"):
            seat = copy.deepcopy(SEAT)
            seat["tiles"]["groups"]["partial"] = {"enabled": False, "roles": [member]}
            with self.subTest(member=member), self.assertRaises((DecompError, layout.LayoutError)):
                tiles.up_config(self.profile(seat))
        seat = copy.deepcopy(SEAT)
        seat["tiles"]["focus"] = "reference-video"
        with self.assertRaises(DecompError):
            tiles.up_config(self.profile(seat))

    def test_misspelled_roles_duplicate_groups_and_nonboolean_switch_fail(self):
        for group in (
            {"enabled": False, "roles": ["missing"]},
            {"enabled": False, "roles": ["reference"]},
            {"enabled": "false", "roles": ["video"]},
            {"enabled": False, "roles": ["video", "video"]},
        ):
            seat = copy.deepcopy(SEAT)
            seat["tiles"]["groups"]["invalid"] = group
            with self.subTest(group=group), self.assertRaises(DecompError):
                tiles.up_config(self.profile(seat))

    def test_invalid_weights_and_nonfitting_pixel_size_fail(self):
        for value in ([0, 1, 1], [True, 1, 1], [1], [1.5, 1, 1]):
            seat = copy.deepcopy(SEAT)
            seat["tiles"]["column_weights"] = value
            with self.subTest(weights=value), self.assertRaises(DecompError):
                tiles.up_config(self.profile(seat))
        up = tiles.up_config(self.profile())
        with self.assertRaises(layout.LayoutError):
            layout.overlays(up, (0, 0, 200, 100))

    def test_integer_scale_rounds_down_and_bar_origin_is_preserved(self):
        up = tiles.up_config(self.profile())
        self.assertEqual(layout.overlays(up, (0, 24, 1800, 1032)), [("video", "game", 0, 24, 960, 720)])
        self.assertEqual(layout.rectangles(up, (0, 0, 1920, 1056))["activity"], (1280, 0, 640, 1056))

    def tree(self):
        def leaf(identifier, app):
            return {"id": identifier, "app_id": app, "rect": {"x": 0, "y": 0, "width": 640, "height": 478}}

        return {
            "nodes": [
                {
                    "id": 4,
                    "type": "workspace",
                    "name": "1",
                    "rect": {"x": 0, "y": 0, "width": 1920, "height": 1056},
                    "nodes": [
                        {"id": 20, "nodes": [leaf(5, "tile-video"), leaf(9, "tile-console")]},
                        {"id": 21, "nodes": [leaf(6, "tile-reference-video"), leaf(11, "tile-reference-console")]},
                        leaf(7, "tile-activity"),
                    ],
                    "floating_nodes": [leaf(34, "game"), leaf(16, "reference-game")],
                }
            ]
        }

    def test_live_apply_only_stops_disabled_roles_and_keeps_game_and_focus(self):
        from unittest.mock import Mock

        ipc = Mock()
        ipc.tree.side_effect = lambda: self.tree()
        with patch.object(apply, "stop_disabled", return_value=["reference"]) as stop:
            result = apply.apply(self.profile(), client=ipc)
        self.assertEqual(stop.call_args.args[1], {"reference", "reference-video", "reference-console"})
        self.assertTrue(result["applied"])
        commands = [call.args[0] for call in ipc.command.call_args_list]
        self.assertEqual([c for c in commands if c.endswith(" kill")], ["[con_id=11] kill", "[con_id=6] kill"])
        self.assertIn("[con_id=20] resize set width 1280 px", commands)
        self.assertIn("[con_id=5] resize set height 960 px", commands)
        self.assertIn("[con_id=9] resize set height 96 px", commands)
        self.assertIn("[con_id=34] resize set 1280 960, move absolute position 0 0", commands)
        self.assertEqual(commands[-1], "[con_id=34] focus")

    def test_missing_enabled_window_fails_before_stopping_anything(self):
        from unittest.mock import Mock

        ipc = Mock()
        ipc.tree.return_value = {"nodes": []}
        with patch.object(apply, "stop_disabled") as stop, self.assertRaises(layout.LayoutError):
            apply.apply(self.profile(), client=ipc)
        stop.assert_not_called()
        ipc.command.assert_not_called()

    def test_live_apply_focuses_a_terminal_before_changing_its_layout(self):
        from unittest.mock import Mock

        ipc = Mock()
        ipc.tree.side_effect = lambda: self.tree()
        focused = "floating"

        def command(value):
            nonlocal focused
            if value.startswith("workspace "):
                focused = "floating"
            if value.startswith("[con_id=5] focus") or value.startswith("[con_id=7] focus"):
                focused = "terminal"
            if "layout splith" in value and focused != "terminal":
                raise RuntimeError("Unable to change layout of floating windows")

        ipc.command.side_effect = command
        with patch.object(apply, "stop_disabled", return_value=[]):
            self.assertTrue(apply.apply(self.profile(), client=ipc)["applied"])

    def test_recovery_uses_game_workspace_when_terminals_are_in_staging_workspace(self):
        from unittest.mock import Mock

        tree = self.tree()
        workspace = tree["nodes"][0]
        tree["nodes"].append(
            {
                "id": 35,
                "type": "workspace",
                "name": "__stream_apply",
                "nodes": workspace.pop("nodes"),
                "rect": {"x": 0, "y": 0, "width": 1920, "height": 1056},
            }
        )
        ipc = Mock()
        ipc.tree.return_value = tree
        with patch.object(apply, "stop_disabled", return_value=[]):
            apply.apply(self.profile(), client=ipc)
        commands = [call.args[0] for call in ipc.command.call_args_list]
        self.assertIn('workspace "1"', commands)
        self.assertIn('[con_id=5] move container to workspace "1"', commands)
        self.assertNotIn('workspace "__stream_apply"', commands)

    def test_disabled_runner_identity_is_verified_and_exit_is_event_driven(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "roles").mkdir()
            (run_dir / "roles" / "reference.pid").write_text("123")
            with (
                patch.object(apply.os, "pidfd_open", return_value=18),
                patch.object(apply.signal, "pidfd_send_signal") as send,
                patch.object(apply.select, "select", return_value=([18], [], [])) as ready,
                patch.object(apply.os, "close"),
                patch.object(Path, "read_bytes", return_value=b"python\0stream\0role\0--id\0reference\0"),
            ):
                self.assertEqual(apply.stop_disabled(run_dir, {"reference"}), ["reference"])
                send.assert_called_once_with(18, signal.SIGTERM)
                ready.assert_called_once_with([18], [], [], 5)
            with (
                patch.object(apply.os, "pidfd_open", return_value=18),
                patch.object(apply.signal, "pidfd_send_signal") as send,
                patch.object(apply.os, "close"),
                patch.object(Path, "read_bytes", return_value=b"python\0unrelated\0"),
                self.assertRaisesRegex(RuntimeError, "stale runner"),
            ):
                apply.stop_disabled(run_dir, {"reference"})
            send.assert_not_called()
