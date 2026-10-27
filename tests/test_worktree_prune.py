"""prune: an ended lane's worktrees and scratch go, a lane a process uses or a successor needs stays."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp import worktree


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class PruneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="prune-")).resolve()
        self.wt = self.root / "wt"
        self.wt.mkdir()
        self.shared = self.wt / "POOL-SRC"
        self.shared.mkdir()
        git("init", "-q", "-b", "main", cwd=self.shared)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x", cwd=self.shared)
        self.scratch = self.root / "lanes"
        self.scratch.mkdir()
        patcher = mock.patch.object(worktree, "_live_paths", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def add(self, name: str) -> Path:
        git("worktree", "add", "-q", "--detach", str(self.wt / name), cwd=self.shared)
        return self.wt / name

    def test_done_lanes_only_are_ended(self) -> None:
        results = self.root / "results"
        for lane, first in (("a", "DONE x"), ("b", "CONTINUE"), ("c", "BLOCKED"), ("d", "PARTIAL")):
            (results / lane).mkdir(parents=True)
            (results / lane / "RESULT.md").write_text(first + "\n")
        self.assertEqual(worktree.ended_lanes(results), {"a"})

    def test_scratch_is_the_lanes_folder_tool_folders_and_loose_files_not_a_longer_lane(self) -> None:
        for d in ("L1", "L1-decomp", "L12", "L1-sub", "L2"):
            (self.scratch / d).mkdir()
        for f in ("L1.log", "L1-make.log", "L1-sub.log", "L12.log", "other.log"):
            (self.scratch / f).write_text("x")
        got = sorted(p.name for p in worktree.lane_scratch(self.scratch, "L1"))
        self.assertEqual(got, ["L1", "L1-decomp", "L1-make.log", "L1.log"])  # L1-sub is its own lane's folder

    def test_prune_removes_trees_and_scratch_and_frees_the_lane(self) -> None:
        tree, tool = self.add("E"), self.add("E-decomp")
        (self.scratch / "E").mkdir()
        (self.scratch / "E" / "big").write_text("x" * 1000)
        (self.scratch / "E.log").write_text("log")
        (self.scratch / "LIVE").mkdir()
        live = self.add("LIVE")
        result, code, _ = worktree.prune(self.shared, ["E"], None, [self.scratch])
        self.assertEqual(code, 0)
        self.assertFalse(
            tree.exists() or tool.exists() or (self.scratch / "E").exists() or (self.scratch / "E.log").exists()
        )
        self.assertTrue(live.exists() and (self.scratch / "LIVE").exists())
        self.assertEqual(len(result["removed"]), 4)

    def test_a_path_a_process_stands_in_or_has_open_stays(self) -> None:
        tree = self.add("BUSY")
        (self.scratch / "BUSY").mkdir()
        live = [str(self.scratch / "BUSY" / "out.log")]  # an open file under the scratch folder
        with mock.patch.object(worktree, "_live_paths", return_value=[*live, str(tree)]):
            result, _, findings = worktree.prune(self.shared, ["BUSY"], None, [self.scratch])
        self.assertTrue(tree.exists() and (self.scratch / "BUSY").exists())
        self.assertEqual(len(result["kept"]), 2)
        self.assertTrue(all(f["key"] == "worktree.referenced" for f in findings))

    def test_uncommitted_work_is_saved_as_a_patch_before_the_tree_goes(self) -> None:
        tree = self.add("DIRTY")
        (tree / "new.c").write_text("int x;\n")
        (tree / "build").mkdir()
        (tree / "build" / "junk.o").write_text("o")
        salvage = self.root / "salvage"
        result, _, _ = worktree.prune(self.shared, ["DIRTY"], None, None, salvage)
        self.assertFalse(tree.exists())
        patch = salvage / "DIRTY.patch"
        self.assertEqual(result["salvaged"], [str(patch)])
        self.assertIn("new.c", patch.read_text())
        self.assertNotIn("junk.o", patch.read_text())

    def test_a_clean_tree_leaves_no_patch(self) -> None:
        self.add("CLEAN")
        salvage = self.root / "salvage"
        result, _, _ = worktree.prune(self.shared, ["CLEAN"], None, None, salvage)
        self.assertEqual(result["salvaged"], [])
        self.assertFalse(salvage.exists())


if __name__ == "__main__":
    unittest.main()
