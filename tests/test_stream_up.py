import io
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from abu_decomp.stream import audio, control, layout
from abu_decomp.stream.inotify import wait_for_path
from abu_decomp.stream.supervise import FatalUnitExit, Group, RoleRunner, Unit

UP = {
    "output": {"name": "HEADLESS-1", "width": 1920, "height": 1080},
    "tiles": [["a", "b"], ["c", "d"]],
    "roles": {"a": {"overlay_app_id": "game"}, "b": {}, "c": {}, "d": {}},
}


class FakeIpc:
    def __init__(self):
        self.commands = []

    def command(self, value):
        self.commands.append(value)


class LayoutTests(unittest.TestCase):
    def test_validate_rejects_bad_grids(self):
        with self.assertRaises(layout.LayoutError):
            layout.validate({**UP, "tiles": [["a", "b"]]})
        with self.assertRaises(layout.LayoutError):
            layout.validate({**UP, "tiles": [["a", "a"], ["c", "d"]]})
        with self.assertRaises(layout.LayoutError):
            layout.validate({**UP, "tiles": [["a", "b"], ["c", "zz"]]})

    def test_config_declares_output_and_overlay_over_its_tile(self):
        text = layout.sway_config(UP)
        assert "output HEADLESS-1 mode 1920x1080" in text
        assert 'for_window [app_id="^game$"]' in text
        assert "floating enable, border none, move absolute position 0 0" in text

    def test_overlay_of_bottom_right_tile_is_offset(self):
        up = {**UP, "roles": {**UP["roles"], "d": {"overlay_app_id": "x"}}}
        assert "position 960 540" in layout.sway_config(up)

    def test_tree_is_built_in_fixed_order(self):
        ipc, spawned = FakeIpc(), []
        layout.build_tree(ipc, UP, spawned.append, lambda wanted: spawned.append("wait " + wanted))
        assert spawned == [
            "a",
            "wait tile-a",
            "b",
            "wait tile-b",
            "c",
            "wait tile-c",
            "d",
            "wait tile-d",
        ]
        assert ipc.commands == [
            '[app_id="^tile\\-a$"] focus; splitv',
            '[app_id="^tile\\-b$"] focus; splitv',
        ]


class OverlayTests(unittest.TestCase):
    def test_window_off_its_tile_gets_one_fix_and_a_placed_one_none(self):
        def tree(rect):
            return {"nodes": [{"app_id": "game", "id": 9, "rect": rect, "nodes": []}], "floating_nodes": []}

        off = {"x": 5, "y": 5, "width": 1280, "height": 960}
        assert layout.overlay_fixes(tree(off), UP) == ["[con_id=9] resize set 960 540, move absolute position 0 0"]
        assert layout.overlay_fixes(tree({"x": 0, "y": 0, "width": 960, "height": 540}), UP) == []


