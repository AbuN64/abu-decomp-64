"""A plain tree (the port): its own land settings, steps and pin gate, and the repin verb. Real git, mocked builds."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any
from unittest import mock

from abu_decomp import land, repin, tas
from abu_decomp.config import load_profile
from abu_decomp.land import Landing, run_process
from abu_decomp.ship import ship

SHA_A, SHA_B = "a" * 40, "b" * 40


def git(root: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t"}
    env["GIT_COMMITTER_EMAIL"] = "t@t"
    return subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, text=True, check=True).stdout


class PortTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        git(self.source, "init", "-q", "-b", "main")
        (self.source / "s.c").write_text("int s;\n")
        git(self.source, "add", ".")
        git(self.source, "commit", "-q", "-m", "s1")
        self.pin1 = git(self.source, "rev-parse", "HEAD").strip()
        (self.source / "s.c").write_text("int s2;\n")
        git(self.source, "commit", "-qam", "s2")
        self.pin2 = git(self.source, "rev-parse", "HEAD").strip()
        self.remote, self.tree = self.root / "remote.git", self.root / "port"
        git(self.root, "init", "-q", "--bare", "-b", "main", str(self.remote))
        git(self.root, "clone", "-q", str(self.remote), str(self.tree))
        (self.tree / ".git/info").mkdir(exist_ok=True)  # a host's init.templatedir may lack info/
        (self.tree / ".git/info/exclude").write_text(".unbake/\n")  # real trees ignore it (ship locks there)
        (self.tree / "decomp.cmake").write_text(f"GIT_TAG {self.pin1}\nset(x)\n")
        (self.tree / "README.md").write_text(f"fetches at commit {self.pin1}.\n")
        git(self.tree, "add", ".")
        git(self.tree, "commit", "-q", "-m", "base")
        git(self.tree, "push", "-q", "origin", "HEAD:main")
        git(self.source, "checkout", "-q", "--detach", self.pin1)  # the checkout the build fetched is at the pin
        (self.root / "events.jsonl").write_text("")
        (self.root / "host.toml").write_text("")
        sections = {
            "tools": {"project": "port", "host": "host.toml", "timeout_seconds": 5},
            "events": {"path": "events.jsonl"},
            "land": {
                "check": ["make", "check"],
                "author": "mo <mo@example.org>",
                "evidence": {"roms": []},
                "trees": {
                    "port": {
                        "mode": "plain",
                        "steps": [
                            {"name": "build", "argv": ["cmake", "--build", "b"]},
                            {"name": "tas", "argv": ["{decomp}", "tas"]},
                        ],
                        "pin": {
                            "source": "source",
                            "ref": "main",
                            "fetched": "source",
                            "files": [
                                {"path": "decomp.cmake", "pattern": "GIT_TAG ([0-9a-f]{40})"},
                                {"path": "README.md", "pattern": "at commit ([0-9a-f]{40})"},
                            ],
                        },
                    }
                },
            },
        }
        (self.root / "seat.json").write_text(json.dumps({"schema_version": 1, "name": "seat", **sections}))
        self.profile = load_profile(self.root / "seat.json")
        self.commands: list[list[str]] = []
        self.exits: dict[str, int] = {}

    def runner(self, argv: Any, cwd: Path, env: Any, timeout: Any) -> Any:
        if argv[0] == "git":
            return run_process(argv, cwd, env, timeout)
        self.commands.append(list(argv))
        return self.exits.get(argv[0], 0), "built\n", ""

    def test_a_tree_with_an_entry_has_its_own_settings_and_others_keep_the_profile_table(self) -> None:
        mine = land.settings(self.profile, self.tree)
        self.assertEqual(
            (mine["mode"], mine["evidence"], mine["check"], mine["author"]),
            ("plain", None, None, "mo <mo@example.org>"),
        )
        self.assertEqual([s["name"] for s in mine["steps"]], ["build", "tas"])
        other = land.settings(self.profile, self.root)
        self.assertEqual((other["mode"], other["check"], other["steps"]), ("unbake", ["make", "check"], []))
        self.assertIsNotNone(other["evidence"])

    def test_a_pin_pattern_with_no_group_is_a_profile_error(self) -> None:
        bad = json.loads((self.root / "seat.json").read_text())
        bad["land"]["trees"]["port"]["pin"]["files"][0]["pattern"] = "GIT_TAG [0-9a-f]{40}"
        (self.root / "seat.json").write_text(json.dumps(bad))
        with self.assertRaises(land.LandError):
            land.settings(load_profile(self.root / "seat.json"), self.tree)

    def patch(self, edits: dict[str, str]) -> tuple[str, Path]:
        for name, text in edits.items():
            (self.tree / name).write_text(text)
        text = git(self.tree, "diff", "HEAD")
        git(self.tree, "checkout", "-q", "HEAD", "--", ".")
        path = self.root / "p.patch"
        path.write_text(text)
        return "P", path

    def land(self, patch: tuple[str, Path], message: str = "Define the flag") -> Any:
        landing = Landing(
            self.profile, self.tree, git(self.tree, "rev-parse", "HEAD").strip(), [patch],
            run=self.runner, build=self.runner, append=lambda p, r: {"ok": True}, messages={"P": message},
        )  # fmt: skip
        return landing.execute()

    def test_a_plain_land_runs_only_the_steps_and_the_pin_gate(self) -> None:
        patch = self.patch({"decomp.cmake": f"GIT_TAG {self.pin1}\nset(x -DY)\n"})
        result, code, findings = self.land(patch)
        self.assertEqual(code, 0, findings)
        gates = {row["gate"]: row.get("pass") for row in result["gates"]}
        self.assertEqual((gates["check"], gates["pin"], gates["apply"]), (True, True, True))
        self.assertNotIn("setup", {g["gate"] for g in result["gates"]})
        self.assertEqual([c[0] for c in self.commands], ["cmake", os.sys.executable])
        self.assertEqual(self.commands[1][-3:], ["--profile", str(self.root / "seat.json"), "tas"][-3:])
        steps = next(g for g in result["gates"] if g["gate"] == "check")["steps"]
        self.assertEqual([s["step"] for s in steps], ["build", "tas"])
        behind = [f for f in findings if f["key"] == "pin.behind"]
        self.assertTrue(behind and " repin --tree " in behind[0]["next"])

    def test_a_failing_step_refuses_the_input_and_stops_the_later_steps(self) -> None:
        self.exits["cmake"] = 2
        _, code, findings = self.land(self.patch({"decomp.cmake": f"GIT_TAG {self.pin1}\nset(y)\n"}))
        self.assertEqual(code, 1)
        self.assertEqual([c[0] for c in self.commands], ["cmake"])
        self.assertEqual(findings[0]["key"], "land.check")

    def test_a_tas_step_that_exits_non_zero_refuses(self) -> None:
        self.exits[os.sys.executable] = 1
        _, code, findings = self.land(self.patch({"decomp.cmake": f"GIT_TAG {self.pin1}\nset(y)\n"}))
        self.assertEqual((code, findings[0]["key"]), (1, "land.check"))
        self.assertIn("tas", findings[0]["message"])

    def test_places_that_disagree_are_refused_by_the_pin_gate(self) -> None:
        patch = self.patch({"README.md": f"fetches at commit {self.pin2}.\n"})
        _, code, findings = self.land(patch)
        self.assertEqual(code, 1)
        self.assertEqual(findings[0]["key"], "pin.differ")
        self.assertIn(" repin --tree ", findings[0]["next"])

    def test_a_pin_behind_the_source_is_a_note_naming_the_repin_verb(self) -> None:
        pin = land.settings(self.profile, self.tree)["pin"]
        found = repin.check(pin, self.tree, run_process, self.profile.source_file)
        row = next(f for f in found["findings"] if f["key"] == "pin.behind")
        self.assertFalse(row["blocking"])
        self.assertTrue(row["next"].startswith("decomp --profile"))

    def test_repin_sets_every_place_and_leaves_the_change_in_the_work_tree(self) -> None:
        result, code, findings = repin.repin(self.profile, self.tree, None)
        self.assertEqual((code, findings), (0, []))
        self.assertEqual(sorted(result["changed"]), ["README.md", "decomp.cmake"])
        self.assertEqual(result["to"], self.pin2)
        self.assertIn(self.pin2, (self.tree / "decomp.cmake").read_text())
        self.assertIn(self.pin2, (self.tree / "README.md").read_text())
        self.assertIn("ship --tree", result["next"])
        again, _, _ = repin.repin(self.profile, self.tree, self.pin2)
        self.assertEqual(again["changed"], [])

    def page_profile(self) -> Any:
        """The seat profile with a separate fetched clone (at pin1) and a tas section with a rebuild command."""
        fetched = self.root / "fetch"
        git(self.root, "clone", "-q", str(self.source), str(fetched))
        git(fetched, "checkout", "-q", "--detach", self.pin1)
        data = json.loads((self.root / "seat.json").read_text())
        data["land"]["trees"]["port"]["pin"]["fetched"] = "fetch"
        (self.root / "runs" / "plays").mkdir(parents=True, exist_ok=True)
        data["tas"] = {
            "movies": "movies", "runs": "runs", "port": "ragewars", "args": [], "rom": "rom.z64",
            "build": {"rebuild": "cmake --build page --target ragewars"},
        }  # fmt: skip
        (self.root / "page.json").write_text(json.dumps(data))
        return load_profile(self.root / "page.json"), fetched

    def test_repin_checks_out_the_fetched_clone_and_rebuilds_the_page_port(self) -> None:
        profile, fetched = self.page_profile()
        result, code, findings = repin.repin(profile, self.tree, None, self.runner)
        self.assertEqual((code, findings), (0, []))
        self.assertEqual(git(fetched, "rev-parse", "HEAD").strip(), self.pin2)
        self.assertEqual(result["fetched"], str(fetched))
        self.assertEqual(self.commands, [["nice", "-n", "10", "cmake", "--build", "page", "--target", "ragewars"]])
        self.assertEqual(result["page_build"]["exit"], 0)

    def test_repin_rebuilds_the_page_port_while_a_replay_is_live(self) -> None:
        profile, fetched = self.page_profile()
        live = self.root / "runs" / "plays" / "r1"
        live.mkdir()
        (live / "run.json").write_text(json.dumps({"id": "r1", "status": "running", "pid": os.getpid()}))
        with mock.patch.object(tas, "read_record", return_value={"id": "r1", "status": "running", "pid": os.getpid()}):
            result, code, _ = repin.repin(profile, self.tree, None, self.runner)
        self.assertEqual(code, 0)  # the rebuild publishes by an atomic swap: a live replay is never in its way
        self.assertEqual(result["page_build"]["exit"], 0)
        self.assertEqual(self.commands, [["nice", "-n", "10", "cmake", "--build", "page", "--target", "ragewars"]])
        self.assertEqual(git(fetched, "rev-parse", "HEAD").strip(), self.pin2)

    def test_repin_reports_a_failed_page_rebuild_and_no_rebuild_skips_it(self) -> None:
        profile, _ = self.page_profile()
        self.exits["nice"] = 2
        _, code, findings = repin.repin(profile, self.tree, None, self.runner)
        self.assertEqual((code, findings[0]["key"]), (1, "repin.build"))
        self.commands.clear()
        repin.repin(profile, self.tree, self.pin1, self.runner, rebuild=False)
        self.assertEqual(self.commands, [])

    def test_repin_to_a_commit_the_source_lacks_is_refused_and_writes_nothing(self) -> None:
        with self.assertRaises(repin.RepinError):
            repin.repin(self.profile, self.tree, SHA_B)
        self.assertIn(self.pin1, (self.tree / "decomp.cmake").read_text())

    def test_a_port_ship_pushes_the_trees_change_to_its_own_remote(self) -> None:
        (self.tree / "decomp.cmake").write_text(f"GIT_TAG {self.pin1}\nset(x -DZ)\n")

        def landing(*a: Any, **k: Any) -> Any:
            return Landing(
                a[0],
                a[1],
                a[2],
                a[3],
                run=self.runner,
                build=self.runner,
                append=lambda p, r: {"ok": True},
                messages=k.get("messages"),
            ).execute()

        result, code, findings = ship(
            self.profile, self.tree, [], worktree="flag", messages={"flag": "Define the flag"},
            landing=landing, append=lambda p, r: {"ok": True},
        )  # fmt: skip
        self.assertEqual(code, 0, findings)
        self.assertEqual(git(self.remote, "rev-parse", "main").strip(), result["pushed"])
        self.assertEqual(git(self.tree, "status", "--porcelain", "--untracked-files=no"), "")


class PlayAllStreamTests(unittest.TestCase):
    def test_each_movie_row_reaches_stderr_as_it_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "m").mkdir()
            for name in ("a", "b"):
                (root / "m" / f"{name}.tas").write_text("# tas 1\n")
            cfg = {"movies": root / "m", "runs": root / "r", "build": {}, "rank": tas.tas_rank.settings(None)}
            rows = {n: {"movie": n, "outcome": "ok", "polls": 9, "polls_reached": 9, "wall_seconds": 1.5} for n in "ab"}
            err = io.StringIO()
            with (
                mock.patch.object(tas, "settings", return_value=cfg),
                mock.patch.object(tas, "play", side_effect=lambda p, n, **k: rows[n]),
                mock.patch("abu_decomp.tas_coverage.build", return_value=None),  # coverage has its own tests
                redirect_stderr(err),
            ):
                out, code = tas.play_all(None)
            self.assertEqual(code, 0)
            lines = [line for line in err.getvalue().splitlines() if line.startswith("tas: ")]
            self.assertEqual(sorted(line.split()[1] for line in lines), ["a", "b"])
            self.assertIn("ok 9/9", lines[0])
            self.assertEqual([r["movie"] for r in out["movies"]], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
