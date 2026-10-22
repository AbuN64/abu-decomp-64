"""decomp ship --from-try F: the proven candidate enters the tree through `unbake submit`, never as a hand diff."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from test_ship import ShipTests, git

from abu_decomp import DecompError
from abu_decomp.ship import ship


class FromTryTests(unittest.TestCase):
    setUp = ShipTests.setUp
    landing = ShipTests.landing
    remote_tip = ShipTests.remote_tip

    def invoker(self, operation: str = "publish", write: bool = True) -> Any:
        def invoke(tool: str, profile: Any, args: Any, *, project: Path) -> Any:
            self.calls = [*getattr(self, "calls", []), (tool, list(args))]
            if write:
                (project / "a.c").write_text("int a;\nint f_matched(void) { return 0; }\n")
            row = {"operation": operation, "findings": [{"message": "not exact"}] if operation != "publish" else []}
            return SimpleNamespace(ok=True, document={"result": {"submissions": [row], "drain": {"refused": []}}})

        return invoke

    def candidate(self) -> None:
        (self.tree / ".unbake/recipe/work").mkdir(parents=True)
        (self.tree / ".unbake/recipe/work/f_matched.c").write_text("int f_matched(void) { return 0; }\n")

    def try_ship(self, **kw: Any) -> Any:
        append = lambda path, record: self.events.append(record) or {"ok": True}  # noqa: E731
        return ship(self.profile, self.tree, [], from_try="f_matched", append=append, **kw)

    def test_the_proven_candidate_is_written_gated_pushed_and_the_tree_ends_clean(self) -> None:
        self.candidate()
        result, code, _ = self.try_ship(landing=self.landing_applying(), invoke=self.invoker())
        self.assertEqual((code, result["pushed"]), (0, self.remote_tip()))
        self.assertEqual(self.calls[0][1][0], "submit")
        self.assertIn("f_matched", git(self.remote, "show", "main:a.c"))
        self.assertEqual(git(self.tree, "status", "--porcelain", "--untracked-files=no"), "")

    def test_a_candidate_that_is_not_exact_is_refused_with_the_try_command_and_the_tree_stays_clean(self) -> None:
        self.candidate()
        with self.assertRaises(DecompError) as raised:
            self.try_ship(invoke=self.invoker(operation="none", write=False))
        self.assertEqual((raised.exception.key, raised.exception.next), ("ship.from_try", "recipe try f_matched"))

    def test_a_refused_landing_puts_the_shared_tree_back_at_the_branch(self) -> None:
        self.candidate()
        result, code, _ = self.try_ship(landing=self.landing(refuse="land.check"), invoke=self.invoker())
        self.assertEqual((code, result["pushed"]), (1, None))
        self.assertEqual(git(self.tree, "status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual((self.tree / "a.c").read_text(), "int a;\n")

    def test_no_candidate_file_names_recipe_work(self) -> None:
        with self.assertRaises(DecompError) as raised:
            self.try_ship(invoke=self.invoker())
        self.assertEqual(raised.exception.next, "recipe work f_matched")

    def landing_applying(self) -> Any:
        def fake(profile: Any, tree: Path, base: str, patches: Any, **_: Any) -> Any:
            git(tree, "apply", str(patches[0][1]))
            plan = {"lane": patches[0][0], "files": ["a.c"], "functions": ["f_matched"], "body": None}
            plan["message"] = "Match f_matched"
            return {"pass": True, "commits": [plan], "lanes": [{"functions": {"names": ["f_matched"]}}]}, 0, []

        return fake
