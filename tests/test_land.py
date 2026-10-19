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
from abu_decomp import tools as land_tools
from abu_decomp.config import load_profile
from abu_decomp.land import (
    LandError,
    Landing,
    build_process,
    code_percentages,
    commit_message,
    conflict_steps,
    diff_lines,
    function_name,
    parse_messages,
    parse_patches,
    run_process,
    sentence_list,
    top_dirs,
    union_merge,
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
        self.check_result = lambda: (0, "built\n", "")

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
        return self.check_result()

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
        # L1's explicit message breaks the hook: that input alone is refused before any build, L2 (whose generated
        # message is 'Fix 1 INCLUDE_ASM stand-in ...', also over this hook's 21) is refused for the same reason.
        self.assertEqual(code, 1)
        self.assertFalse(result["pass"])
        self.assertEqual(
            [(f["key"], f.get("lane")) for f in findings if f["key"].startswith("land.commit_")],
            [("land.commit_hook", "L1"), ("land.commit_hook", "L2")],
        )
        self.assertIn("subject too long", findings[0]["message"])
        self.assertEqual(self.unbake, [])  # nothing was built: both inputs were refused up front
        self.assertEqual(git(self.tree, "rev-parse", "HEAD").strip(), self.base)
        self.assertEqual(list((self.tree / ".git").glob("LAND_MSG_*")), [])

    def test_passing_hook_and_over_limit_explicit_subject(self):
        self.hook("#!/bin/sh\nexit 0\n")
        one = self.patch("L1", {"c.c": "int untouched(void) { return 1; }\n"})
        _, code, findings = self.landing([one], {"L1": "x" * 73}).execute()
        self.assertEqual(code, 1)
        self.assertEqual([f["key"] for f in findings][:1], ["land.commit_subject"])
        self.assertEqual(self.unbake, [])

    def test_a_refused_message_refuses_that_input_alone_before_the_build(self):
        self.hook('#!/bin/sh\nif [ "$(head -n1 "$1" | wc -c)" -gt 31 ]; then echo \'subject too long\'; exit 1; fi\n')
        seen = []
        self.on_sync = lambda: seen.append((self.tree / "c.c").read_text())
        bad = self.patch("bad", {"c.c": "int untouched(void) { return 1; }\n"})
        good = self.patch("good", {"b.c": "int func_3(void) { return 3; }\n"})
        long = "Fix a subject that is 75 characters long, and so the hook refuses it......"
        result, code, findings = self.landing([bad, good], {"bad": long, "good": "Fix func_3"}).execute()
        self.assertEqual(code, 0)
        self.assertEqual([r["status"] for r in result["lanes"]], ["excluded", "applied"])
        self.assertEqual([f["key"] for f in findings if f["blocking"]], ["land.commit_subject"])
        self.assertEqual(seen, [C_C, C_C])  # the syncs that ran had not seen the refused input's change
        self.assertEqual([c["lane"] for c in result["commits"]], ["good"])
        self.assertTrue(result["pass"])

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

    def test_setup_runs_every_round_so_changed_facts_are_extracted_again(self):
        one = self.patch("L1", {"c.c": C_C + "// x\n"})
        self.land([one])
        git(self.tree, "checkout", "-q", "HEAD", "--", ".")
        self.unbake.clear()
        result, code, _ = self.land([one])  # the same pin: unbake setup itself answers at once when nothing moved
        self.assertEqual(code, 0)
        self.assertIn(("setup",), self.unbake)
        self.assertEqual([g["gate"] for g in result["gates"]][:3], ["base", "apply", "setup"])
        self.assertFalse(result["gates"][2].get("skipped"))
        self.assertFalse((self.tree / ".git" / "abu-decomp-land.json").exists())

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

    def seed(self, files, union=True):
        """Commit more base files and re-load the profile with (or without) land.union_paths."""
        for name, text in files.items():
            (self.tree / name).parent.mkdir(parents=True, exist_ok=True)
            (self.tree / name).write_text(text)
        git(self.tree, "add", ".")
        git(self.tree, "commit", "-q", "-m", "seed")
        self.base = git(self.tree, "rev-parse", "HEAD").strip()
        seat = json.loads((self.root / "seat.json").read_text())
        if union:
            seat["land"]["union_paths"] = ["versions/*/symbol_addrs.txt"]
        (self.root / "seat.json").write_text(json.dumps(seat))
        self.profile = load_profile(self.root / "seat.json")

    def test_two_lanes_appending_to_a_union_file_both_land(self):
        addrs = "versions/us/symbol_addrs.txt"
        self.seed({addrs: "base = 0x1;\n"})
        one = self.patch("one", {addrs: "base = 0x1;\none = 0x2;\n", "b.c": "int func_3(void) { return 3; }\n"})
        two = self.patch("two", {addrs: "base = 0x1;\ntwo = 0x3;\none = 0x2;\n"})
        result, code, findings = self.land([one, two])
        self.assertEqual(code, 0)
        self.assertEqual([r["status"] for r in result["lanes"]], ["applied", "applied"])
        self.assertEqual((self.tree / addrs).read_text(), "base = 0x1;\none = 0x2;\ntwo = 0x3;\n")
        self.assertEqual([f["key"] for f in findings if f["key"] != "land.commit_message"], [])
        self.assertEqual(result["lanes"][1]["files"], [addrs])

    def test_without_union_paths_the_second_append_conflicts_and_is_refused(self):
        addrs = "versions/us/symbol_addrs.txt"
        self.seed({addrs: "base = 0x1;\n"}, union=False)
        one = self.patch("one", {addrs: "base = 0x1;\none = 0x2;\n"})
        two = self.patch("two", {addrs: "base = 0x1;\ntwo = 0x3;\n", "b.c": "int func_3(void) { return 3; }\n"})
        result, code, _findings = self.land([one, two])
        self.assertEqual(code, 0)
        self.assertEqual(result["lanes"][1]["status"], "applied")
        self.assertEqual([r["file"] for r in result["lanes"][1]["refused"]], [addrs])
        self.assertEqual(result["lanes"][1]["files"], ["b.c"])
        self.assertEqual(git(self.tree, "ls-files", "-u"), "")

    def test_a_conflicting_file_is_refused_and_the_rest_of_the_patch_lands(self):
        mixed = self.patch(
            "mixed", {"a.c": "int func_1(void) { return 1; }\n", "b.c": "int func_3(void) { return 3; }\n"}
        )
        (self.tree / "a.c").write_text("totally different\n")
        git(self.tree, "commit", "-qam", "moved")
        moved = git(self.tree, "rev-parse", "HEAD").strip()
        result, code, findings = self.land([mixed], base=moved)
        self.assertEqual(code, 0)
        row = result["lanes"][0]
        self.assertEqual(row["status"], "applied")
        self.assertEqual(row["files"], ["b.c"])
        self.assertEqual([r["file"] for r in row["refused"]], ["a.c"])
        self.assertEqual(row["functions"]["names"], ["func_3"])
        refused = [f for f in findings if f["key"] == "land.file_refused"]
        self.assertEqual([(f["file"], f["blocking"], f["lane"]) for f in refused], [("a.c", True, "mixed")])
        self.assertEqual((self.tree / "a.c").read_text(), "totally different\n")
        self.assertEqual(result["commits"][0]["files"], ["b.c"])
        self.assertIn("REFUSED 1 file(s): mixed:a.c", self.events[-1]["message"])
        self.assertEqual(self.events[-1]["payload"]["refused"][0]["file"], "a.c")

    def test_a_function_split_across_a_refused_file_is_not_credited(self):
        self.seed({"asm/func_1.s": "nop\n"})
        split = self.patch(
            "split",
            {
                "a.c": '#include "x.h"\nint func_1(void) { return 1; }\nINCLUDE_ASM(const s32, "a", func_2);\n',
                "asm/func_1.s": "gone\n",
                "b.c": "int func_3(void) { return 3; }\n",
            },
        )
        (self.tree / "asm/func_1.s").write_text("moved\n")
        git(self.tree, "commit", "-qam", "moved")
        moved = git(self.tree, "rev-parse", "HEAD").strip()
        result, _code, findings = self.land([split], base=moved)
        row = result["lanes"][0]
        self.assertEqual([r["file"] for r in row["refused"]], ["asm/func_1.s"])
        self.assertEqual(row["files"], ["a.c", "b.c"])
        self.assertEqual(row["functions"]["names"], ["func_3"])
        self.assertEqual(row["functions"]["converted"], 1)
        self.assertEqual(row["functions"]["split"], ["func_1"])
        self.assertIn("land.function_split", [f["key"] for f in findings])

    def test_a_function_two_patches_both_carry_lands_once(self):
        stand = "".join(f'INCLUDE_ASM(const s32, "d", func_{n});\n' for n in (1, 2, 3))
        self.seed({"d.c": '#include "x.h"\n' + stand + "int tail(void) { return 9; }\n"})

        def body(n, value):
            return f"int func_{n}(void) {{ return {value}; }}\n"

        def text(*rows):
            return '#include "x.h"\n' + "".join(rows) + "int tail(void) { return 9; }\n"

        stand_row = lambda n: f'INCLUDE_ASM(const s32, "d", func_{n});\n'  # noqa: E731
        one = self.patch("one", {"d.c": text(body(1, 1), stand_row(2), stand_row(3))})
        two = self.patch("two", {"d.c": text(body(1, 1), body(2, 2), stand_row(3))})
        three = self.patch("three", {"d.c": text(stand_row(1), body(2, 22), body(3, 3))})
        result, code, findings = self.land([one, two, three])
        self.assertEqual(code, 0)
        self.assertEqual((self.tree / "d.c").read_text(), text(body(1, 1), body(2, 2), body(3, 3)))
        rows = {row["lane"]: row for row in result["lanes"]}
        self.assertEqual([r["status"] for r in rows.values()], ["applied"] * 3)
        self.assertEqual([r["refused"] for r in rows.values()], [[], [], []])
        self.assertEqual(rows["two"]["functions"]["names"], ["func_2"])
        self.assertEqual(rows["three"]["functions"]["names"], ["func_3"])
        keys = [f["key"] for f in findings if f["key"] != "land.commit_message"]
        self.assertEqual(keys, ["land.duplicate_function"])
        duplicate = next(f for f in findings if f["key"] == "land.duplicate_function")
        self.assertEqual(
            (duplicate["lane"], duplicate["kept_lane"], duplicate["functions"]), ("three", ["two"], ["func_2"])
        )
        self.assertFalse(duplicate["blocking"])
        self.assertIn("three", duplicate["message"])
        self.assertIn("two", duplicate["message"])

    def test_a_wrapped_function_another_patch_converted_differently_is_dropped_with_its_whole_hunk(self):
        wrapped = (
            '#ifdef NON_MATCHING\n#include "TYPES.H"\ns32 func_1(const s32 *t) {\n    return t[0];\n}\n'
            '#else\nINCLUDE_ASM(const s32, "d", func_1);\n#endif\n'
        )
        head = "s32 helper(void) { return 0; }\n" + "".join(f"/* filler {n} */\n" for n in range(8))
        tail = "int tail(void) { return 9; }\n"
        self.seed({"d.c": head + wrapped + tail})
        first = self.patch("first", {"d.c": head + "s32 func_1(const s32 *t) { return t[1]; }\n" + tail})
        second_body = "s32 func_1(const s32 *t) {\n    return t[2];\n}\n"
        second = self.patch(
            "second",
            {"d.c": head.replace("return 0;", "return 7;") + second_body + tail},
        )
        result, code, findings = self.land([first, second])
        self.assertEqual(code, 0)
        self.assertEqual(
            (self.tree / "d.c").read_text(),
            head.replace("return 0;", "return 7;") + "s32 func_1(const s32 *t) { return t[1]; }\n" + tail,
        )
        self.assertEqual([r["refused"] for r in result["lanes"]], [[], []])
        self.assertEqual(result["lanes"][0]["functions"]["names"], ["func_1"])
        self.assertEqual(result["lanes"][1]["functions"]["names"], [])
        self.assertEqual(
            [f["key"] for f in findings if f["key"] == "land.duplicate_function"], ["land.duplicate_function"]
        )

    def test_symbol_lines_merge_by_name_and_an_input_with_a_conflicting_address_is_refused_alone(self):
        addrs = "versions/us/symbol_addrs.txt"
        self.seed({addrs: "base = 0x1; // type:func\n"})
        head = "base = 0x1; // type:func\n"
        one = self.patch("one", {addrs: head + "foo = 0x10; // type:func\n"})
        two = self.patch("two", {addrs: head + "foo = 0x10; // type:func size:0x4\nbar = 0x20; // type:func\n"})
        three = self.patch("three", {addrs: head + "bar = 0x30; // type:func\nbaz = 0x40;\n"})
        result, code, findings = self.land([one, two, three])
        self.assertEqual(code, 0)
        self.assertEqual((self.tree / addrs).read_text(), head + "foo = 0x10; // type:func\nbar = 0x20; // type:func\n")
        self.assertEqual([r["status"] for r in result["lanes"]], ["applied", "applied", "excluded"])
        conflict = [f for f in findings if f["key"] == "land.symbol_conflict"]
        self.assertEqual(len(conflict), 1)
        self.assertEqual((conflict[0]["name"], conflict[0]["kept"], conflict[0]["refused"]), ("bar", "0x20", "0x30"))
        self.assertEqual(conflict[0]["lane"], "three")
        self.assertIn("0x20", conflict[0]["message"])
        self.assertIn("0x30", conflict[0]["message"])
        self.assertIn("0x30", result["lanes"][2]["reason"])  # the refusal names the conflict
        self.assertEqual(result["gates"][1]["excluded"][0]["lane"], "three")

    def test_an_input_with_a_line_splat_rejects_is_refused_alone_with_its_reason(self):
        addrs = "versions/us/symbol_addrs.txt"
        self.seed({addrs: "base = 0x1;\n"})
        one = self.patch("one", {addrs: "base = 0x1;\nok = 0x2; // type:func\nbad = 0x3; // type:data\n"})
        two = self.patch("two", {addrs: "base = 0x1;\nfine = 0x5;\n"})
        seen = []

        def check(lines):
            seen.append(list(lines))
            return {line: "Unrecognized symbol type in 'type:data'" for line in lines if "type:data" in line}

        append = lambda path, record: {"ok": True}  # noqa: E731
        landing = Landing(
            self.profile, self.tree, self.base, [one, two], invoke=self.invoke, run=self.runner, build=self.runner,
            append=append, pin=lambda: "u1", splat_check=check,
        )  # fmt: skip
        result, code, findings = landing.execute()
        self.assertEqual(code, 0)
        self.assertEqual((self.tree / addrs).read_text(), "base = 0x1;\nfine = 0x5;\n")  # nothing of input one landed
        self.assertEqual(seen[0], ["ok = 0x2; // type:func", "bad = 0x3; // type:data"])
        self.assertEqual([r["status"] for r in result["lanes"]], ["excluded", "applied"])
        invalid = [f for f in findings if f["key"] == "land.symbol_invalid"]
        self.assertEqual(len(invalid), 1)
        self.assertTrue(invalid[0]["blocking"])
        self.assertEqual(invalid[0]["line"], "bad = 0x3; // type:data")
        self.assertIn("type:data", invalid[0]["message"])
        self.assertNotIn("symbol_check", result)

    def landing_with(self, patches, **hooks):
        return Landing(
            self.profile, self.tree, self.base, patches, invoke=self.invoke, run=self.runner, build=self.runner,
            append=lambda path, record: {"ok": True}, pin=lambda: "u1", **hooks,
        )  # fmt: skip

    def test_a_move_retracts_the_old_line_and_a_clash_in_the_merged_file_refuses_only_that_input(self):
        addrs = "versions/de/symbol_addrs.txt"
        old = "func_8043DADC_de = 0x8043DADC; // type:func rom:0x1A3ADC\n"
        moved = (
            "func_8043DADC_de_padding_1A3ADC = 0x8043DADC; // rom:0x1A3ADC size:0x4\nfunc_8043DADC_de = 0x8043DAE0;\n"
        )
        self.seed({addrs: "base = 0x1;\n" + old})
        move = self.patch("move", {addrs: "base = 0x1;\n" + moved})
        add = self.patch("add", {addrs: "base = 0x1;\n" + old + "other = 0x8043DADC;\n"})  # a second symbol there
        calls = []

        def whole(paths):
            calls.append([(p, (self.tree / p).read_text()) for p in paths])
            text = (self.tree / addrs).read_text()
            if text.count("= 0x8043DADC;") > 1:
                return {addrs: "Duplicate symbol detected! b clashes with a defined at vram 0x8043DADC."}
            return {}

        result, code, findings = self.landing_with([move, add], splat_file_check=whole).execute()
        self.assertEqual(code, 0)
        self.assertEqual([r["status"] for r in result["lanes"]], ["applied", "excluded"])
        text = (self.tree / addrs).read_text()
        self.assertIn("func_8043DADC_de = 0x8043DAE0;", text)
        self.assertNotIn("func_8043DADC_de = 0x8043DADC", text)
        self.assertNotIn("other", text)  # nothing of the refused input landed
        clash = [f for f in findings if f["key"] == "land.symbol_clash"]
        self.assertEqual(len(clash), 1)
        self.assertEqual(clash[0]["lane"], "add")
        self.assertIn("clashes with", clash[0]["message"])
        self.assertEqual([[p for p, _ in call] for call in calls], [[addrs], [addrs]])

    def test_union_merge_retracts_only_symbol_lines_the_tree_holds_exactly(self):
        merged, conflicts = union_merge(
            "a = 0x1;\nb = 0x2;\n# note\n", ["b = 0x3;", "c = 0x4;"], ["a = 0x1;", "# note", "z = 0x9;", "b = 0x7;"]
        )
        self.assertEqual(merged, "b = 0x2;\n# note\nc = 0x4;\n")  # a retracted; b's stale removal is not exact
        self.assertEqual([c["name"] for c in conflicts], ["b"])

    def test_a_tool_provenance_refusal_comes_before_any_input_is_written_to_the_tree(self):
        one = self.patch(
            "L1", {"a.c": A_C.replace('INCLUDE_ASM(const s32, "a", func_1);\n', "int func_1(void) { return 1; }\n")}
        )

        def refuse():
            raise DecompError("dependency.provenance", "unbake has invalid VCS provenance")

        result, code, findings = self.landing_with([one], ready=refuse).execute()
        self.assertEqual(code, 1)
        self.assertEqual([f["key"] for f in findings], ["dependency.provenance"])
        self.assertEqual(git(self.tree, "status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual((self.tree / "a.c").read_text(), A_C)
        self.assertEqual((self.unbake, self.commands), ([], []))  # no tool ran either
        self.assertEqual(result["lanes"], [])

    def test_the_real_invoke_checks_the_tools_on_the_tree_and_a_fake_one_does_not(self):
        with mock.patch("abu_decomp.land.tools.ready") as ready:
            Landing(self.profile, self.tree, self.base, [], invoke=land_tools.invoke).ready()
            Landing(self.profile, self.tree, self.base, [], invoke=self.invoke).ready()
        ready.assert_called_once_with(self.profile, self.tree.resolve())

    def test_a_move_retracts_the_tree_line_whatever_its_attributes_say(self):
        merged, conflicts = union_merge(
            "f = 0x10; // type:func rom:0x10 size:0x20\n",
            ["pad = 0x10; // size:0x4", "f = 0x14;"],
            ["f = 0x10; // type:func"],
        )
        self.assertEqual(merged, "pad = 0x10; // size:0x4\nf = 0x14;\n")
        self.assertEqual(conflicts, [])

    def test_a_conflict_after_the_tree_moved_the_symbol_says_to_export_the_patch_again(self):
        merged, conflicts = union_merge("f = 0x30;\n", ["f = 0x14;"], ["f = 0x10;"])
        self.assertEqual(merged, "f = 0x30;\n")
        self.assertEqual((conflicts[0]["kept"], conflicts[0]["base"]), ("0x30", "0x10"))
        self.assertIn("export the patch again", conflict_steps(conflicts[0]))
        _, bare = union_merge("f = 0x30;\n", ["f = 0x14;"])
        self.assertIsNone(bare[0]["base"])
        self.assertIn("remove its old line", conflict_steps(bare[0]))

    def build_rounds(self, bad, count=5):
        """count inputs d1..dN each touching its own file; check fails while a file holds BAD."""
        self.seed({f"d{i}.c": f"int d{i}(void) {{ return 0; }}\n" for i in range(1, count + 1)})
        patches = [
            self.patch(f"L{i}", {f"d{i}.c": "BAD\n" if i in bad else f"int d{i}(void) {{ return {i}; }}\n"})
            for i in range(1, count + 1)
        ]
        builds = []

        def result():
            builds.append(
                sorted(
                    p.name
                    for p in self.tree.glob("d*.c")
                    if p.read_text().startswith("int d") and "return 0" not in p.read_text()
                )
            )
            broken = [p.name for p in self.tree.glob("d*.c") if "BAD" in p.read_text()]
            return (1, "", f"ld: {broken[0]}: BAD") if broken else (0, "built\n", "")

        self.check_result = result
        return patches, builds

    def test_a_build_failure_is_bisected_by_input_and_only_the_bad_input_is_dropped(self):
        patches, builds = self.build_rounds({3})
        result, code, findings = self.landing_with(patches).execute()
        self.assertEqual((code, result["pass"]), (0, True))
        self.assertEqual(
            [r["status"] for r in result["lanes"]], ["applied", "applied", "excluded", "applied", "applied"]
        )
        self.assertNotIn("BAD", "".join(p.read_text() for p in self.tree.glob("d*.c")))
        self.assertEqual(sorted(p.name for p in self.tree.glob("d*.c")), [f"d{i}.c" for i in range(1, 6)])
        self.assertEqual([r["lane"] for r in result["refused_inputs"]], ["L3"])
        failed = [f for f in findings if f["key"] == "land.input_build_failed"]
        self.assertEqual((len(failed), failed[0]["lane"]), (1, "L3"))
        self.assertIn("BAD", failed[0]["message"])
        self.assertEqual(len(builds), 1 + 2 + 1)  # prefixes of 2 (passes) and 3 (fails) find the third input
        self.assertLessEqual(len(builds) - 2, 3)  # never more than ceil(log2 5) probes
        self.assertEqual(len(result["attempts"]), len(builds) - 1)  # the last build is the result's own gates
        self.assertTrue(all(g["pass"] for g in result["gates"] if g["pass"] is not None))
        self.assertEqual([c["lane"] for c in result["commits"]], ["L1", "L2", "L4", "L5"])

    def test_two_bad_inputs_are_both_dropped_and_a_failing_lone_input_is_not_bisected(self):
        patches, _ = self.build_rounds({2, 5})
        result, code, _ = self.landing_with(patches).execute()
        self.assertEqual(code, 0)
        self.assertEqual(
            [r["status"] for r in result["lanes"]], ["applied", "excluded", "applied", "applied", "excluded"]
        )
        self.assertEqual([r["lane"] for r in result["refused_inputs"]], ["L2", "L5"])

    def test_a_round_that_fails_with_one_input_left_reports_the_failure_and_drops_nothing(self):
        patches, builds = self.build_rounds({1}, count=1)
        result, code, findings = self.landing_with(patches).execute()
        self.assertEqual((code, result["pass"], len(builds)), (1, False, 1))
        self.assertIn("land.check", [f["key"] for f in findings])
        self.assertEqual(result["refused_inputs"], [])

    def test_a_replay_removes_files_a_failed_build_left_and_keeps_the_rest(self):
        patches, _ = self.build_rounds({2}, count=3)
        self.on_check = lambda: (self.tree / "stray.s").write_text("x\n")
        result, code, _ = self.landing_with(patches).execute()
        self.assertEqual(code, 1)  # the stray assembly of the last build is the untracked_s gate's finding
        self.assertEqual([r["lane"] for r in result["refused_inputs"]], ["L2"])
        self.assertEqual(result["untracked_other"], [])

    def test_union_paths_must_be_a_list_of_globs(self):
        seat = json.loads((self.root / "seat.json").read_text())
        seat["land"]["union_paths"] = "versions/*"
        (self.root / "seat.json").write_text(json.dumps(seat))
        self.profile = load_profile(self.root / "seat.json")
        with self.assertRaises(DecompError):
            self.land([])

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

    def test_a_refused_land_leaves_the_tree_exactly_as_it_found_it(self):
        one = self.patch("L1", {"c.c": C_C + "// x\n"})

        def check():
            (self.tree / "README.md").write_text(README.format(a="50.00", b="59.99"))
            (self.tree / "made.txt").write_text("x")

        self.on_check = check
        _, code, findings = self.land([one])
        self.assertEqual((code, findings[0]["key"]), (1, "land.readme"))
        self.assertEqual(git(self.tree, "status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual(git(self.tree, "rev-parse", "HEAD").strip(), self.base)
        self.assertFalse((self.tree / "made.txt").exists())

    def test_a_land_that_raises_leaves_the_tree_clean_too(self):
        one = self.patch("L1", {"c.c": C_C + "// x\n"})
        self.on_check = lambda: (_ for _ in ()).throw(LandError("land.git", "boom"))
        with self.assertRaises(LandError):
            self.land([one])
        self.assertEqual(git(self.tree, "status", "--porcelain"), "")

    def test_a_land_refused_for_a_dirty_tree_keeps_the_owners_changes(self):
        one = self.patch("L1", {"c.c": C_C + "// x\n"})
        (self.tree / "c.c").write_text("dirty\n")
        _, code, findings = self.land([one])
        self.assertEqual((code, findings[0]["key"]), (1, "land.dirty"))
        self.assertEqual((self.tree / "c.c").read_text(), "dirty\n")

    def test_a_successful_land_keeps_its_changes_in_the_tree(self):
        one = self.patch("L1", {"c.c": C_C + "// x\n"})
        self.on_check = lambda: (self.tree / "README.md").write_text(README.format(a="51.00", b="60.00"))
        _, code, _ = self.land([one])
        self.assertEqual(code, 0)
        self.assertIn("c.c", git(self.tree, "status", "--porcelain"))
        self.assertTrue((self.tree / "c.c").read_text().endswith("// x\n"))

    def test_a_falling_functions_percentage_beside_a_rising_code_one_is_not_a_code_loss(self):
        row = "<pre><code>code      [###]  {c}%  1 of 2</code><br><code>functions [###]  {f}%  3 of 4</code></pre>"
        self.assertEqual(code_percentages(row.format(c="77.44", f="90.67")), [77.44])
        self.assertEqual(code_percentages(row.format(c="77.55", f="90.58")), [77.55])
        self.assertEqual(code_percentages(README.format(a="50.00", b="60.00")), [50.0, 60.0])

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
