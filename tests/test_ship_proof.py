"""Proof-carrying ships: the proof record, the carry-over decision and the carried landing (real git, mocked builds)."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import test_ship

from abu_decomp import land, shipproof
from abu_decomp.ship import ship

PATCH = """diff --git a/src/a.CPP b/src/a.CPP
--- a/src/a.CPP
+++ b/src/a.CPP
@@ -1 +1,2 @@
 int a;
+int f(void) { return 1; }
diff --git a/versions/us/symbol_addrs.txt b/versions/us/symbol_addrs.txt
--- a/versions/us/symbol_addrs.txt
+++ b/versions/us/symbol_addrs.txt
@@ -1 +1,2 @@
 old = 0x80000000;
+f = 0x80000010;
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-1%
+2%
"""


def record(**changes: Any) -> dict[str, Any]:
    shape = shipproof.describe(PATCH)
    units = {"us:src/a.CPP": {"i": "ab", "reads": ["src/a.CPP", "include/t.H"]}}
    made = shipproof.build_record("L", "B", "P", shape, units, {"us": {"sha1": "x", "ok": True}}, {"check": True})
    return {**made, **changes}


def fake_git(moved: dict[str, str], ancestor: bool = True) -> Any:
    """git(*args) over `moved`: the files changed since the base, and the -U0 diff of each."""

    def run(*args: str) -> tuple[int, str]:
        if args[0] == "merge-base":
            return (0 if ancestor else 1), ""
        if args[:2] == ("diff", "--name-only"):
            return 0, "\n".join(moved) + "\n"
        if args[0] == "diff":
            return 0, moved.get(args[-1], "")
        raise AssertionError(args)

    return run


class ShapeTests(unittest.TestCase):
    def test_a_patch_is_described_by_its_files_rows_and_generated_files(self) -> None:
        shape = shipproof.describe(PATCH)
        self.assertEqual(shape["files"], ["src/a.CPP", "versions/us/symbol_addrs.txt"])
        self.assertEqual(shape["generated"], ["README.md"])
        self.assertEqual(shape["rows"], {"versions/us/symbol_addrs.txt": ["addr:0x80000010", "name:f"]})
        self.assertEqual(shape["headers"], [])

    def test_a_dependency_file_lists_its_source_first(self) -> None:
        text = "build/us/src/a.CPP.i.ok: \\\n src/a.CPP include/t.H \\\n include/u.H\ninclude/t.H:\n"
        self.assertEqual(shipproof.parse_deps(text), ["src/a.CPP", "include/t.H", "include/u.H"])

    def test_units_are_the_built_ones_that_read_a_touched_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tree = Path(temp)
            for name, deps in (("a", "src/a.CPP include/t.H"), ("b", "src/b.CPP include/t.H"), ("c", "src/c.CPP")):
                out = tree / "build/us/src"
                out.mkdir(parents=True, exist_ok=True)
                (out / f"{name}.CPP.d").write_text(f"build/us/src/{name}.CPP.i.ok: {deps}\ninclude/t.H:\n")
                (out / f"{name}.CPP.i").write_text(f"text {name}")
            units = shipproof.collect_units(tree, {"include/t.H"})
            self.assertEqual(sorted(units), ["us:src/a.CPP", "us:src/b.CPP"])
            self.assertEqual(len(units["us:src/a.CPP"]["i"]), 64)


class JudgeTests(unittest.TestCase):
    def judge(self, moved: dict[str, str], rec: dict[str, Any] | None = None, **kw: Any) -> tuple[bool, str]:
        rec = rec or record()
        return shipproof.judge(rec, rec["patch"], "M", fake_git(moved, **kw))

    def test_the_proof_carries_over_when_nothing_it_reads_changed(self) -> None:
        ok, why = self.judge({"src/z.CPP": "", "README.md": "", "docs/map-us.json": "", "progress.json": ""})
        self.assertTrue(ok, why)

    def test_the_branch_still_at_the_base_carries(self) -> None:
        rec = record(base="M")
        self.assertTrue(shipproof.judge(rec, "P", "M", fake_git({}))[0])

    def test_a_commit_that_touched_a_file_a_unit_reads_re_proves(self) -> None:
        ok, why = self.judge({"include/t.H": ""})
        self.assertFalse(ok)
        self.assertIn("include/t.H", why)
        self.assertIn("us:src/a.CPP", why)
        self.assertFalse(self.judge({"src/a.CPP": ""})[0])

    def test_a_build_input_every_unit_reads_re_proves(self) -> None:
        for name in ("config.toml", "Makefile", "versions/us/text_sizes.mk", "tools/gcc/cc1"):
            self.assertFalse(self.judge({name: ""})[0], name)

    def test_rows_merge_one_by_one_in_a_fact_file(self) -> None:
        path = "versions/us/symbol_addrs.txt"
        other = f"diff --git a/{path} b/{path}\n+g = 0x80000020;\n"
        self.assertTrue(self.judge({path: other})[0])  # a different symbol at a different address
        clash = f"diff --git a/{path} b/{path}\n+f = 0x80000030;\n"
        ok, why = self.judge({path: clash})
        self.assertFalse(ok)
        self.assertIn("name:f", why)
        same_address = f"diff --git a/{path} b/{path}\n+h = 0x80000010;\n"
        self.assertFalse(self.judge({path: same_address})[0])

    def test_a_patch_that_changes_a_header_waits_for_a_quiet_branch(self) -> None:
        shape = shipproof.describe(PATCH.replace("src/a.CPP", "include/t.H"))
        rec = record(headers=shape["headers"], closure=["include/t.H", "src/a.CPP"])
        ok, why = self.judge({"src/z.CPP": ""}, rec)
        self.assertFalse(ok)
        self.assertIn("types train", why)
        self.assertTrue(self.judge({"README.md": ""}, rec)[0])

    def test_refusals_without_a_record_a_changed_patch_a_failed_proof_or_a_rebased_branch(self) -> None:
        self.assertFalse(shipproof.judge(None, "P", "M", fake_git({}))[0])
        self.assertFalse(shipproof.judge(record(), "other", "M", fake_git({}))[0])
        self.assertFalse(shipproof.judge(record(**{"pass": False}), "P", "M", fake_git({}))[0])
        self.assertIn("rebase", self.judge({}, ancestor=False)[1])


class ProveTests(unittest.TestCase):
    """The proof built in a real work tree (git worktree of a clone), with the builds mocked."""

    def setUp(self) -> None:
        shared = test_ship.ShipTests()
        shared.setUp()
        self.addCleanup(shared.doCleanups)
        self.__dict__.update({k: v for k, v in shared.__dict__.items() if k in {"root", "remote", "tree", "profile"}})
        test_ship.git(self.tree, "worktree", "add", "--detach", "-q", str(self.root / "wt" / "L"), "HEAD")
        self.work = self.root / "wt" / "L"
        (self.work / "a.c").write_text("int a;\nint f(void) { return 1; }\n")
        self.patch = self.root / "l.patch"
        self.patch.write_text(test_ship.git(self.work, "diff", "HEAD"))
        test_ship.git(self.work, "checkout", "-q", "--", "a.c")
        self.calls: list[tuple[str, ...]] = []

    def mocks(self, check: int = 0) -> dict[str, Any]:
        def invoke(tool: str, profile: Any, args: Any, **kw: Any) -> Any:
            self.calls.append((tool, *args))
            return SimpleNamespace(ok=True, document={})

        def build(argv: Any, cwd: Path, env: Any, timeout: Any) -> Any:
            self.calls.append(("build", str(cwd)))
            out = cwd / "build/us"
            out.mkdir(parents=True, exist_ok=True)
            (out / "a.c.d").write_text("build/us/a.c.i.ok: a.c include/t.H\n")
            (out / "a.c.i").write_text("preprocessed")
            return check, "tail line\n", ""

        return {"run": land.run_process, "build": build, "invoke": invoke}

    def test_the_patch_is_applied_for_the_proof_and_taken_off_again(self) -> None:
        rec = shipproof.prove(self.profile, self.tree, "L", self.patch, **self.mocks())
        self.assertEqual(rec["base"], test_ship.git(self.work, "rev-parse", "HEAD").strip())
        self.assertEqual((rec["files"], rec["gates"], rec["pass"]), (["a.c"], {"check": True, "verify": True}, True))
        self.assertEqual(sorted(rec["units"]), ["us:a.c"])
        self.assertEqual(rec["closure"], ["a.c", "include/t.H"])
        self.assertEqual(rec["patch"], shipproof.patch_hash(self.patch))
        self.assertIn(("build", str(self.work)), self.calls)
        self.assertEqual(test_ship.git(self.work, "status", "--porcelain", "--untracked-files=no"), "")

    def test_a_work_tree_that_already_holds_the_patch_is_proved_as_it_is(self) -> None:
        test_ship.git(self.work, "apply", str(self.patch))
        rec = shipproof.prove(self.profile, self.tree, "L", self.patch, **self.mocks())
        self.assertTrue(rec["pass"])
        self.assertIn("preprocessed" if False else "f(void)", (self.work / "a.c").read_text())  # left applied

    def test_a_failed_build_is_a_record_that_did_not_pass_with_its_tail(self) -> None:
        rec = shipproof.prove(self.profile, self.tree, "L", self.patch, **self.mocks(check=2))
        self.assertEqual((rec["pass"], rec["units"]), (False, {}))
        self.assertIn("tail line", rec["tail"])

    def test_tracked_changes_beyond_the_patch_are_no_proof_of_it(self) -> None:
        (self.work / "z.c").write_text("x")
        test_ship.git(self.work, "add", "z.c")
        with self.assertRaises(shipproof.NoProof) as caught:
            shipproof.prove(self.profile, self.tree, "L", self.patch, **self.mocks())
        self.assertIn("beyond the patch", caught.exception.detail)
        self.assertFalse((self.work / "a.c").read_text().count("f(void)"))  # the patch is off again

    def test_a_lane_without_a_work_tree_has_none_to_prove_in(self) -> None:
        self.assertIsNone(shipproof.worktree_of(self.tree, "NOPE"))
        with self.assertRaises(shipproof.NoProof):
            shipproof.prove(self.profile, self.tree, "NOPE", self.patch, **self.mocks())


class CarriedShipTests(unittest.TestCase):
    def setUp(self) -> None:
        shared = test_ship.ShipTests()
        shared.setUp()
        self.addCleanup(shared.doCleanups)
        self.shared = shared
        self.__dict__.update({k: v for k, v in shared.__dict__.items() if k in {"root", "remote", "tree", "profile"}})
        test_ship.git(self.tree, "worktree", "add", "--detach", "-q", str(self.root / "wt" / "L"), "HEAD")
        self.patch = self.root / "l.patch"
        self.patch.write_text("diff --git a/a.c b/a.c\n--- a/a.c\n+++ b/a.c\n@@ -1 +1,2 @@\n int a;\n+int f;\n")
        self.base = test_ship.git(self.tree, "rev-parse", "HEAD").strip()
        self.lands: list[dict[str, Any]] = []
        self.spawned = 0
        self.proved = 0

    def prove(self, *args: Any, **kw: Any) -> dict[str, Any]:
        self.proved += 1
        shape = shipproof.describe(self.patch.read_text())
        units = {"us:a.c": {"i": "ab", "reads": ["a.c", "include/t.H"]}}
        return shipproof.build_record(
            "L", self.base, shipproof.patch_hash(self.patch), shape, units, {"us": {"sha1": "x", "ok": True}},
            {"check": True},
        )  # fmt: skip

    def landing(self, profile: Any, tree: Path, base: str, patches: Any, **kw: Any) -> Any:
        self.lands.append({"base": base, **kw})
        (tree / "a.c").write_text((tree / "a.c").read_text() + "int f;\n")
        plan = {"lane": "L", "files": ["a.c"], "functions": ["f"], "message": "Match f", "body": None}
        return {"pass": True, "commits": [plan], "lanes": [{"lane": "L", "functions": {"names": ["f"]}}]}, 0, []

    def go(self, **kw: Any) -> Any:
        append = lambda path, record: {"ok": True}  # noqa: E731
        return ship(
            self.profile, self.tree, [("L", self.patch)], landing=self.landing, append=append, prove=self.prove,
            spawn=lambda: setattr(self, "spawned", self.spawned + 1), **kw,
        )  # fmt: skip

    def push_other(self, name: str) -> None:
        other = self.shared.other
        (other / name).write_text("int x;\n")
        test_ship.git(other, "add", ".")
        test_ship.git(other, "commit", "-q", "-m", name)
        test_ship.git(other, "push", "-q", "origin", "HEAD:main")

    def test_a_proof_that_holds_lands_with_no_build_and_is_stored_with_the_commit(self) -> None:
        self.push_other("unrelated.c")  # the branch moved after the proof, in a file no unit of it reads
        result, code, findings = self.go()
        self.assertEqual(code, 0, findings)
        self.assertEqual([row.get("carry") for row in self.lands], [True])
        self.assertTrue(result["carried"])
        self.assertLess(result["lock_seconds"], 10)
        sha = result["pushed"]
        self.assertEqual(sha, test_ship.git(self.remote, "rev-parse", "main").strip())
        note = json.loads(test_ship.git(self.tree, "notes", f"--ref={shipproof.NOTES}", "show", sha))
        self.assertEqual((note["mode"], note["base"] == self.base, note["lane"]), ("carried", False, "L"))
        landed = json.loads((self.tree / ".git" / shipproof.LANDED / f"{sha}.json").read_text())
        self.assertIn("us:a.c", landed["proof"]["units"])
        self.assertTrue(test_ship.git(self.remote, "rev-parse", f"refs/notes/{shipproof.NOTES}").strip())
        self.assertEqual(self.spawned, 1)  # the batch regeneration starts after a carried push
        self.assertEqual(self.proved, 1)

    def test_a_commit_that_touched_a_file_the_proof_read_makes_the_round_build(self) -> None:
        self.push_other("a.c")
        result, code, _ = self.go()
        self.assertEqual(code, 0)
        self.assertEqual([row.get("carry") for row in self.lands], [None])  # the full land
        self.assertFalse(result["carried"])
        self.assertEqual(self.spawned, 0)

    def test_the_proof_is_built_once_and_found_again(self) -> None:
        self.go()
        test_ship.git(self.tree, "reset", "-q", "--hard", self.base)
        self.go()
        self.assertEqual(self.proved, 1)

    def test_no_proof_flag_lands_with_the_full_gates(self) -> None:
        _, code, _ = self.go(proof=False)
        self.assertEqual((code, [row.get("carry") for row in self.lands], self.proved), (0, [None], 0))

    def test_a_patch_whose_proof_build_failed_is_refused_before_the_queue(self) -> None:
        def failed(*args: Any, **kw: Any) -> dict[str, Any]:
            return {**self.prove(), "pass": False, "tail": "make: *** [x] Error 1", "roms": {}}

        held = __import__("os").open(self.tree / ".git" / "decomp-ship.lock", __import__("os").O_RDWR | 64)
        self.addCleanup(__import__("os").close, held)
        __import__("fcntl").flock(held, __import__("fcntl").LOCK_EX)  # a ship ahead: a late refusal would hang
        _, code, findings = ship(
            self.profile, self.tree, [("L", self.patch)], landing=self.landing, prove=failed, lock_wait=1
        )
        self.assertEqual((code, findings[0]["key"], self.lands), (1, "ship.proof_failed", []))
        self.assertIn("Error 1", findings[0]["tail"])


class BatchAdoptionTests(unittest.TestCase):
    def test_only_inputs_with_a_passing_proof_and_no_row_clash_join_a_round(self) -> None:
        shared = test_ship.ShipTests()
        shared.setUp()
        self.addCleanup(shared.doCleanups)
        common = shared.tree / ".git"
        rows = {"versions/us/symbol_addrs.txt": ["name:f", "addr:0x10"]}
        made = {}
        for lane, mine in (("A", rows), ("B", {"versions/us/symbol_addrs.txt": ["name:g"]}), ("C", rows)):
            path = shared.root / f"{lane}.patch"
            path.write_text(lane)
            shape = {**shipproof.describe(""), "rows": mine}
            record = shipproof.build_record(lane, "B", shipproof.patch_hash(path), shape, {}, {}, {"check": True})
            shipproof.store(common, record)
            made[lane] = [(lane, path)]
        ghost = shared.root / "D.patch"
        ghost.write_text("D")  # no record: unproven
        one = ship_object(shared, made["A"])
        accept = one.accepts()
        self.assertTrue(accept(made["B"]))
        self.assertFalse(accept(made["C"]))  # its rows clash with A's
        self.assertFalse(accept([("D", ghost)]))  # unproven: lands alone or bounces
        lonely = ship_object(shared, [("D", ghost)])
        self.assertFalse(lonely.accepts()([("B", made["B"][0][1])]))  # an unproven ship adopts nobody


def ship_object(shared: Any, patches: Any) -> Any:
    from abu_decomp.ship import Ship

    return Ship(shared.profile, shared.tree, patches)


class CarriedLandingTests(unittest.TestCase):
    def test_a_carried_round_records_the_build_gates_as_carried_and_runs_no_build_stage(self) -> None:
        ticks = iter(range(1000))
        fake = SimpleNamespace(
            carry=True, config={"mode": "unbake"}, clock=lambda: next(ticks), gates=[], cpu_since_gate=lambda: 0.0
        )
        fake.gate = lambda *a, **k: land.Landing.gate(fake, *a, **k)
        fake.carried = lambda: land.Landing.carried(fake)
        stages = land.Landing.build_stages(fake)
        self.assertEqual(len(stages), 1)
        self.assertTrue(land.Landing.carried(fake))
        self.assertEqual(
            [(g["gate"], g["pass"], g["carried"]) for g in fake.gates],
            [(n, True, True) for n in ("setup", "check", "sync_after", "audit")],
        )


class RegenTests(unittest.TestCase):
    def setUp(self) -> None:
        shared = test_ship.ShipTests()
        shared.setUp()
        self.addCleanup(shared.doCleanups)
        self.shared = shared
        self.__dict__.update({k: v for k, v in shared.__dict__.items() if k in {"root", "remote", "tree", "profile"}})
        self.built = 0
        self.verify = True

    def invoke(self, tool: str, profile: Any, args: Any, **kw: Any) -> Any:
        if args[0] == "sync":
            (self.tree / "README.md").write_text(f"regenerated {self.built}\n")  # sync writes what the build says
        return SimpleNamespace(ok=self.verify or args[0] != "verify", document={"result": {"written": ["README.md"]}})

    def build(self, argv: Any, cwd: Path, env: Any, timeout: Any) -> tuple[int, str, str]:
        self.built += 1
        return (0 if self.verify else 2), "", "tail"

    def regen(self, **kw: Any) -> Any:
        return ship(
            self.profile, self.tree, [], regen=True, invoke=self.invoke, build=self.build,
            append=lambda *a: {"ok": True}, **kw,
        )  # fmt: skip

    def push_other(self, name: str) -> None:
        other = self.shared.other
        test_ship.git(other, "pull", "-q", "--rebase", "origin", "main")
        (other / name).write_text("int x;\n")
        test_ship.git(other, "add", ".")
        test_ship.git(other, "commit", "-q", "-m", name)
        test_ship.git(other, "push", "-q", "origin", "HEAD:main")

    def test_it_waits_until_enough_ships_landed_then_regenerates_in_one_commit(self) -> None:
        self.regen(regen_now=True)  # the first regeneration sets the mark
        self.assertEqual(self.built, 1)
        self.push_other("one.c")
        result, code, _ = self.regen(regen_min=2, regen_age=1e9)
        self.assertEqual((code, result["regen"]["due"], result["regen"]["pending"], self.built), (0, False, 1, 1))
        self.push_other("two.c")
        result, code, _ = self.regen(regen_min=2, regen_age=1e9)
        self.assertEqual((code, result["regen"]["due"], self.built), (0, True, 2))
        self.assertEqual(result["regen"]["files"], ["README.md"])
        tip = test_ship.git(self.remote, "log", "-1", "--format=%an|%s", "main").strip()
        self.assertEqual(tip, "mo|Regenerate maps and progress after 2 ships")
        _, code, _ = self.regen(regen_min=2, regen_age=1e9)  # nothing landed since: not due again
        self.assertEqual(self.built, 2)

    def test_a_branch_that_does_not_build_is_a_finding_and_no_commit(self) -> None:
        self.push_other("one.c")
        self.verify = False
        before = test_ship.git(self.remote, "rev-parse", "main").strip()
        result, code, findings = self.regen(regen_now=True)
        self.assertEqual((code, findings[0]["key"], result["pushed"]), (1, "ship.main_red", None))
        self.assertEqual(test_ship.git(self.remote, "rev-parse", "main").strip(), before)
        self.assertTrue((self.tree / ".git" / "decomp-ship.red.json").exists())


class CliTests(unittest.TestCase):
    def test_the_ship_command_takes_the_proof_and_regen_flags(self) -> None:
        out = subprocess.run(
            ["python3", "-m", "abu_decomp", "ship", "--help"], capture_output=True, text=True, check=False
        )
        self.assertIn("--regen", out.stdout + out.stderr)
        self.assertIn("--no-proof", out.stdout + out.stderr)


if __name__ == "__main__":
    unittest.main()
