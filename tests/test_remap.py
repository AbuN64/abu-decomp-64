"""`decomp port remap` and the remap step of `decomp repin`: renames of the source reach the port's facts. Real git."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from abu_decomp import cli, land, remap, repin
from abu_decomp.config import load_profile
from abu_decomp.land import run_process

FACTS = ["versions/v/symbol_addrs.txt", "versions/v/aliases.txt"]
OLD = """\
func_80001000_de = 0x80001000; // type:func size:0x10
func_80002000_de = 0x80002000; // type:func size:0x10
func_80003000_de = 0x80003000; // type:func size:0x10
func_80004000_de = 0x80004000; // type:func size:0x10
func_80005000_de = 0x80005000; // type:func size:0x10
D_800E0000 = 0x800E0000;
D_800E0010 = 0x800E0010;
"""
ALIASES_OLD = "D_800E0010_alias = 0x800E0010;\n"
NEW = """\
Math_Sqrtf = 0x80001000; // type:func size:0x10
func_80002000_de = 0x80002000; // type:func size:0x10
Two_Names_A = 0x80003000; // type:func size:0x10
func_80005000_de = 0x80005000; // type:func size:0x10
rspbootTextStart = 0x800E0000;
D_800E0010 = 0x800E0010;
"""
ALIASES_NEW = "Two_Names_B = 0x80003000; // type:func\n"


def git(root: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t"}
    env["GIT_COMMITTER_EMAIL"] = "t@t"
    return subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, text=True, check=True).stdout


def parsed(text: str) -> dict[tuple[int, str], set[str]]:
    return remap.parse(text)


class RemapTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source, self.tree = self.root / "source", self.root / "port"
        (self.source / "versions/v").mkdir(parents=True)
        git(self.source, "init", "-q", "-b", "main")
        (self.source / "versions/v/symbol_addrs.txt").write_text(OLD)
        (self.source / "versions/v/aliases.txt").write_text(ALIASES_OLD)
        git(self.source, "add", ".")
        git(self.source, "commit", "-q", "-m", "old")
        self.old = git(self.source, "rev-parse", "HEAD").strip()
        (self.source / "versions/v/symbol_addrs.txt").write_text(NEW)
        (self.source / "versions/v/aliases.txt").write_text(ALIASES_NEW)
        git(self.source, "commit", "-qam", "new")
        self.new = git(self.source, "rev-parse", "HEAD").strip()
        (self.tree / "platform/port").mkdir(parents=True)
        (self.tree / "cmake").mkdir()
        (self.tree / "cmake/decomp.cmake").write_text(f"GIT_TAG {self.old}\n")
        self.manifest = self.tree / "platform/port/manifest.json"
        self.manifest.write_text('{"entry": "func_80001000_de", "keep": "func_80002000_de", "ptr": "D_800E0000"}\n')
        (self.tree / "platform/port/notes.md").write_text("func_80001000_de_extra and xfunc_80001000_de stay.\n")
        (self.tree / "platform/port/blob.bin").write_bytes(b"\xff\xfe func_80001000_de")
        (self.root / "seat.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "seat",
                    "tools": {"project": "port", "host": "host.toml", "timeout_seconds": 5},
                    "events": {"path": "events.jsonl"},
                    "land": {
                        "check": ["make", "check"],
                        "author": "mo <mo@example.org>",
                        "evidence": {"roms": []},
                        "trees": {
                            "port": {
                                "mode": "plain",
                                "steps": [{"name": "build", "argv": ["true"]}],
                                "pin": {
                                    "source": "source",
                                    "ref": "main",
                                    "files": [{"path": "cmake/decomp.cmake", "pattern": "GIT_TAG ([0-9a-f]{40})"}],
                                    "remap": {"facts": FACTS, "paths": ["platform/port"]},
                                },
                            }
                        },
                    },
                }
            )
        )
        (self.root / "events.jsonl").write_text("")
        (self.root / "host.toml").write_text("")
        self.profile = load_profile(self.root / "seat.json")

    def pin(self) -> dict[str, Any]:
        return land.settings(self.profile, self.tree)["pin"]

    def test_a_one_for_one_address_is_a_rename_and_many_for_many_is_ambiguous(self) -> None:
        found = remap.renames(parsed(OLD + ALIASES_OLD), parsed(NEW + ALIASES_NEW))
        self.assertEqual(found["renames"], {"func_80001000_de": "Math_Sqrtf", "D_800E0000": "rspbootTextStart"})
        self.assertEqual(found["gone"], {"func_80004000_de", "func_80003000_de", "D_800E0010_alias"})
        self.assertEqual([a["address"] for a in found["ambiguous"]], ["0x80003000"])

    def test_two_names_lost_at_one_address_are_ambiguous_not_guessed(self) -> None:
        old = parsed("a = 0x1; // type:func\nb = 0x1; // type:func\n")
        new = parsed("c = 0x1; // type:func\n")
        found = remap.renames(old, new)
        self.assertEqual((found["renames"], found["gone"]), ({}, {"a", "b"}))
        self.assertEqual(found["ambiguous"][0]["lost"], ["a", "b"])

    def test_a_name_the_source_still_defines_elsewhere_is_never_renamed(self) -> None:
        old = parsed("a = 0x1; // type:func\nb = 0x2; // type:func\n")
        new = parsed("a = 0x2; // type:func\nc = 0x1; // type:func\n")
        self.assertEqual(remap.renames(old, new)["renames"], {"b": "a"})

    def test_remap_rewrites_whole_words_in_text_files_only_and_reports_what_it_did(self) -> None:
        result, findings = remap.remap(run_process, self.tree, self.pin(), self.old, self.new)
        self.assertEqual([(f["key"], f["blocking"]) for f in findings], [("remap.ambiguous", False)])
        self.assertEqual(result["renamed"], 2)
        self.assertEqual(result["files"], {"platform/port/manifest.json": 2})
        self.assertEqual(json.loads(self.manifest.read_text())["entry"], "Math_Sqrtf")
        self.assertEqual(json.loads(self.manifest.read_text())["ptr"], "rspbootTextStart")
        self.assertIn(
            "func_80001000_de_extra and xfunc_80001000_de", (self.tree / "platform/port/notes.md").read_text()
        )
        self.assertEqual((self.tree / "platform/port/blob.bin").read_bytes(), b"\xff\xfe func_80001000_de")

    def test_check_writes_nothing(self) -> None:
        before = self.manifest.read_text()
        result, _ = remap.remap(run_process, self.tree, self.pin(), self.old, self.new, write=False)
        self.assertEqual((result["spellings"], result["written"], self.manifest.read_text()), (2, False, before))

    def test_a_dropped_name_the_tree_still_spells_is_a_blocking_finding(self) -> None:
        self.manifest.write_text('{"x": "func_80004000_de"}\n')
        _, findings = remap.remap(run_process, self.tree, self.pin(), self.old, self.new)
        self.assertEqual(
            [(f["key"], f["blocking"]) for f in findings], [("remap.stale", True), ("remap.ambiguous", False)]
        )
        self.assertIn("func_80004000_de", findings[0]["message"])

    def test_repin_moves_the_pin_and_the_names_in_one_change(self) -> None:
        result, code, findings = repin.repin(self.profile, self.tree, None, rebuild=False)
        self.assertEqual(code, 0, findings)
        self.assertEqual(result["remap"]["renamed"], 2)
        self.assertEqual(sorted(result["changed"]), ["cmake/decomp.cmake", "platform/port/manifest.json"])
        self.assertIn(self.new, (self.tree / "cmake/decomp.cmake").read_text())
        self.assertIn("Math_Sqrtf", self.manifest.read_text())

    def test_repin_refuses_while_the_tree_spells_a_dropped_name(self) -> None:
        self.manifest.write_text('{"x": "func_80004000_de"}\n')
        _, code, findings = repin.repin(self.profile, self.tree, None, rebuild=False)
        self.assertEqual(code, 1)
        self.assertEqual([f["key"] for f in findings], ["remap.stale"])
        self.assertIn("func_80004000_de", findings[0]["message"])

    def test_the_port_remap_verb_runs_since_a_commit_and_check_is_read_only(self) -> None:
        argv = [
            "--profile",
            str(self.root / "seat.json"),
            "port",
            "remap",
            "--tree",
            str(self.tree),
            "--since",
            self.old,
        ]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*argv, "--check"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["result"]["renamed"], 2)
        self.assertIn("func_80001000_de", self.manifest.read_text())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(argv), 0)
        self.assertIn("Math_Sqrtf", self.manifest.read_text())


if __name__ == "__main__":
    unittest.main()
