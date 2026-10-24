import subprocess
import tempfile
import unittest
from pathlib import Path

from abu_decomp.behave import candidate as cand


def git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin",
        },
    )


class CandidateTreeTests(unittest.TestCase):
    def test_a_candidate_in_a_sibling_worktree_compiles_against_that_tree(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            main, lane, other = root / "main", root / "lane", root / "other"
            main.mkdir()
            git(main, "init", "-q", "-b", "main")
            (main / "a.c").write_text("x\n")
            git(main, "add", "a.c")
            git(main, "-c", "commit.gpgsign=false", "commit", "-qm", "one")
            git(main, "worktree", "add", "-q", str(lane), "-b", "lane")
            other.mkdir()
            git(other, "init", "-q")
            (lane / "src").mkdir()
            (other / "c.c").write_text("y\n")
            self.assertEqual(cand.candidate_tree(lane / "src" / "f.c", main).resolve(), lane.resolve())
            self.assertIsNone(cand.candidate_tree(main / "a.c", main))  # the seat's own tree
            self.assertIsNone(cand.candidate_tree(other / "c.c", main))  # another repository
            self.assertIsNone(cand.candidate_tree(root / "nowhere" / "f.c", main))  # no git tree at all


if __name__ == "__main__":
    unittest.main()
