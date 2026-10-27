"""The scheduled reap: idle orphaned browser trees and ended lanes' worktrees go, live ones stay."""

import os
import signal
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from abu_decomp import DecompError, reap

REAP = {
    "worktree_idle_hours": 6,
    "chromium_sample_seconds": 3,
    "chromium_min_age_minutes": 10,
    "chromium_idle_cpu_percent": 5,
    "interval_minutes": 30,
}


def profile(section=REAP):
    land = {"reap": section} if section is not None else {}
    return SimpleNamespace(section=lambda name: land, source_file=Path("/seat/rig.json"))


def proc(pid, ppid, comm="chromium", args="/usr/bin/chromium --headless", cwd="/", ticks=10, age=7200.0):
    return reap.Proc(pid, ppid, comm, args, cwd, ticks, age)


class BrowserTests(unittest.TestCase):
    def test_an_idle_orphan_tree_goes_and_a_busy_or_young_or_parented_one_stays(self) -> None:
        def tree(root, ticks=10, age=7200.0, parent=1):
            return [
                proc(root, parent, age=age, ticks=ticks),
                proc(root + 1, root, args="/usr/bin/chromium --type=renderer", ticks=ticks, age=age),
            ]

        before = tree(100) + tree(200) + tree(300) + tree(400, parent=50) + [proc(50, 1, comm="node", args="node x")]
        after = (
            tree(100, ticks=11)  # a little CPU, under the idle share
            + tree(200, ticks=30)  # spent more than 5% of a core in the window
            + tree(300, age=60.0)  # younger than the minimum age
            + tree(400, parent=50)  # a live lane's browser: its parent is not init
            + [proc(50, 1, comm="node", args="node x")]
            + [proc(500, 1)]  # started inside the window
            + [proc(600, 1, comm="chrome_crashpad", args="/usr/lib/chromium/chrome_crashpad_handler")]
        )
        found = reap.idle_browsers(before, after, 600, 3, 5)
        self.assertEqual([[p.pid for p in t] for t in found], [[100, 101]])

    def test_the_user_manager_counts_as_an_orphan_parent(self) -> None:
        manager = proc(7, 1, comm="systemd", args="systemd --user")
        both = [manager, proc(70, 7)]
        self.assertEqual(len(reap.idle_browsers(both, both, 600, 3, 5)), 1)


class SnapshotTests(unittest.TestCase):
    def test_age_counts_from_the_boot_time_not_the_container_uptime(self) -> None:
        proc_dir = Path(tempfile.mkdtemp(prefix="reap-proc-"))
        hertz = os.sysconf("SC_CLK_TCK")
        boot = int(time.time()) - 100_000
        (proc_dir / "stat").write_text(f"cpu 1 2 3\nbtime {boot}\n")
        (proc_dir / "uptime").write_text("50.00 40.00\n")  # a container's own uptime must not matter
        entry = proc_dir / "42"
        entry.mkdir()
        fields = [
            "S",
            "1",
            *["0"] * 9,
            "5",
            "7",
            *["0"] * 6,
            str(90_000 * hertz),
        ]  # state, ppid, ... utime stime ... start
        (entry / "stat").write_text("42 (chromium) " + " ".join(fields) + " 0 0\n")
        (entry / "cmdline").write_bytes(b"/usr/lib/chromium/chromium\0--headless\0")
        [found] = reap.snapshot(proc_dir)
        self.assertEqual((found.pid, found.ppid, found.comm, found.ticks), (42, 1, "chromium", 12))
        self.assertAlmostEqual(found.age, 10_000, delta=5)


class WorktreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="reap-"))
        self.shared = self.root / "shared"
        self.shared.mkdir()
        self.wt = self.root / "wt"
        for name in ("ended", "live", "fresh", "queued"):
            (self.wt / name).mkdir(parents=True)
        old = 1_000_000.0
        for name in ("ended", "live", "queued"):
            os.utime(self.wt / name, (old, old))
        self.now = old + 7 * 3600
        os.utime(self.wt / "fresh", (self.now - 60, self.now - 60))
        patcher = mock.patch.object(
            reap.worktree, "_referenced", side_effect=lambda s, p: ["queued ship input"] if p.name == "queued" else []
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_an_aged_unheld_unqueued_worktree_is_stale(self) -> None:
        holder = proc(9, 1, comm="make", args="make", cwd=str(self.wt / "live" / "build"))
        stale = reap.stale_worktrees(self.shared, [holder], 6, self.now, os.getpid())
        self.assertEqual(stale, ["ended"])

    def test_run_dry_reports_and_changes_nothing_then_run_kills_and_prunes(self) -> None:
        idle = [proc(100, 1), proc(101, 100, args="/usr/bin/chromium --type=gpu")]
        kills, pruned, slept = [], [], []
        common = dict(
            read=lambda: idle,
            kill=lambda pid, sig: kills.append((pid, sig)),
            sleep=slept.append,
            now=lambda: self.now,
        )
        with mock.patch.object(
            reap.worktree, "prune", side_effect=lambda s, n, *a: pruned.append(n) or ({"removed": n}, 0, [])
        ):
            dry, code, _ = reap.run(profile(), self.shared, dry_run=True, **common)
            self.assertEqual((kills, pruned, code), ([], [], 0))
            self.assertEqual(dry["chromium"], {"trees": 1, "processes": 2})
            self.assertEqual(dry["worktrees"]["stale"], ["ended", "live"])
            real, _, _ = reap.run(profile(), self.shared, **common)
        self.assertEqual(pruned, [["ended", "live"]])  # no process stands in them in this snapshot
        self.assertIn((100, signal.SIGTERM), kills)
        self.assertIn((101, signal.SIGKILL), kills)
        self.assertEqual(real["worktrees"]["removed"], ["ended", "live"])
        self.assertEqual(slept[0], 3)  # the sample window comes from the profile

    def test_an_ended_lane_goes_with_its_scratch_even_when_it_is_not_idle(self) -> None:
        results = Path(tempfile.mkdtemp(prefix="results-"))
        (results / "fresh").mkdir()
        (results / "fresh" / "RESULT.md").write_text("DONE shipped\n")
        (results / "next").mkdir()
        (results / "next" / "RESULT.md").write_text("CONTINUE\n")
        section = {**REAP, "ended_results": str(results), "lane_scratch": ["/rig/lanes"], "salvage": "/rig/salvage"}
        calls = []
        with mock.patch.object(reap.worktree, "prune", side_effect=lambda *a: calls.append(a) or ({}, 0, [])):
            reap.run(profile(section), self.shared, read=lambda: [], sleep=lambda s: None, now=lambda: self.now)
        _, names, results_arg, scratch, salvage = calls[0]
        self.assertEqual(names, ["ended", "fresh", "live"])  # idle ones plus the DONE one; CONTINUE stays
        self.assertEqual((results_arg, scratch, salvage), (None, [Path("/rig/lanes")], Path("/rig/salvage")))


class ScheduleTests(unittest.TestCase):
    def test_a_profile_without_reap_figures_is_refused_naming_them(self) -> None:
        with self.assertRaisesRegex(DecompError, "interval_minutes"):
            reap.settings(profile({"worktree_idle_hours": 6}))
        with self.assertRaises(DecompError):
            reap.settings(profile(None))

    def test_enable_writes_the_timer_and_starts_it_and_disable_removes_it(self) -> None:
        folder = Path(tempfile.mkdtemp(prefix="reap-units-"))
        with mock.patch.object(reap, "_systemctl") as ctl:
            document, code, _ = reap.enable(profile(), Path("/rig/tree"), folder)
            self.assertEqual((document["enabled"], document["every_minutes"], code), ("decomp-reap.timer", 30, 0))
            self.assertIn("enable", ctl.call_args.args)
            self.assertIn("worktree --tree /rig/tree reap", (folder / "decomp-reap.service").read_text())
            self.assertIn("OnUnitActiveSec=30min", (folder / "decomp-reap.timer").read_text())
            reap.disable(folder)
        self.assertEqual(list(folder.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
