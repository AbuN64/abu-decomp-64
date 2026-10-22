"""Ships that queue together land together: the ship that takes the lock lands the inputs waiting behind it."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
import unittest
from pathlib import Path
from typing import Any

import test_ship

from abu_decomp.ship import ship
from abu_decomp.ship_batch import Batch, restrict


class BatchTests(unittest.TestCase):
    def setUp(self) -> None:
        shared = test_ship.ShipTests()  # the same remote, warm clone and profile the ship tests use
        shared.setUp()
        self.addCleanup(shared.doCleanups)
        self.__dict__.update({k: v for k, v in shared.__dict__.items() if k in {"root", "remote", "tree", "profile"}})

    def remote_tip(self) -> str:
        return test_ship.git(self.remote, "rev-parse", "main").strip()

    def multi(self, calls: list[list[str]], refuse: str = "") -> Any:
        def fake(profile: Any, tree: Path, base: str, patches: Any, **_: Any) -> Any:
            calls.append([lane for lane, _ in patches])
            plans, lanes = [], []
            for lane, _ in patches:
                (tree / f"{lane}.c").write_text(f"int {lane};\n")
                plans.append({"lane": lane, "files": [f"{lane}.c"], "functions": [lane], "body": None})
                plans[-1]["message"] = f"Match {lane}"
                lanes.append({"lane": lane, "functions": {"names": [lane]}, "status": "applied"})
            return {"pass": True, "commits": plans, "lanes": lanes}, 0, []

        return fake

    def test_two_queued_ships_land_in_one_round_and_each_gets_its_own_result(self) -> None:
        calls: list[list[str]] = []
        common = self.tree / ".git"
        held = os.open(common / "decomp-ship.lock", os.O_RDWR | os.O_CREAT)
        fcntl.flock(held, fcntl.LOCK_EX)
        out: dict[str, Any] = {}

        def run(lane: str) -> None:
            append = lambda path, record: {"ok": True}  # noqa: E731
            out[lane] = ship(
                self.profile, self.tree, [(lane, self.root / f"{lane}.patch")], landing=self.multi(calls), append=append
            )

        threads = [threading.Thread(target=run, args=(lane,)) for lane in ("L1", "L2")]
        for thread in threads:
            thread.start()
            deadline = time.time() + 20
            while len(list((common / "decomp-ship.inputs").glob("*.json"))) < threads.index(thread) + 1:
                self.assertLess(time.time(), deadline)
                time.sleep(0.05)
        (self.root / "L1.patch").write_text("")
        (self.root / "L2.patch").write_text("")
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
        for thread in threads:
            thread.join(30)
        self.assertEqual(calls, [["L1", "L2"]])
        for lane in ("L1", "L2"):
            shipped, code, _ = out[lane]
            self.assertEqual(code, 0)
            self.assertEqual([row["lane"] for row in shipped["inputs"]], [lane])
            self.assertEqual(shipped["functions"], [lane])
            self.assertEqual(shipped["pushed"], self.remote_tip())
        self.assertEqual(
            test_ship.git(self.remote, "log", "--format=%s", "-2", "main").split(), ["Match", "L2", "Match", "L1"]
        )
        self.assertEqual(list((common / "decomp-ship.inputs").glob("*")), [])

    def test_a_claim_of_a_dead_adopter_goes_back_to_the_queue(self) -> None:
        common = self.tree / ".git"
        common.mkdir(exist_ok=True)
        batch = Batch(common, "00000000000000000002-1")
        batch.publish([("L", self.root / "l.patch")], {})
        batch.mine.rename(batch.inputs / "00000000000000000002-1.claimed-999999999")
        self.assertEqual(batch.poll(), ("queued", None))
        self.assertTrue(batch.mine.exists())

    def test_the_round_verdict_is_split_per_input(self) -> None:
        shipped = {
            "pushed": "abc",
            "inputs": [
                {"lane": "A", "status": "refused", "key": "land.check", "message": "no"},
                {"lane": "B", "status": "shipped", "functions": ["fb"]},
            ],
        }
        mine, code, found = restrict((shipped, 0, [{"key": "x", "lane": "A"}, {"key": "y", "lane": "B"}]), {"A"})
        self.assertEqual(
            (code, mine["pushed"], mine["refused"]["key"], [f["key"] for f in found]), (1, None, "land.check", ["x"])
        )
        mine, code, _ = restrict((shipped, 0, []), {"B"})
        self.assertEqual((code, mine["pushed"], mine["functions"]), (0, "abc", ["fb"]))
        json.dumps(mine)


class PrecheckTests(unittest.TestCase):
    def test_a_long_subject_is_refused_before_the_queue_wait(self) -> None:
        shared = test_ship.ShipTests()
        shared.setUp()
        self.addCleanup(shared.doCleanups)
        held = os.open(shared.tree / ".git" / "decomp-ship.lock", os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, held)
        fcntl.flock(held, fcntl.LOCK_EX)  # a ship ahead: a refusal after the wait would never come
        _, code, findings = ship(
            shared.profile, shared.tree, [("L", shared.root / "l.patch")], messages={"L": "m" * 80}, lock_wait=1
        )
        self.assertEqual((code, findings[0]["key"]), (1, "land.commit_subject"))
        self.assertIn("decomp", findings[0]["next"])


class PlaceholderTests(unittest.TestCase):
    def test_a_converted_function_keeping_its_extraction_name_is_found_and_a_named_one_is_not(self) -> None:
        import tempfile
        from types import SimpleNamespace

        from abu_decomp.land import Landing

        with tempfile.TemporaryDirectory() as temp:
            tree = Path(temp)
            (tree / "a.c").write_text(
                'void func_80201A80_de(int a)\n{\n}\nINCLUDE_ASM("m", func_80201A90_de);\nint Named_Thing(void)\n{\n}\n'
                "void func_80201AA0_de(int);\n"
            )
            row = {"files": ["a.c"], "functions": {"names": ["func_80201A80_de", "func_80201AA0_de", "Named_Thing"]}}
            self.assertEqual(Landing.placeholder_names(SimpleNamespace(tree=tree), row), ["func_80201A80_de"])


class BuildLogTests(unittest.TestCase):
    def test_a_failed_build_keeps_its_first_errors_and_the_log_path_after_the_closing_warnings(self) -> None:
        import tempfile

        from abu_decomp.land import build_process

        with tempfile.TemporaryDirectory() as temp:
            script = (
                "echo 'a.c:3: error: conflicting types for f'; for i in $(seq 30); do echo warning $i; done; exit 2"
            )
            code, out, _ = build_process(["sh", "-c", script], Path(temp), None, 30)
            last = out.strip().splitlines()[-12:]
            self.assertEqual(code, 2)
            self.assertTrue(any("conflicting types for f" in line for line in last))
            self.assertIn("full log:", last[-1])
            self.assertIn("warning 30", (Path(temp) / ".unbake/ship-check.log").read_text())


class WorktreeTriesTests(unittest.TestCase):
    def test_a_try_recorded_in_a_worktree_counts_for_the_tree(self) -> None:
        import tempfile

        from abu_decomp import provenance

        shared = test_ship.ShipTests()
        shared.setUp()
        self.addCleanup(shared.doCleanups)
        wt = shared.root / "wt"
        test_ship.git(shared.tree, "worktree", "add", "--detach", "-q", str(wt))
        (wt / ".unbake/recipe").mkdir(parents=True)
        row = {
            "item": "f",
            "result": "exact",
            "time": 1,
            "session": "s",
            "text": "int f_x(void)\n{\n    return 3;\n}\n",
        }
        (wt / ".unbake/recipe/tries.jsonl").write_text(json.dumps(row) + "\n")
        self.assertEqual([p.name for p in provenance.worktrees(shared.tree)], ["wt"])
        found = provenance.recorded([wt / ".unbake/recipe/tries.jsonl"])
        self.assertTrue(found)
        self.assertEqual(tempfile.gettempdir() is not None, True)


class SeedTests(unittest.TestCase):
    def test_a_seeded_tree_gets_the_shared_trees_times_for_files_with_equal_bytes(self) -> None:
        from abu_decomp import worktree

        shared = test_ship.ShipTests()
        shared.setUp()
        self.addCleanup(shared.doCleanups)
        wt = shared.root / "wt"
        test_ship.git(shared.tree, "worktree", "add", "--detach", "-q", str(wt))
        os.utime(shared.tree / "a.c", ns=(10**18, 10**18))
        (wt / "a.c").write_text("int a;\n")
        (wt / "new.c").write_text("x")
        test_ship.git(wt, "add", "new.c")
        self.assertGreaterEqual(worktree.align_times(shared.tree, wt), 1)
        self.assertEqual((wt / "a.c").stat().st_mtime_ns, 10**18)


class ArityTests(unittest.TestCase):
    def test_a_64_bit_argument_starts_at_an_even_word(self) -> None:
        from abu_decomp.arity import argument_words

        self.assertEqual(argument_words(["int64", "int64"]), 4)
        self.assertEqual(argument_words(["int", "int64"]), 4)
        self.assertEqual(argument_words(["int", "int", "uint64"]), 4)
        self.assertEqual(argument_words(["int", "int", "int"]), 3)
        self.assertEqual(argument_words(["int", "int64", "int"]), 5)
        self.assertEqual(
            argument_words(["ptr", "int64", "int64"]), 6
        )  # lldiv(LldivResult *, s64, s64): a0, a2:a3, stack


class LooseTests(unittest.TestCase):
    def test_a_rename_or_retype_inside_a_non_matching_body_needs_no_try(self) -> None:
        from abu_decomp import evidence, provenance

        old = "#ifdef NON_MATCHING\nint f(int a)\n{\n    s32 x = func_8(a);\n    return x;\n}\n#endif\n"
        new = old.replace("func_8", "__divdi3").replace("s32", "u32")
        edit = old.replace("x = func_8(a)", "x = func_8(a) + 1")
        base, same, other = (evidence.parse(t) for t in (old, new, edit))
        self.assertEqual(provenance.unproven(base, same, {}), [])
        self.assertEqual([r["function"] for r in provenance.unproven(base, other, {})], ["f"])


class PruneTests(unittest.TestCase):
    def test_an_ended_lanes_worktree_goes_unless_a_queued_ship_names_it(self) -> None:
        from abu_decomp import worktree

        shared = test_ship.ShipTests()
        shared.setUp()
        self.addCleanup(shared.doCleanups)
        base = shared.tree.parent / "wt"
        for lane in ("A", "B"):
            test_ship.git(shared.tree, "worktree", "add", "--detach", "-q", str(base / lane))
        inputs = shared.tree / ".git" / "decomp-ship.inputs"
        inputs.mkdir()
        (inputs / "1-1.json").write_text(json.dumps({"patches": [["B", str(base / "B" / "x.patch")]]}))
        (base / "A" / ".unbake/recipe").mkdir(parents=True)
        (base / "A" / ".unbake/recipe/skips.jsonl").write_text('{"item":"f","reason":"other"}\n')
        result, code, findings = worktree.prune(shared.tree, ["A", "B", "C"])
        self.assertEqual(result["skips_carried"], 1)
        self.assertIn("f", (shared.tree / ".unbake/recipe/skips.jsonl").read_text())
        self.assertEqual(code, 0)
        self.assertEqual([Path(p).name for p in result["removed"]], ["A"])
        self.assertEqual([f["key"] for f in findings], ["worktree.referenced"])
        self.assertFalse((base / "A").exists())
        self.assertTrue((base / "B").exists())
