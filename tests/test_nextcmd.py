"""Every `next` a producer prints is a command that runs as printed (nextcmd.problems(...) == [])."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import test_port_tree

from abu_decomp import land, nextcmd, repin, tas_health, tas_rank, tas_triage
from abu_decomp.land import Landing, run_process
from abu_decomp.ship import ship

LONG = "Repin the decomposition source to the commit that moved the boot bindings and the overlay table"


class HelperTests(unittest.TestCase):
    def test_subject_cuts_on_a_word_within_the_limit(self) -> None:
        got = nextcmd.subject(LONG, 72)
        self.assertLessEqual(len(got), 72)
        self.assertTrue(LONG.startswith(got) and LONG[len(got)] == " ")
        self.assertEqual(nextcmd.subject("short", 72), "short")

    def test_problems_finds_each_kind_of_wrong_next(self) -> None:
        cases = {
            "edit the file, then run: edit it by hand": "not a command",
            "decomp ship --tree /t --patch LANE=PATH:SHORTER_MESSAGE": "placeholder",
            "decomp ship --tree /t --worktree w:<what moved>": "placeholder",
            "decomp nonsense --x": "decomp parser",
            f"decomp ship --tree /t --worktree w:{'x' * 80}": "commit subject is 80",
            "decomp ship --tree /t --patch L=/p && git push": "chain",
            "": "empty",
        }
        for command, want in cases.items():
            self.assertTrue(any(want in p for p in nextcmd.problems(command)), (command, nextcmd.problems(command)))
        self.assertEqual(nextcmd.problems("decomp --profile p ship --tree /t --worktree w:Fix_a_bug"), [])

    def test_worktree_form_replaces_patch_form(self) -> None:
        got = nextcmd.as_worktree("decomp --profile p ship --tree /t --patch L=/p/x.patch:Shorter", "wt")
        self.assertEqual(got, "decomp --profile p ship --tree /t --worktree wt:Shorter")
        self.assertEqual(nextcmd.problems(got), [])
        self.assertEqual(nextcmd.as_worktree("git status", "wt"), "git status")

    def test_read_site_commands_run(self) -> None:
        self.assertEqual(nextcmd.problems(nextcmd.read_site("src/MENU/a.CPP:5")), [])
        self.assertEqual(nextcmd.problems(nextcmd.read_site("src/MENU/a.CPP")), [])


class ProducerTests(test_port_tree.PortTreeTests):
    """The port tree fixture (real git, mocked builds) drives the producers."""

    def check(self, command: str | None) -> None:
        self.assertTrue(command)
        self.assertEqual(nextcmd.problems(str(command)), [], command)

    def test_every_land_finding_key_ends_in_a_valid_command(self) -> None:
        inputs = {"L": (self.root / "x.patch", "M" * 90)}
        for key in land.NEXT_COMMANDS:
            finding = {"key": key, "lane": "L", "file": "a.c"}
            self.check(land.next_command(self.profile, self.tree, "HEAD", finding, inputs))
        self.check(land.next_command(self.profile, self.tree, "HEAD", {"key": "land.unknown"}, inputs))
        got = land.next_command(self.profile, self.tree, "HEAD", {"key": "land.commit_subject", "lane": "L"}, inputs)
        self.assertIn("--patch", got)

    def test_repin_next_is_a_concrete_ship_with_a_subject_inside_the_limit(self) -> None:
        test_port_tree.git(self.source, "commit", "-q", "--allow-empty", "-m", LONG)
        result, _, _ = repin.repin(self.profile, self.tree, None)
        self.check(result["next"])
        self.assertIn("--worktree", result["next"])
        self.assertNotIn("<", result["next"])

    def test_every_pin_finding_next_is_valid(self) -> None:
        pin = land.settings(self.profile, self.tree)["pin"]
        (self.tree / "README.md").write_text(f"fetches at commit {self.pin2}.\n")
        for row in repin.check(pin, self.tree, run_process, self.profile.source_file)["findings"]:
            if row.get("next"):
                self.check(row["next"])
        (self.tree / "README.md").write_text("nothing\n")
        found = repin.check(pin, self.tree, run_process, self.profile.source_file)["findings"]
        self.check(next(f for f in found if f["key"] == "pin.missing")["next"])

    def test_a_stale_binary_behind_a_lagging_pin_says_repin_first(self) -> None:
        listing, binary, src = self.root / "sources.txt", self.tree / "bin", self.root / "a.c"
        src.write_text("x")
        listing.write_text(f"{src}\n")
        binary.write_text("old")
        os.utime(binary, (1, 1))
        rebuild = f"cmake --build {self.tree}/build --target ragewars"
        cfg = {"port": binary, "build": {"sources": listing, "rebuild": rebuild}}
        health = tas_health.read(cfg, self.profile)
        self.assertTrue(health["stale"])
        self.check(health["rebuild"])
        self.assertIn(" repin --tree ", health["rebuild"])
        self.assertEqual(tas_health.header(health)["then"], rebuild)
        self.assertIn(" repin ", tas_rank.stale_rows(health)[0]["next"])
        repin.repin(self.profile, self.tree, None)  # the pin now matches the source: the build command is the next
        self.assertEqual(tas_health.read(cfg, self.profile)["rebuild"], rebuild)
        self.check(rebuild)

    def test_a_worktree_refusal_names_the_worktree_form_with_a_short_subject(self) -> None:
        (self.tree / "decomp.cmake").write_text(f"GIT_TAG {self.pin1}\nset(x -DZ)\n")

        def landing(*a: Any, **k: Any) -> Any:
            return Landing(
                a[0], a[1], a[2], a[3], run=self.runner, build=self.runner,
                append=lambda p, r: {"ok": True}, messages=k.get("messages"),
            ).execute()  # fmt: skip

        _, code, findings = ship(
            self.profile, self.tree, [], worktree="flag", messages={"flag": LONG * 2},
            landing=landing, append=lambda p, r: {"ok": True},
        )  # fmt: skip
        self.assertEqual(code, 1)
        refusal = next(f for f in findings if f.get("blocking"))
        self.assertEqual(refusal["key"], "land.commit_subject")
        self.check(refusal["next"])
        self.assertIn("--worktree", refusal["next"])
        self.assertNotIn("--patch", refusal["next"])


class TriageTests(unittest.TestCase):
    def test_tas_triage_nexts_are_valid(self) -> None:
        profile = SimpleNamespace(source_file=Path("/p/w.json"))
        with (
            mock.patch.object(tas_triage, "_project", return_value=None),
            mock.patch("abu_decomp.arity.notes", return_value=[]),
        ):
            for subject in ("host-fault-0000beef", "func_80411EEC_de", "code-pointer-8011fe88", "what"):
                got = tas_triage.claim(profile, {"outcome": "stop", "subject": subject})
                self.assertEqual(nextcmd.problems(got["next"]), [], (subject, got["next"]))
            got = tas_triage.claim(profile, {"outcome": "hang", "subject": ""})
            self.assertEqual(nextcmd.problems(got["next"]), [])


if __name__ == "__main__":
    unittest.main()
