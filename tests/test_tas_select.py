import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from abu_decomp import tas, tas_rank, tas_select

SELECT = {"game": "rw", "others": ["shim"], "changed": "git diff --name-only {old} {new}"}


def report(run: Path, files: dict[str, int]) -> Path:
    run.mkdir(parents=True)
    (run / "coverage.json").write_text(
        json.dumps(
            {"files": [{"file": f, "functions": [{"name": "f", "execution_count": n}]} for f, n in files.items()]}
        )
    )
    return run


class SelectTests(unittest.TestCase):
    def test_changed_files_is_none_when_anything_is_unknown(self):
        old, new = {"rw": "a", "shim": "s"}, {"rw": "b", "shim": "s"}
        self.assertIsNone(tas_select.changed_files(SELECT, None, new))
        self.assertIsNone(tas_select.changed_files(SELECT, old, {"rw": "b", "shim": "t"}))
        done = SimpleNamespace(returncode=1, stdout="")
        with mock.patch("subprocess.run", return_value=done):
            self.assertIsNone(tas_select.changed_files(SELECT, old, new))
        self.assertEqual(tas_select.changed_files(SELECT, old, old), set())

    def test_changed_files_lists_the_command_output(self):
        done = SimpleNamespace(returncode=0, stdout="src/a.c\n\nsrc/b.c\n")
        with mock.patch("subprocess.run", return_value=done) as run:
            got = tas_select.changed_files(SELECT, {"rw": "a", "shim": "s"}, {"rw": "b", "shim": "s"})
        self.assertEqual(got, {"src/a.c", "src/b.c"})
        self.assertEqual(run.call_args[0][0], ["git", "diff", "--name-only", "a", "b"])

    def test_affected_only_by_entered_sources(self):
        files = {"tree/src/a.c": True, "tree/src/b.c": False}
        self.assertTrue(tas_select.affected(files, {"src/a.c"}))
        self.assertFalse(tas_select.affected(files, {"src/b.c"}))
        self.assertTrue(tas_select.affected(files, {"include/x.h"}))  # not a compiled source: everything
        self.assertTrue(tas_select.affected(files, {"src/new.c"}))  # in no report: unknown
        self.assertTrue(tas_select.affected(None, {"src/b.c"}))

    def test_plan_replays_unknown_failed_and_touched_movies(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pins = {"rw": "a", "shim": "s"}
            good = {"outcome": "ok", "pins": pins}
            previous = {
                "touched": (report(root / "t", {"x/src/a.c": 1}), good),
                "clear": (report(root / "c", {"x/src/a.c": 0, "x/src/z.c": 3}), good),
                "failed": (report(root / "f", {"x/src/a.c": 0}), {"outcome": "crash", "pins": pins}),
                "unpinned": (report(root / "u", {"x/src/a.c": 0}), {"outcome": "ok"}),
            }
            names = ["touched", "clear", "failed", "unpinned", "never"]
            profile = SimpleNamespace(section=lambda name: {"select": SELECT}, source_file=Path("/p.json"))
            done = SimpleNamespace(returncode=0, stdout="src/a.c\n")
            with (
                mock.patch.object(tas_select, "read_pins", return_value={"rw": "b", "shim": "s"}),
                mock.patch("subprocess.run", return_value=done),
            ):
                replay, skipped, why = tas_select.plan(profile, {}, names, previous)
        self.assertEqual((replay, sorted(skipped)), (["touched", "failed", "unpinned", "never"], ["clear"]))
        self.assertIn("1 of 5", why)


class PlayAllFastTests(unittest.TestCase):
    def cfg(self, root: Path, names=("a", "b")):
        (root / "m").mkdir()
        for name in names:
            (root / "m" / f"{name}.tas").write_text("# tas 1\n")
        return {"movies": root / "m", "runs": root / "r", "build": {}, "rank": tas_rank.settings(None)}

    def test_first_pass_without_the_bridge_and_failures_replayed_under_it(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cfg = self.cfg(root)
            seen = []

            def play(profile, name, **kwargs):
                seen.append((name, kwargs.get("supervise", True)))
                if name == "b" and kwargs.get("supervise") is False:
                    folder = root / "gone"
                    folder.mkdir()
                    raise tas.NeedsSupervisor(folder)
                return {"movie": name, "outcome": "ok" if name == "a" else "crash", "polls": 9, "polls_reached": 9}

            with (
                mock.patch.object(tas, "settings", return_value=cfg),
                mock.patch.object(tas, "play", side_effect=play),
                mock.patch("abu_decomp.tas_coverage.build", return_value=None),
                mock.patch("abu_decomp.slots.capacity", return_value=3),
            ):
                out, _ = tas.play_all(None, jobs=1)
                self.assertEqual(sorted(seen), [("a", False), ("b", False), ("b", True)])
                self.assertFalse((root / "gone").exists())
                self.assertEqual([r["outcome"] for r in out["movies"]], ["ok", "crash"])
                seen.clear()
                tas.play_all(None, jobs=1, gdb_all=True)
                self.assertEqual(sorted(seen), [("a", True), ("b", True)])

    def test_changed_skips_unaffected_movies_with_their_standing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cfg = self.cfg(root)
            last = {"outcome": "ok", "polls_reached": 7, "polls": 9, "movie": "a"}
            plan = (["b"], {"a": (root / "run-a", last)}, "1 of 2 movies unaffected by the changed sources")
            played = []
            with (
                mock.patch.object(tas, "settings", return_value=cfg),
                mock.patch.object(
                    tas,
                    "play",
                    side_effect=lambda p, n, **k: (
                        played.append(n) or {"movie": n, "outcome": "ok", "polls": 9, "polls_reached": 9}
                    ),
                ),
                mock.patch.object(tas, "run_rows", return_value=[]),
                mock.patch("abu_decomp.tas_select.plan", return_value=plan),
                mock.patch("abu_decomp.tas_coverage.build", return_value=None),
            ):
                out, code = tas.play_all(None, jobs=1, changed=True)
        self.assertEqual((played, code), (["b"], 0))
        self.assertEqual([(r["movie"], r["skipped"]) for r in out["movies"]], [("a", "run-a"), ("b", None)])
        self.assertEqual(out["selected"]["skipped"], 1)
        self.assertIn("selective replay", tas.table(out))


if __name__ == "__main__":
    unittest.main()
