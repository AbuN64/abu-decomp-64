import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from abu_decomp import DecompError
from abu_decomp.config import load_profile
from abu_decomp.land import (
    LandError,
    Landing,
    build_process,
    code_percentages,
    diff_lines,
    function_name,
    parse_messages,
    parse_patches,
    run_process,
    sentence_list,
    top_dirs,
)

README = "| code [###] {a}% (~80%) 10 of 20 |\n| code [###] {b}% (~80%) 10 of 20 |\n"
A_C = '#include "x.h"\nINCLUDE_ASM(const s32, "a", func_1);\nINCLUDE_ASM(const s32, "a", func_2);\n'
B_C = 'INCLUDE_ASM(const s32, "b", func_3);\n'
C_C = "int untouched(void) { return 0; }\n"


def git(root, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t"}
    env["GIT_COMMITTER_EMAIL"] = "t@t"
    return subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, text=True, check=True).stdout


class LandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tree = self.root / "tree"
        self.tree.mkdir()
        git(self.tree, "init", "-q", "-b", "main")
        for name, text in (
            ("a.c", A_C),
            ("b.c", B_C),
            ("c.c", C_C),
            ("README.md", README.format(a="50.00", b="60.00")),
        ):
            (self.tree / name).write_text(text)
        git(self.tree, "add", ".")
        git(self.tree, "commit", "-q", "-m", "base")
        self.base = git(self.tree, "rev-parse", "HEAD").strip()
        (self.root / "host.toml").write_text("")
        (self.root / "events.jsonl").write_text("")
        sections = {
            "tools": {"project": "tree", "host": "host.toml", "timeout_seconds": 5},
            "events": {"path": "events.jsonl"},
            "land": {"check": ["make", "check"], "jobs": ["admit", "8"]},
        }
        (self.root / "seat.json").write_text(json.dumps({"schema_version": 1, "name": "seat", **sections}))
        self.profile = load_profile(self.root / "seat.json")
        self.unbake, self.commands, self.events = [], [], []
        self.on_sync = lambda: None
        self.on_check = lambda: None

    def patch(self, lane, edits):
        """A lane patch made against the base: edits {file: new text}; the tree is restored afterwards."""
        for name, text in edits.items():
            (self.tree / name).write_text(text)
        text = git(self.tree, "diff", "HEAD")
        git(self.tree, "checkout", "-q", "HEAD", "--", ".")
        path = self.root / f"{lane}.patch"
        path.write_text(text)
        return lane, path

    def invoke(self, tool, profile, args, project=None):
        self.unbake.append(tuple(args))
        self.assertEqual(project, self.tree)
        if args[0] == "sync" and "--check" not in args:
            self.on_sync()
        return SimpleNamespace(ok=self.sync_ok(args), returncode=0, document={"ok": True, "findings": []})

    def sync_ok(self, args):
        return True

    def runner(self, argv, cwd, env, timeout):
        if argv[0] == "git":
            return run_process(argv, cwd, env, timeout)
        self.commands.append((tuple(argv), env is not None and "UNBAKE_HOST" in env))
        if argv[0] == "admit":
            return 0, "6\n", ""
        self.on_check()
        return 0, "built\n", ""

    def land(self, patches, *, pin="u1", base=None):
        append = lambda path, record: self.events.append(record) or {"ok": True}  # noqa: E731
        landing = Landing(
            self.profile,
            self.tree,
            base or self.base,
            patches,
            invoke=self.invoke,
            run=self.runner,
            build=self.runner,
            append=append,
            pin=lambda: pin,
        )
        return landing.execute()

    def gates(self, result):
        return {row["gate"]: row["pass"] for row in result["gates"]}

    def test_applies_lanes_groups_files_and_counts_functions(self):
        one = self.patch(
            "L1", {"a.c": A_C.replace('INCLUDE_ASM(const s32, "a", func_1);\n', "int func_1(void) { return 1; }\n")}
        )
        two = self.patch("L2", {"b.c": "int func_3(void) { return 3; }\n"})
        self.on_check = lambda: (self.tree / "README.md").write_text(README.format(a="51.00", b="60.00"))
        result, code, findings = self.land([one, two])
        self.assertEqual((code, findings), (0, []))
        self.assertTrue(all(v for v in self.gates(result).values()))
        groups = {g["lane"]: g["files"] for g in result["groups"]}
        self.assertEqual(groups, {"L1": ["a.c"], "L2": ["README.md", "b.c"]})  # generated README joins the last group
        self.assertEqual(result["generated"], ["README.md"])
        lanes = {row["lane"]: row["functions"] for row in result["lanes"]}
        self.assertEqual(lanes["L1"]["converted"], 1)
        self.assertEqual(lanes["L1"]["names"], ["func_1"])
        self.assertEqual(result["functions"], {"converted": 2, "added": 0})
        self.assertEqual([c[0] for c in self.commands], [("admit", "8"), ("make", "check", "-j6")])
        self.assertTrue(self.commands[1][1])  # UNBAKE_HOST reaches the check command
        self.assertEqual(self.unbake, [("setup",), ("sync",), ("sync",), ("sync", "--check")])
        self.assertEqual([e["kind"] for e in self.events], ["LAND"])
        self.assertEqual(self.events[0]["status"], "passed")

    def test_commit_plan_one_entry_per_lane_with_the_project_message(self):
        one = self.patch(
            "L1", {"a.c": A_C.replace('INCLUDE_ASM(const s32, "a", func_1);\n', "int func_1(void) { return 1; }\n")}
        )
        two = self.patch("L2", {"b.c": "int func_3(void) { return 3; }\n"})
        three = self.patch("L3", {"c.c": "int untouched(void) { return 1; }\n"})
        result, code, findings = self.land([one, two, three])
        self.assertEqual(code, 0)
        self.assertEqual([c["lane"] for c in result["commits"]], ["L1", "L2", "L3"])
        self.assertEqual(result["commits"][0]["message"], "Fix 1 INCLUDE_ASM stand-in by matching func_1 as C")
        self.assertEqual(result["commits"][0]["functions"], ["func_1"])
        self.assertEqual(result["commits"][1]["files"], ["b.c"])
        self.assertIsNone(result["commits"][2]["message"])  # no function and no explicit message: never guessed
        self.assertEqual([f["key"] for f in findings], ["land.commit_message"])
        self.assertEqual(
            result["commit_commands"][0],
            "git add -- a.c && git commit -m 'Fix 1 INCLUDE_ASM stand-in by matching func_1 as C' -- a.c",
        )
        self.assertEqual(len(result["commit_commands"]), 2)

    def test_explicit_patch_message_names_a_repin_only_commit(self):
        three = self.patch("L3", {"c.c": "int untouched(void) { return 1; }\n"})
        landing = Landing(
            self.profile,
            self.tree,
            self.base,
            [three],
            invoke=self.invoke,
            run=self.runner,
            build=self.runner,
            append=lambda path, record: {"ok": True},
            pin=lambda: "u1",
            messages={"L3": "Fix the pin: repin"},
        )
        result, code, findings = landing.execute()
        self.assertEqual((code, findings), (0, []))
        self.assertEqual(result["commits"][0]["message"], "Fix the pin: repin")

    def test_patch_message_parsing_and_commit_helpers(self):
        rows = ["A=/x/a.patch", "B=/x/b.patch:Fix a: with colons"]
        self.assertEqual([lane for lane, _ in parse_patches(rows)], ["A", "B"])
        self.assertEqual(str(parse_patches(rows)[1][1]), "/x/b.patch")
        self.assertEqual(parse_messages(rows), {"B": "Fix a: with colons"})
        with self.assertRaises(LandError):
            parse_patches(["A=/x/a.patch:"])
        self.assertEqual(top_dirs(["src/AI/NAV/x.c", "src/AI/y.c", "src/MENU/EVENT/z.c", "README.md"]), ["AI", "MENU"])
        self.assertEqual(sentence_list(["a", "b", "c"]), "a, b and c")
        self.assertEqual(sentence_list(["a"]), "a")

    def test_setup_is_skipped_when_the_tree_was_set_up_with_the_same_pin(self):
        one = self.patch("L1", {"c.c": C_C + "// x\n"})
        self.land([one])
        git(self.tree, "checkout", "-q", "HEAD", "--", ".")
        self.unbake.clear()
        result, code, _ = self.land([one])
        self.assertEqual(code, 0)
        self.assertNotIn(("setup",), self.unbake)
        self.assertTrue(result["gates"][2]["skipped"])
        self.unbake.clear()
        git(self.tree, "checkout", "-q", "HEAD", "--", ".")
        self.land([one], pin="u2")
        self.assertIn(("setup",), self.unbake)

    def test_conflicting_patch_is_excluded_with_its_reason_and_the_tree_is_restored(self):
        good = self.patch("good", {"b.c": "int func_3(void) { return 3; }\n"})
        bad = self.patch("bad", {"a.c": "int func_1(void) { return 1; }\n"})
        (self.tree / "a.c").write_text("totally different\n")
        git(self.tree, "commit", "-qam", "moved")
        moved = git(self.tree, "rev-parse", "HEAD").strip()
        result, code, findings = self.land([bad, good], base=moved)
        self.assertEqual(code, 0)
        self.assertEqual([r["status"] for r in result["lanes"]], ["excluded", "applied"])
        self.assertTrue(result["lanes"][0]["reason"])
        self.assertEqual(findings[0]["key"], "land.patch_excluded")
        self.assertFalse(findings[0]["blocking"])
        self.assertEqual((self.tree / "a.c").read_text(), "totally different\n")
        self.assertEqual(git(self.tree, "ls-files", "-u"), "")
        self.assertEqual(result["groups"], [{"lane": "good", "files": ["b.c"]}])

    def test_no_patch_applying_fails_the_apply_gate_and_skips_the_rest(self):
        bad = self.patch("bad", {"a.c": "int func_1(void) { return 1; }\n"})
        (self.tree / "a.c").write_text("totally different\n")
        git(self.tree, "commit", "-qam", "moved")
        result, code, findings = self.land([bad, ("gone", self.root / "nope.patch")], base="HEAD")
        self.assertEqual(code, 1)
        self.assertEqual(self.gates(result)["apply"], False)
        self.assertIsNone(self.gates(result)["check"])
        self.assertIn("land.no_patches", [f["key"] for f in findings])
        self.assertEqual(self.unbake, [])
        self.assertEqual(self.events[0]["status"], "failed")

    def test_dirty_tree_and_wrong_base_fail_first(self):
        one = self.patch("L1", {"c.c": C_C + "// x\n"})
        (self.tree / "c.c").write_text("dirty\n")
        _, code, findings = self.land([one])
        self.assertEqual((code, findings[0]["key"]), (1, "land.dirty"))
        git(self.tree, "checkout", "-q", "HEAD", "--", ".")
        _, code, findings = self.land([one], base="nonesuch")
        self.assertEqual((code, findings[0]["key"]), (1, "land.base"))
        self.assertEqual(self.unbake, [])

    def test_sync_re_adding_a_stand_in_in_an_untouched_file_fails_the_marker_gate(self):
        one = self.patch("L1", {"a.c": "int func_1(void) { return 1; }\n"})
        self.on_sync = lambda: (self.tree / "c.c").write_text(C_C + 'INCLUDE_ASM(const s32, "c", func_9);\n')
        result, code, findings = self.land([one])
        self.assertEqual(code, 1)
        self.assertEqual(self.gates(result)["markers"], False)
        self.assertEqual(findings[0]["key"], "land.markers")
        self.assertIn("c.c", findings[0]["files"])
        self.assertIsNone(self.gates(result)["readme"])

    def test_marker_added_by_the_patch_itself_is_counted_not_a_gate_failure(self):
        one = self.patch("L1", {"b.c": B_C + 'INCLUDE_ASM(const s32, "b", func_7);\n'})
        result, code, _ = self.land([one])
        self.assertEqual(code, 0)
        self.assertEqual(result["functions"], {"converted": 0, "added": 1})

    def test_untracked_assembly_fails_and_other_untracked_files_are_listed(self):
        one = self.patch("L1", {"c.c": C_C + "// x\n"})

        def sync():
            (self.tree / "asm").mkdir(exist_ok=True)
            (self.tree / "asm" / "f.s").write_text("nop\n")
            (self.tree / "note.txt").write_text("x")

        self.on_sync = sync
        result, code, findings = self.land([one])
        self.assertEqual((code, findings[0]["key"]), (1, "land.untracked_s"))
        self.assertEqual(findings[0]["files"], ["asm/f.s"])
        self.assertEqual(result["untracked_other"], ["note.txt"])

    def test_a_staged_new_assembly_file_from_a_patch_passes(self):
        path = self.tree / "asm" / "g.s"
        path.parent.mkdir()
        path.write_text("nop\n")
        git(self.tree, "add", "-N", ".")
        text = git(self.tree, "diff", "HEAD")
        git(self.tree, "rm", "-qf", "--cached", "asm/g.s")
        path.unlink()
        patch = self.root / "s.patch"
        patch.write_text(text)
        result, code, _ = self.land([("S", patch)])
        self.assertEqual(code, 0)
        self.assertEqual(result["groups"], [{"lane": "S", "files": ["asm/g.s"]}])

    def test_readme_code_percentage_falling_fails(self):
        one = self.patch("L1", {"c.c": C_C + "// x\n"})
        self.on_check = lambda: (self.tree / "README.md").write_text(README.format(a="50.00", b="59.99"))
        _, code, findings = self.land([one])
        self.assertEqual((code, findings[0]["key"]), (1, "land.readme"))
        self.assertEqual(findings[0]["lower"], [{"index": 1, "base": 60.0, "now": 59.99}])

    def test_unbake_refusal_and_check_failure_stop_the_run_with_a_named_finding(self):
        one = self.patch("L1", {"c.c": C_C + "// x\n"})
        self.sync_ok = lambda args: args != ("sync", "--check") and "--check" not in args
        result, code, findings = self.land([one])
        self.assertEqual((code, findings[0]["key"]), (1, "land.sync_check"))
        git(self.tree, "checkout", "-q", "HEAD", "--", ".")
        self.runner = lambda argv, cwd, env, timeout: (
            run_process(argv, cwd, env, timeout)
            if argv[0] == "git"
            else (2, "", "boom")
            if argv[0] == "make"
            else (0, "4", "")
        )
        self.sync_ok = lambda args: True
        result, code, findings = self.land([one])
        self.assertEqual((code, findings[0]["key"], findings[0]["tail"]), (1, "land.check", "boom"))
        self.assertIsNone(self.gates(result)["sync_after"])

    def test_event_writer_failure_fails_the_landing(self):
        one = self.patch("L1", {"c.c": C_C + "// x\n"})

        def boom(path, record):
            raise DecompError("events.writer", "no writer")

        landing = Landing(
            self.profile,
            self.tree,
            self.base,
            [one],
            invoke=self.invoke,
            run=self.runner,
            build=self.runner,
            append=boom,
            pin=lambda: "u",
        )
        result, code, findings = landing.execute()
        self.assertEqual((code, result["pass"], findings[-1]["key"]), (1, False, "events.writer"))

    def test_helpers(self):
        self.assertEqual(parse_patches(["A=/x/a.patch"]), [("A", Path("/x/a.patch"))])
        with self.assertRaises(LandError):
            parse_patches(["A=/x", "A=/y"])
        with self.assertRaises(LandError):
            parse_patches(["nolane"])
        self.assertEqual(function_name('INCLUDE_ASM(const s32, "a/b", func_8_us);'), "func_8_us")
        self.assertEqual(function_name('#pragma GLOBAL_ASM("asm/x/func_9.s")'), "func_9")
        self.assertEqual(code_percentages("code [##] 76.77% (~78%) 1 of 2\ndata [] 5.0%"), [76.77])
        files = diff_lines("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n")
        self.assertEqual(files, {"x": (["new"], ["old"])})

    def test_check_build_streams_its_output_live_and_keeps_the_tail(self):
        seen = io.StringIO()
        with mock.patch("sys.stderr", seen):
            code, tail, _ = build_process(
                [sys.executable, "-c", "import sys; print('one'); print('two', file=sys.stderr); sys.exit(3)"],
                Path.cwd(),
                None,
                30,
            )
        self.assertEqual(code, 3)
        self.assertEqual(sorted(seen.getvalue().split()), ["one", "two"])
        self.assertEqual(sorted(tail.split()), ["one", "two"])


if __name__ == "__main__":
    unittest.main()
