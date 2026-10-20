"""decomp ship: real git (a bare remote and a warm clone), the land itself replaced by a fake."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from abu_decomp import DecompError
from abu_decomp.config import load_profile
from abu_decomp.ship import ship


def git(root: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t"}
    env["GIT_COMMITTER_EMAIL"] = "t@t"
    return subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, text=True, check=True).stdout


class ShipTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.remote, self.tree, self.other = self.root / "remote.git", self.root / "tree", self.root / "other"
        git(self.root, "init", "-q", "--bare", "-b", "main", str(self.remote))
        git(self.root, "clone", "-q", str(self.remote), str(self.other))
        (self.other / "a.c").write_text("int a;\n")
        git(self.other, "add", ".")
        git(self.other, "commit", "-q", "-m", "base")
        git(self.other, "push", "-q", "origin", "HEAD:main")
        git(self.root, "clone", "-q", str(self.remote), str(self.tree))
        (self.tree / "build.o").write_text("warm")  # ignored build state the ship must keep
        (self.tree / ".git/info/exclude").write_text("build.o\n")
        (self.root / "host.toml").write_text("")
        (self.root / "events.jsonl").write_text("")
        sections = {
            "tools": {"project": "tree", "host": "host.toml", "timeout_seconds": 5},
            "events": {"path": "events.jsonl"},
            "land": {"check": ["make", "check"], "author": "mo <mo@example.org>"},
        }
        (self.root / "seat.json").write_text(json.dumps({"schema_version": 1, "name": "seat", **sections}))
        self.profile = load_profile(self.root / "seat.json")
        self.lands: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.before_push = lambda: None

    def landing(self, refuse: str | None = None, split: bool = False) -> Any:
        def fake(profile: Any, tree: Path, base: str, patches: Any, **_: Any) -> Any:
            self.lands.append(base)
            self.assertEqual(git(tree, "rev-parse", "HEAD").strip(), base)
            (tree / "a.c").write_text((tree / "a.c").read_text() + "int f_matched(void) { return 0; }\n")
            (tree / "new.h").write_text("/* generated */\n")
            if refuse:
                git(tree, "checkout", "-q", "--", "a.c")
                (tree / "new.h").unlink()
                return {"pass": False, "lanes": []}, 1, [{"key": refuse, "message": "no", "blocking": True}]
            self.before_push()
            plan = {"lane": "L", "files": ["a.c", "new.h"], "functions": ["f_matched"], "body": None}
            plan["message"] = "Match f_matched"
            found = [{"key": "land.file_refused", "message": "x", "blocking": True}] if split else []
            result = {"pass": True, "commits": [plan], "lanes": [{"functions": {"names": ["f_matched"]}}]}
            return result, 0, found

        return fake

    def ship(self, **fake: Any) -> Any:
        append = lambda path, record: self.events.append(record) or {"ok": True}  # noqa: E731
        return ship(
            self.profile, self.tree, [("L", self.root / "l.patch")], landing=self.landing(**fake), append=append
        )

    def remote_tip(self) -> str:
        return git(self.remote, "rev-parse", "main").strip()

    def test_a_landed_input_is_committed_as_the_author_and_pushed_fast_forward(self) -> None:
        base = self.remote_tip()
        result, code, _ = self.ship()
        self.assertEqual(code, 0)
        self.assertEqual(result["pushed"], self.remote_tip())
        self.assertEqual(git(self.remote, "rev-parse", "main~1").strip(), base)
        log = git(self.remote, "log", "-1", "--format=%an <%ae>|%cn <%ce>|%B", "main")
        self.assertEqual(log.strip(), "mo <mo@example.org>|mo <mo@example.org>|Match f_matched")
        self.assertEqual(git(self.tree, "status", "--porcelain"), "")
        self.assertTrue((self.tree / "build.o").exists())
        event = self.events[-1]
        self.assertEqual(
            (event["kind"], event["payload"]["op"], event["payload"]["functions"]), ("LAND", "shipped", ["f_matched"])
        )

    def test_a_refused_input_pushes_nothing_and_leaves_the_tree_at_the_branch(self) -> None:
        base = self.remote_tip()
        result, code, _ = self.ship(refuse="land.check")
        self.assertEqual((code, result["pushed"], result["refused"]["key"]), (1, None, "land.check"))
        self.assertEqual(self.remote_tip(), base)
        self.assertEqual(git(self.tree, "rev-parse", "HEAD").strip(), base)
        self.assertEqual(git(self.tree, "status", "--porcelain"), "")

    def test_an_input_that_lands_only_in_part_is_refused_whole(self) -> None:
        base = self.remote_tip()
        _, code, _ = self.ship(split=True)
        self.assertEqual((code, self.remote_tip()), (1, base))
        self.assertEqual(git(self.tree, "status", "--porcelain"), "")
        self.assertTrue((self.tree / "build.o").exists())

    def test_a_branch_that_moved_lands_the_input_again_on_the_new_tip(self) -> None:
        def someone_pushes() -> None:
            self.before_push = lambda: None
            (self.other / "b.c").write_text("int b;\n")
            git(self.other, "add", ".")
            git(self.other, "commit", "-q", "-m", "other")
            git(self.other, "push", "-q", "origin", "HEAD:main")

        self.before_push = someone_pushes
        _, code, _ = self.ship()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.lands), 2)
        self.assertEqual(self.lands[1], git(self.remote, "rev-parse", "main~1").strip())
        self.assertEqual(git(self.remote, "log", "-1", "--format=%s", "main").strip(), "Match f_matched")

    def test_an_input_main_already_holds_pushes_nothing(self) -> None:
        def fake(profile: Any, tree: Path, base: str, patches: Any, **_: Any) -> Any:
            plan = {"lane": "L", "files": [], "functions": [], "message": None, "body": None}
            return {"pass": True, "commits": [plan], "lanes": []}, 0, []

        base = self.remote_tip()
        result, code, _ = ship(self.profile, self.tree, [("L", self.root / "l.patch")], landing=fake)
        self.assertEqual((code, result["refused"]["key"], self.remote_tip()), (1, "ship.empty", base))

    def test_a_tree_with_tracked_changes_is_refused_and_kept(self) -> None:
        (self.tree / "a.c").write_text("work\n")
        with self.assertRaises(DecompError) as raised:
            self.ship()
        self.assertEqual(raised.exception.key, "ship.dirty")
        self.assertEqual((self.tree / "a.c").read_text(), "work\n")

    def test_two_patches_are_a_batch_and_not_a_ship(self) -> None:
        with self.assertRaises(DecompError):
            ship(self.profile, self.tree, [("A", self.root / "a"), ("B", self.root / "b")], landing=self.landing())


if __name__ == "__main__":
    unittest.main()
