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
    commit_message,
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
        self.unbake, self.commands, self.events, self.recipes = [], [], [], []
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
        self.assertEqual(project, self.tree)
        if tool == "recipe":
            self.recipes.append(tuple(args))
            ok = args[0] != "name"
            result = {"moves": 2, "files": ["a.c", "x.c"], "suggestions": [{"to": "a/b.c"}]}
            document = {"ok": ok, **(result if ok else {}), "findings": [] if ok else [{"message": "no map"}]}
            return SimpleNamespace(ok=ok, returncode=0 if ok else 1, document=document)
        self.unbake.append(tuple(args))
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

    def with_recipe(self, steps):
        document = json.loads((self.root / "seat.json").read_text())
        document["land"]["recipe"] = steps
        (self.root / "seat.json").write_text(json.dumps(document))
        self.profile = load_profile(self.root / "seat.json")

    def test_recipe_proposals_run_on_the_landed_files_and_ride_the_result_and_the_event(self):
        self.with_recipe(
            [
                {"name": "structure", "args": ["structure", "--json", "--paths", "{files}"]},
                {"name": "names", "args": ["name", "next", "--json"]},
            ]
        )
        one = self.patch(
            "L1", {"a.c": A_C.replace('INCLUDE_ASM(const s32, "a", func_1);\n', "int func_1(void) { return 1; }\n")}
        )
        result, code, findings = self.land([one])
        self.assertEqual(code, 0)  # a refusing step informs; the landing still passes
        self.assertEqual(self.recipes[0], ("structure", "--json", "--paths", "a.c"))
        self.assertEqual(result["recipe"]["structure"]["moves"], 2)
        self.assertEqual(result["recipe"]["names"], {"ok": False, "error": "no map"})
        self.assertEqual([f["key"] for f in findings], ["land.recipe"])
        self.assertFalse(findings[0]["blocking"])
        self.assertTrue(self.gates(result)["recipe"])
        self.assertIn("recipe proposes structure 2 moves", self.events[0]["message"])
        self.assertEqual(self.events[0]["payload"]["recipe"]["structure"]["suggestions"], 1)
        self.assertEqual(self.git_status(), "")  # proposals never touch tracked files beyond the patches' own

    def git_status(self):
        return "".join(
            line + "\n" for line in git(self.tree, "status", "--porcelain").splitlines() if not line.startswith("M ")
        )

    def test_a_failed_gate_runs_no_recipe(self):
        self.with_recipe([{"name": "structure", "args": ["structure", "--json", "--paths", "{files}"]}])
        one = self.patch("L1", {"a.c": A_C.replace("func_1", "func_9")})
        self.sync_ok = lambda args: args[0] != "sync"
        result, code, _ = self.land([one])
        self.assertEqual(code, 1)
        self.assertEqual(self.recipes, [])
        self.assertIsNone(self.gates(result)["recipe"])

    def test_recipe_steps_that_write_the_tree_are_refused_in_the_profile(self):
        with self.assertRaises(DecompError):
            self.with_recipe([{"name": "apply", "args": ["apply"]}])
            self.land([])

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

    def test_subject_is_capped_with_the_function_list_in_the_body(self):
        names = [f"func_{8025522 + i}_us" for i in range(10)]
        dirs = ["GFX", "MENU", "TEXTURE"]
        subject, body = commit_message("INCLUDE_ASM", names, dirs, 72)
        self.assertEqual(subject, "Fix 10 INCLUDE_ASM stand-ins in GFX, MENU and TEXTURE by matching C")
        self.assertLessEqual(len(subject), 72)
        self.assertEqual(body, "Matched as C: " + ", ".join(names))
        many = [f"DIR{i}" for i in range(12)]
        subject, body = commit_message("INCLUDE_ASM", names, many, 72)
        self.assertEqual(subject, "Fix 10 INCLUDE_ASM stand-ins in DIR0 and 11 more dirs by matching C")
        subject, _ = commit_message("INCLUDE_ASM", names, many[:5], 72)
        self.assertEqual(subject, "Fix 10 INCLUDE_ASM stand-ins in DIR0, DIR1 and 3 more dirs by matching C")
        self.assertEqual(commit_message("INCLUDE_ASM", names, many, 30)[0], "Fix 10 INCLUDE_ASM stand-ins")
        self.assertEqual(commit_message("INCLUDE_ASM", names, [], 72)[0], "Fix 10 INCLUDE_ASM stand-ins by matching C")

    def test_subject_of_exactly_the_limit_keeps_the_names(self):
        full = "Fix 2 INCLUDE_ASM stand-ins in AB by matching aaaa and bbbb as C"
        self.assertEqual(commit_message("INCLUDE_ASM", ["aaaa", "bbbb"], ["AB"], len(full)), (full, None))
        shorter = commit_message("INCLUDE_ASM", ["aaaa", "bbbb"], ["AB"], len(full) - 1)
        self.assertEqual(shorter, ("Fix 2 INCLUDE_ASM stand-ins in AB by matching C", "Matched as C: aaaa, bbbb"))

    def test_long_lists_land_with_a_short_subject_and_a_body_paragraph(self):
        names = [f"func_{i:04d}_long_name_to_overflow" for i in range(6)]
        (self.tree / "a.c").write_text("".join(f'INCLUDE_ASM(const s32, "a", {n});\n' for n in names))
        git(self.tree, "commit", "-qam", "many")
        base = git(self.tree, "rev-parse", "HEAD").strip()
        one = self.patch("L1", {"a.c": "".join(f"int {n}(void) {{ return 1; }}\n" for n in names)})
        result, code, _ = self.land([one], base=base)
        self.assertEqual(code, 0)
        plan = result["commits"][0]
        self.assertLessEqual(len(plan["message"]), 72)
        self.assertEqual(plan["body"], "Matched as C: " + ", ".join(names))
        command = result["commit_commands"][0]
        self.assertIn(f"-m 'Matched as C: {', '.join(names)}' -- a.c", command)
        self.assertEqual(command.count(" -m "), 2)

    def hook(self, text):
        path = self.tree / ".git" / "hooks" / "commit-msg"
        path.write_text(text)
        path.chmod(0o755)

    def landing(self, patches, messages):
        return Landing(
            self.profile,
            self.tree,
            self.base,
            patches,
            invoke=self.invoke,
            run=self.runner,
            build=self.runner,
            append=lambda path, record: {"ok": True},
            pin=lambda: "u1",
            messages=messages,
        )

    def test_commit_msg_hook_violation_is_a_finding_before_any_commit(self):
        self.hook('#!/bin/sh\nif [ "$(head -n1 "$1" | wc -c)" -gt 21 ]; then echo \'subject too long\'; exit 1; fi\n')
        one = self.patch("L1", {"c.c": "int untouched(void) { return 1; }\n"})
        two = self.patch("L2", {"b.c": "int func_3(void) { return 3; }\n"})
        result, code, findings = self.landing([one, two], {"L1": "Fix the pin: a long subject"}).execute()
        self.assertEqual(code, 1)
        self.assertFalse(result["pass"])
        self.assertEqual(
            [(f["key"], f["lane"]) for f in findings], [("land.commit_hook", "L1"), ("land.commit_hook", "L2")]
        )
        self.assertIn("subject too long", findings[0]["message"])
        self.assertEqual(git(self.tree, "rev-parse", "HEAD").strip(), self.base)
        self.assertEqual(list((self.tree / ".git").glob("LAND_MSG_*")), [])

    def test_passing_hook_and_over_limit_explicit_subject(self):
        self.hook("#!/bin/sh\nexit 0\n")
        one = self.patch("L1", {"c.c": "int untouched(void) { return 1; }\n"})
        _, code, findings = self.landing([one], {"L1": "x" * 73}).execute()
        self.assertEqual((code, [f["key"] for f in findings]), (1, ["land.commit_subject"]))

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
        self.assertEqual(top_dirs(["src/MENU/EVENT/z.c", "src/AI/NAV/x.c", "src/AI/y.c", "README.md"]), ["AI", "MENU"])
        self.assertEqual(top_dirs(["include/A/x.h", "versions/de/t.mk", "src/SDK/a.c", "src/sdk/b.c"]), ["SDK"])
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
