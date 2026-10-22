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

    def batch(self, split: str | None = None, broken: str | None = None) -> Any:
        """A land of several inputs: each lane adds its own file and gets its own commit; `split` lands only in part
        (a file refused), `broken` is the input the land's build bisect refuses."""
        self.calls: list[list[str]] = []

        def fake(profile: Any, tree: Path, base: str, patches: Any, **_: Any) -> Any:
            lanes = [lane for lane, _ in patches]
            self.calls.append(lanes)
            found = []
            if split in lanes:
                found.append({"key": "land.file_refused", "message": "x.c conflicts", "blocking": True, "lane": split})
            plan, rows, refused = [], [], []
            for lane in lanes:
                if lane == broken:
                    refused.append({"lane": lane, "reason": "land.check: make exited 2", "builds": 3})
                    rows.append({"lane": lane, "status": "applied", "functions": {"names": []}})
                    continue
                (tree / f"{lane}.c").write_text(f"int {lane}_f(void) {{ return 0; }}\n")
                plan.append({"lane": lane, "files": [f"{lane}.c"], "message": f"Match {lane}_f", "body": None})
                rows.append({"lane": lane, "status": "applied", "functions": {"names": [f"{lane}_f"]}})
            if broken:
                found.append({"key": "land.input_build_failed", "message": "B", "blocking": True, "lane": broken})
            return {"pass": True, "commits": plan, "lanes": rows, "refused_inputs": refused}, 0, found

        return fake

    def ship_all(self, lanes: list[str], **fake: Any) -> Any:
        append = lambda path, record: self.events.append(record) or {"ok": True}  # noqa: E731
        patches = [(lane, self.root / f"{lane}.patch") for lane in lanes]
        return ship(self.profile, self.tree, patches, landing=self.batch(**fake), append=append)

    def test_every_ready_input_lands_in_one_round_and_one_push_with_a_commit_each(self) -> None:
        base = self.remote_tip()
        result, code, _ = self.ship_all(["A", "B", "C"])
        self.assertEqual((code, self.calls), (0, [["A", "B", "C"]]))  # one land: one setup, one check
        self.assertEqual(git(self.remote, "log", "--format=%s", f"{base}..main").split("\n")[:3], [
            "Match C_f", "Match B_f", "Match A_f"])  # fmt: skip
        self.assertEqual([r["status"] for r in result["inputs"]], ["shipped"] * 3)
        self.assertEqual(len([s for s in result["steps"] if s["step"] == "push"]), 1)
        self.assertEqual(self.events[-1]["payload"]["inputs"][1], {"lane": "B", "status": "shipped", "key": None})

    def test_an_input_that_lands_only_in_part_is_refused_whole_and_the_rest_land_again(self) -> None:
        result, code, findings = self.ship_all(["A", "B", "C"], split="B")
        self.assertEqual((code, self.calls), (0, [["A", "B", "C"], ["A", "C"]]))
        self.assertEqual([r["status"] for r in result["inputs"]], ["shipped", "refused", "shipped"])
        self.assertEqual(result["inputs"][1]["key"], "land.file_refused")
        self.assertFalse((self.tree / "B.c").exists())
        self.assertFalse([f for f in findings if f.get("blocking")])
        self.assertEqual(git(self.tree, "status", "--porcelain"), "")

    def test_the_input_the_build_bisect_refuses_stays_out_and_the_rest_ship(self) -> None:
        result, code, findings = self.ship_all(["A", "B", "C"], broken="B")
        self.assertEqual(code, 0)
        self.assertEqual([r["status"] for r in result["inputs"]], ["shipped", "refused", "shipped"])
        self.assertEqual(result["inputs"][1]["key"], "land.input_build_failed")
        self.assertFalse([f for f in findings if f.get("blocking")])
        self.assertIn("refused B (land.input_build_failed)", self.events[-1]["message"])

    def test_every_input_landing_only_in_part_pushes_nothing(self) -> None:
        base = self.remote_tip()
        result, code, _ = self.ship_all(["B"], split="B")
        self.assertEqual((code, result["pushed"], self.remote_tip()), (1, None, base))
        self.assertEqual(git(self.tree, "status", "--porcelain"), "")

    def test_two_inputs_with_one_lane_name_are_refused(self) -> None:
        with self.assertRaises(DecompError):
            ship(self.profile, self.tree, [("A", self.root / "a"), ("A", self.root / "b")], landing=self.landing())


if __name__ == "__main__":
    unittest.main()


