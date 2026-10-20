import os
import tempfile
import unittest
from pathlib import Path

from abu_decomp.stream import services


class ServicesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run_dir = self.root / "run"
        self.old, self.new = self.root / "venv.aaaaaaaaaaaa", self.root / "venv.bbbbbbbbbbbb"
        for venv in (self.old, self.new):
            (venv / "bin").mkdir(parents=True)
            (venv / "bin" / "python").write_text("")
        self.live = self.root / "venv"
        self.live.symlink_to(self.old)
        self.pid = os.getpid()  # any live pid counts as a running unit
        self.up = {"roles": {"sup": {}, "oracle": {"player": True}}, "services": {"cycle": {}}}

    def put(self, name, venv, kind="role"):
        services.record(self.run_dir, name, self.pid, kind=kind, python=str(self.live / "bin/python"), venv=str(venv))

    def swap(self):
        self.live.unlink()
        self.live.symlink_to(self.new)

    def test_stable_python_maps_a_versioned_path_back_to_the_live_symlink(self):
        assert services.stable_python(str(self.old / "bin/python")) == str(self.live / "bin/python")
        assert services.stable_python(str(self.new / "bin/python")) == str(self.new / "bin/python")  # not live
        assert services.stable_python("/usr/bin/python3") == "/usr/bin/python3"

    def test_old_role_is_restarted_and_a_current_one_is_left_alone(self):
        self.put("sup", self.old)
        self.put("cycle", self.old, "service")
        self.swap()
        self.put("fresh", self.new)
        sent = []

        def signal_one(run_dir, up, name):
            sent.append(name)
            self.put(name, self.new)  # the unit comes back recorded on the new venv

        done, findings = services.restart_stale(
            self.run_dir, self.up, self.live, signal_one=signal_one, settle=0, sleep=lambda s: None
        )
        assert findings == [] and sent == ["cycle", "sup"]
        assert services.stale(self.run_dir, self.live) == []

    def test_player_role_is_never_restarted(self):
        self.put("oracle", self.old)
        self.swap()
        done, findings = services.restart_stale(
            self.run_dir, self.up, self.live, signal_one=lambda *a: 1 / 0, settle=0, sleep=lambda s: None
        )
        assert findings == [] and done[0]["action"] == "skipped"

    def test_unit_that_does_not_come_back_is_a_blocking_finding(self):
        self.put("sup", self.old)
        self.swap()
        done, findings = services.restart_stale(
            self.run_dir, self.up, self.live, signal_one=lambda *a: {}, wait=lambda *a: False, settle=0
        )
        assert findings[0]["key"] == "deploy.restart_failed" and findings[0]["blocking"]

    def test_held_lock_blocks_restart(self):
        self.put("sup", self.old)
        self.swap()
        up = {**self.up, "restart": {"hold_locks": ["/x"]}}
        ticks = iter(range(0, 1000, 100))
        done, findings = services.restart_stale(
            self.run_dir, up, self.live, timeout=150, signal_one=lambda *a: 1 / 0,
            held_locks=lambda paths: paths, clock=lambda: next(ticks), sleep=lambda s: None,
        )
        assert findings[0]["key"] == "deploy.restart_busy"

    def test_up_restarts_its_unit_only_when_its_own_code_changed(self):
        for venv, text in ((self.old, "a"), (self.new, "b")):
            for rel in services.UP_FILES:
                path = venv / "lib/python3.12/site-packages/abu_decomp" / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("same")
        self.put(services.UP, self.old, "up")
        self.swap()
        assert services.up_needs_restart(self.run_dir, self.live) is None
        (self.new / "lib/python3.12/site-packages/abu_decomp/stream/up.py").write_text("changed")
        assert "up.py" in services.up_needs_restart(self.run_dir, self.live)
        os.remove(self.run_dir / "units" / "stream-up.json")
        assert "no record" in services.up_needs_restart(self.run_dir, self.live)


if __name__ == "__main__":
    unittest.main()