class BarFocusAudioTests(unittest.TestCase):
    def test_bar_runs_the_status_command_and_is_optional(self):
        up = {**UP, "bar": {"height": 20}}
        text = layout.sway_config(up, ["python", "-m", "x", "status line"])
        assert "status_command python -m x 'status line'" in text and "height 20" in text
        assert "bar {" not in layout.sway_config(UP)

    def test_overlay_geometry_follows_workspace_minus_bar(self):
        workspace = {"type": "workspace", "name": "1", "rect": {"x": 0, "y": 0, "width": 1920, "height": 1056}}
        workspace["nodes"] = [{"app_id": "game", "id": 4, "rect": {"x": 0, "y": 0, "width": 9, "height": 9}}]
        tree = {"type": "root", "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080}, "nodes": [workspace]}
        assert layout.overlay_fixes(tree, UP) == ["[con_id=4] resize set 960 528, move absolute position 0 0"]

    def test_focus_goes_to_the_default_role_window_when_it_appears(self):
        up = {**UP, "focus_role": "a"}
        new = {"change": "new", "container": {"app_id": "game", "id": 7}}
        assert layout.focus_command(new, up) == "[con_id=7] focus"
        assert layout.focus_command({"change": "title", "container": {"app_id": "game", "id": 7}}, up) is None
        assert layout.focus_command({"change": "new", "container": {"app_id": "other", "id": 8}}, up) is None
        assert layout.focus_command(new, UP) is None

    def test_sink_is_created_only_when_missing(self):
        calls = []

        def run(argv, **_kw):
            calls.append(argv)
            return SimpleNamespace(stdout="1\tm2\tmodule\n")

        assert audio.ensure_sink("m2", {}, run) is False
        assert audio.ensure_sink("other", {}, run) is True
        assert calls[-1][:3] == ["pactl", "load-module", "module-null-sink"]


class SuperviseTests(unittest.TestCase):
    def test_exited_unit_is_restarted_after_delay(self):
        group = Group()
        unit = Unit("quick", ["/bin/sh", "-c", "exit 0"], {}, 0.05)
        group.start(unit)
        try:
            while unit.starts < 3:
                group.step(max_wait=2)
        finally:
            group.stop()
        assert unit.starts >= 3

    def test_watched_fd_runs_handler_in_the_loop(self):
        import os

        group, seen = Group(), []
        read, write = os.pipe()
        group.watch(read, lambda: seen.append(os.read(read, 8)))
        os.write(write, b"x")
        group.step(max_wait=2)
        assert seen == [b"x"]

    def test_fatal_unit_raises(self):
        group = Group()
        group.start(Unit("sway", ["/bin/sh", "-c", "exit 7"], fatal=True))
        try:
            with self.assertRaises(FatalUnitExit) as caught:
                group.step(max_wait=5)
        finally:
            group.stop()
        assert caught.exception.code == 7

    def test_unit_without_restart_seconds_runs_once_and_is_not_fatal(self):
        group = Group()
        unit = Unit("role", ["/bin/sh", "-c", "exit 3"])
        group.start(unit)
        try:
            assert group.step(max_wait=5) == ["role"]
            group.step(max_wait=0.2)
        finally:
            group.stop()
        assert unit.starts == 1


class RoleRunnerTests(unittest.TestCase):
    def runner(self, script):
        return RoleRunner(["/bin/sh", "-c", script], title="r", out=io.StringIO())

    def test_exit_is_held_not_restarted_and_restart_runs_again(self):
        runner = self.runner("echo hi; exit 4")
        runner.start()
        runner.child.wait()
        assert runner.reap() and runner.starts == 1
        assert "exited rc=4; held" in runner.out.getvalue()
        assert not runner.reap()
        runner.restart()
        runner.child.wait()
        assert runner.starts == 2

    def test_stop_terminates_the_whole_process_group_and_holds(self):
        runner = self.runner("sleep 60 & wait")
        runner.start()
        child = runner.child
        runner.stop()
        assert child.poll() is not None and runner.child is None
        assert "stopped on request" in runner.out.getvalue()

    def test_signals_drive_a_live_runner(self):
        import os
        import signal

        runner = self.runner("sleep 60")
        outcome = {}

        def until(check):
            for _ in range(250):
                if check():
                    return True
                threading.Event().wait(0.02)
            return False

        def driver():
            outcome["started"] = until(lambda: runner.starts == 1)
            os.kill(os.getpid(), signal.SIGUSR1)
            outcome["restarted"] = until(lambda: runner.starts == 2)
            os.kill(os.getpid(), signal.SIGUSR2)
            outcome["stopped"] = until(lambda: runner.child is None)
            os.kill(os.getpid(), signal.SIGTERM)

        thread = threading.Thread(target=driver)
        thread.start()
        runner.serve()
        thread.join(5)
        assert outcome == {"started": True, "restarted": True, "stopped": True}


class ControlTests(unittest.TestCase):
    def test_key_tokens_become_wtype_arguments(self):
        assert control.wtype_args(["ctrl+c"]) == ["-M", "ctrl", "-k", "c", "-m", "ctrl"]
        assert control.wtype_args(["F5", "Return"]) == ["-k", "F5", "-k", "Return"]
        assert control.wtype_args(["hello"]) == ["--", "hello"]
        assert control.wtype_args(["+"]) == ["--", "+"]

    def test_restart_and_stop_signal_the_runner_pid(self):
        import os
        import signal

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "roles").mkdir()
            control.pidfile(run_dir, "a").write_text(str(os.getpid()))
            seen = []
            previous = signal.signal(signal.SIGUSR1, lambda n, _f: seen.append(n))
            try:
                assert control.restart(run_dir, "a")["runner"] == os.getpid()
            finally:
                signal.signal(signal.SIGUSR1, previous)
            assert seen == [signal.SIGUSR1]
            with self.assertRaises(ValueError):
                control.stop(run_dir, "missing")

    def test_send_focuses_role_window_types_then_restores_default_focus(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "sway-ipc.1.2.sock").touch()
            (run_dir / "wayland-1").touch()
            calls = []

            def run(argv, **_kw):
                calls.append(argv)
                return SimpleNamespace(returncode=0, stdout='[{"success": true}]')

            up = {**UP, "focus_role": "b"}
            result = control.send(run_dir, up, "a", ["F5"], run)
            assert result["window"] == "game"
            focus = [c[-1] for c in calls if c[0] == "swaymsg"]
            assert focus == ['[app_id="^game$"] focus', '[app_id="^tile\\-b$"] focus']
            assert ["wtype", "-k", "F5"] in calls


class WaitTests(unittest.TestCase):
    def test_wait_for_path_wakes_on_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sock"
            threading.Timer(0.1, target.touch).start()
            assert wait_for_path(target, 5)
            assert not wait_for_path(Path(tmp) / "never", 0.1)


if __name__ == "__main__":
    unittest.main()