class ShipWorktreeTests(unittest.TestCase):
    """`worktree=NAME`: the tree's own tracked changes are the input; the fake land applies the captured patch."""

    setUp = ShipTests.setUp
    remote_tip = ShipTests.remote_tip

    def landing(self, refuse: str | None = None, split: bool = False) -> Any:
        def fake(profile: Any, tree: Path, base: str, patches: Any, **_: Any) -> Any:
            self.lands.append(base)
            self.assertEqual(git(tree, "status", "--porcelain", "--untracked-files=no"), "")
            if refuse:
                return {"pass": False, "lanes": []}, 1, [{"key": refuse, "message": "no", "blocking": True}]
            git(tree, "apply", str(patches[0][1]))
            plan = {"lane": patches[0][0], "files": ["a.c"], "functions": ["f"], "body": None, "message": "Fix a"}
            return {"pass": True, "commits": [plan], "lanes": [{"functions": {"names": ["f"]}}]}, 0, []

        return fake

    def work(self, **fake: Any) -> Any:
        append = lambda path, record: self.events.append(record) or {"ok": True}  # noqa: E731
        return ship(self.profile, self.tree, [], worktree="W", landing=self.landing(**fake), append=append)

    def snapshot(self) -> tuple[str, str, str]:
        return (
            git(self.tree, "diff", "HEAD", "--binary"),
            git(self.tree, "rev-parse", "HEAD").strip(),
            git(self.tree, "symbolic-ref", "-q", "HEAD").strip(),
        )

    def test_the_trees_own_change_is_gated_committed_and_pushed_and_the_tree_ends_clean(self) -> None:
        (self.tree / "a.c").write_text("int a2;\n")
        (self.tree / "scratch.txt").write_text("untracked stays\n")
        result, code, _ = self.work()
        self.assertEqual(code, 0)
        self.assertEqual(result["pushed"], self.remote_tip())
        self.assertEqual(git(self.remote, "show", "main:a.c"), "int a2;\n")
        self.assertEqual(git(self.tree, "status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual(git(self.tree, "rev-parse", "HEAD").strip(), result["pushed"])
        self.assertEqual(git(self.tree, "symbolic-ref", "-q", "HEAD").strip(), "refs/heads/main")
        self.assertTrue((self.tree / "scratch.txt").exists() and (self.tree / "build.o").exists())

    def test_a_refusal_leaves_the_tree_exactly_as_it_was(self) -> None:
        (self.tree / "a.c").write_text("int a2;\n")
        before = self.snapshot()
        result, code, _ = self.work(refuse="land.check")
        self.assertEqual((code, result["pushed"]), (1, None))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.tree / "a.c").read_text(), "int a2;\n")

    def test_changes_that_do_not_apply_on_the_new_tip_are_refused_and_kept(self) -> None:
        (self.other / "a.c").write_text("int other;\n")
        git(self.other, "commit", "-q", "-am", "other")
        git(self.other, "push", "-q", "origin", "HEAD:main")
        (self.tree / "a.c").write_text("int mine;\n")
        before = self.snapshot()
        with self.assertRaises(DecompError) as raised:
            self.work()
        self.assertEqual(raised.exception.key, "ship.worktree_conflict")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.lands, [])


class ShipBoostTests(unittest.TestCase):
    """The ship lock and the CPU/IO boost of its holder are one rule: the unit holding the lock outranks the lanes."""

    setUp = ShipTests.setUp
    landing = ShipTests.landing

    SCOPE = "0::/user.slice/user-1000.slice/user@1000.service/decomp.slice/ssh-1-2.scope\n"

    def boosted(self, weight: Any, cgroup: Any, answer: tuple[int, str, str] = (0, "", "")) -> Any:
        from abu_decomp import land

        seat = json.loads((self.root / "seat.json").read_text())
        seat["land"].pop("lock_weight", None)
        if weight is not None:
            seat["land"]["lock_weight"] = weight
        (self.root / "seat.json").write_text(json.dumps(seat))
        profile, calls = load_profile(self.root / "seat.json"), []

        def run(argv: Any, cwd: Path, env: Any, timeout: Any) -> Any:
            if argv[0] == "systemctl":
                calls.append(list(argv))
                return answer
            return land.run_process(argv, cwd, env, timeout)

        got = ship(profile, self.tree, [("L", self.root / "l.patch")], landing=self.landing(), run=run, cgroup=cgroup)
        return got, calls

    def test_the_lock_holder_gets_the_profiles_weight_on_its_own_scope(self) -> None:
        (result, code, findings), calls = self.boosted(1000, lambda: self.SCOPE)
        self.assertEqual(code, 0)
        self.assertEqual(
            calls,
            [["systemctl", "--user", "set-property", "--runtime", "ssh-1-2.scope", "CPUWeight=1000", "IOWeight=1000"]],
        )
        self.assertIn({"step": "boost", "seconds": 0.0, "unit": "ssh-1-2.scope", "weight": 1000}, result["steps"])
        self.assertFalse([f for f in findings if f["key"] == "ship.boost"])
        self.assertEqual(os.environ.pop("UNBAKE_PRIORITY"), "1")  # the land's unbake calls take pool slots first

    def test_a_refused_property_call_is_a_finding_and_the_ship_still_runs(self) -> None:
        (_, code, findings), _ = self.boosted(1000, lambda: self.SCOPE, (1, "", "Failed to connect to bus"))
        self.assertEqual(code, 0)
        boost = [f for f in findings if f["key"] == "ship.boost"]
        self.assertEqual(len(boost), 1)
        self.assertIn("Failed to connect to bus", boost[0]["message"])
        self.assertFalse(boost[0]["blocking"])

    def test_no_unit_or_no_weight_is_a_finding_never_a_silent_pass(self) -> None:
        (_, _, found), calls = self.boosted(1000, lambda: "0::/\n")
        self.assertEqual(calls, [])
        self.assertIn("no systemd scope or service", next(f for f in found if f["key"] == "ship.boost")["message"])
        (_, _, found), calls = self.boosted(None, lambda: self.SCOPE)
        self.assertEqual(calls, [])
        self.assertIn("lock_weight is not set", next(f for f in found if f["key"] == "ship.boost")["message"])

    def test_a_weight_out_of_range_is_a_profile_error(self) -> None:
        with self.assertRaises(DecompError):
            self.boosted(0, lambda: self.SCOPE)
