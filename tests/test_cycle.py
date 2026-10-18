import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from abu_decomp import DecompError
from abu_decomp.config import load_profile
from abu_decomp.cycle import Cycle, delta_message


def report(code_matched):
    row = {"code_matched": code_matched, "code_total": 1000, "data_matched": 50, "functions_matched": 7}
    return {"ok": True, "result": {"versions": {"us": row}}}


class Tools:
    """Counts each tool call; the report numbers come from a list, one per call."""

    def __init__(self, numbers, recipe_ok=True):
        self.numbers, self.recipe_ok = list(numbers), recipe_ok
        self.calls = []

    def __call__(self, tool, profile, args):
        self.calls.append((tool, tuple(args)))
        if tool == "recipe":
            return SimpleNamespace(ok=self.recipe_ok, returncode=0 if self.recipe_ok else 2, document={"ok": True})
        return SimpleNamespace(ok=True, returncode=0, document=report(self.numbers.pop(0)))

    def count(self, tool):
        return sum(1 for name, _ in self.calls if name == tool)


class FakeRefs:
    pull_seconds = None

    def __init__(self, profile, config):
        self.config = config
        self.pulls = 0
        if config.get("pull_seconds"):
            self.pull_seconds = config["pull_seconds"]
        self.read, self.write = os.pipe()
        os.set_blocking(self.read, False)  # like the inotify fds RefSource returns
        self.pending = 0

    def watch_fds(self):
        return [self.read]

    def scan(self):
        landed, self.pending = self.pending, 0
        return landed

    def land(self):
        self.pending = 1
        os.write(self.write, b"x")

    def pull(self):
        self.pulls += 1
        if self.pulls == 1:
            self.land()  # the first pull fetched one new commit; later pulls find nothing


class Builds:
    def __init__(self, ok=True):
        self.ok, self.argvs = ok, []

    def __call__(self, argv, **_kw):
        self.argvs.append(argv)
        if self.ok:
            return SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")
        return SimpleNamespace(returncode=1, stdout='{"ok": false, "findings": [{"message": "gate boot"}]}', stderr="")


class CycleBase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.project = self.root / "project"
        for name in ("src/game", "include", "build/src", "docs"):
            (self.project / name).mkdir(parents=True)
        (self.root / "host.toml").write_text("")
        self.events = []

    def cycle(self, numbers, *, recipe_ok=True, refs=False, builds=None, **extra):
        table = {
            "sources": ["src", "include"],
            "suffixes": [".c", ".h", ".CPP"],
            "exclude": ["build"],
            "settle_seconds": 0.05,
            "recipe": ["check"],
            **extra,
        }
        if refs:
            table["refs"] = {"repos": [], "state_path": "seen.json", **(refs if isinstance(refs, dict) else {})}
        table = {key: value for key, value in table.items() if value is not None}
        document = {
            "schema_version": 1,
            "name": "t",
            "tools": {"project": str(self.project), "host": str(self.root / "host.toml"), "timeout_seconds": 5},
            "events": {"path": str(self.root / "events.jsonl")},
            "cycle": table,
        }
        path = self.root / "seat.json"
        path.write_text(json.dumps(document))
        self.tools = Tools(numbers, recipe_ok)
        cycle = Cycle(
            load_profile(path),
            invoke=self.tools,
            append=lambda _path, record: self.events.append(record),
            ref_source=FakeRefs,
            run_command=builds or Builds(),
        )
        self.addCleanup(cycle.close)
        return cycle


class CycleTests(CycleBase):
    def test_one_touched_source_file_runs_recipe_and_report_exactly_once(self):
        cycle = self.cycle([100, 100])
        (self.project / "src/game/a.c").write_text("int a;\n")
        runs = cycle.serve(max_runs=1)
        self.assertEqual([run["trigger"] for run in runs], [[str(self.project / "src/game/a.c")]])
        self.assertEqual((self.tools.count("recipe"), self.tools.count("unbake")), (1, 2))  # baseline + one run
        self.assertEqual(self.tools.calls[1], ("recipe", ("check",)))
        self.assertEqual(cycle.relevant(cycle.watcher.read()), [])

    def test_unrelated_files_trigger_nothing(self):
        cycle = self.cycle([])
        (self.project / "src/game/notes.txt").write_text("x")
        (self.project / "src/game/a.c.swp").write_text("x")
        (self.project / "build/src/a.c").write_text("x")
        (self.project / "docs/b.c").write_text("x")
        (self.project / "progress.json").write_text("{}")
        self.assertEqual(cycle.relevant(cycle.watcher.read()), [])
        self.assertEqual(self.tools.calls, [])

    def test_a_burst_of_changes_is_one_run(self):
        cycle = self.cycle([100, 100])
        for name in ("a.c", "b.c", "c.CPP"):
            (self.project / "src/game" / name).write_text("1")
        (self.project / "src/game/a.c").write_text("2")
        (self.project / "include/x.h").write_text("3")
        runs = cycle.serve(max_runs=1)
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(runs[0]["trigger"]), 4)
        self.assertEqual(self.tools.count("recipe"), 1)
        self.assertEqual(cycle.relevant(cycle.watcher.read()), [])

    def test_a_new_directory_is_watched_and_its_sources_count(self):
        cycle = self.cycle([100, 100, 100])
        (self.project / "src/new/deep").mkdir(parents=True)
        (self.project / "src/new/deep/b.c").write_text("1")
        first = cycle.serve(max_runs=1)[0]
        self.assertIn(str(self.project / "src/new/deep/b.c"), first["trigger"])
        (self.project / "src/new/deep/b.c").write_text("2")
        second = cycle.serve(max_runs=2, baseline=False)[1]
        self.assertEqual(second["trigger"], [str(self.project / "src/new/deep/b.c")])

    def test_match_carries_the_before_and_after_report_numbers_only_when_they_change(self):
        cycle = self.cycle([100, 120, 120])
        (self.project / "src/game/a.c").write_text("1")
        cycle.serve(max_runs=1)
        (self.project / "src/game/a.c").write_text("2")
        cycle.serve(max_runs=2, baseline=False)
        matches = [event for event in self.events if event["kind"] == "MATCH"]
        self.assertEqual(len(matches), 1)
        payload = matches[0]["payload"]
        self.assertEqual(payload["before"]["us"]["code_matched"], 100)
        self.assertEqual(payload["after"]["us"]["code_matched"], 120)
        self.assertEqual(matches[0]["message"], "progress us code 100->120 (+20)")

    def test_failed_recipe_is_a_run_fail_and_the_report_still_runs(self):
        cycle = self.cycle([100, 100], recipe_ok=False)
        (self.project / "src/game/a.c").write_text("1")
        run = cycle.serve(max_runs=1)[0]
        self.assertFalse(run["recipe"])
        self.assertEqual([event["kind"] for event in self.events], ["RUN-FAIL"])
        self.assertEqual(self.tools.count("unbake"), 2)

    def test_a_landed_ref_triggers_one_run(self):
        cycle = self.cycle([100, 100], refs=True)
        cycle.refs.land()
        run = cycle.serve(max_runs=1)[0]
        self.assertEqual(run["trigger"], ["landed refs"])
        self.assertEqual(self.tools.count("recipe"), 1)

    def test_report_only_without_a_recipe(self):
        cycle = self.cycle([100, 100], recipe=None)
        (self.project / "include/x.h").write_text("1")
        cycle.serve(max_runs=1)
        self.assertEqual((self.tools.count("recipe"), self.tools.count("unbake")), (0, 2))

    def test_unknown_keys_and_missing_lists_are_refused(self):
        with self.assertRaises(DecompError):
            self.cycle([], extra_key=1)
        with self.assertRaises(DecompError):
            self.cycle([], sources=[])

    def test_delta_message_names_every_changed_counter(self):
        before = {"us": {"code_matched": 1, "data_matched": 5}, "eu": {"code_matched": 3}}
        after = {"us": {"code_matched": 2, "data_matched": 5}, "eu": {"code_matched": 3}}
        self.assertEqual(delta_message(before, after), "progress us code 1->2 (+1)")
        self.assertIsNone(delta_message(after, after))


class BuildLoopTests(CycleBase):
    """The live build loop 1:1: build once at start, then once per landed ref, no source roots, no report."""

    def loop(self, builds, **extra):
        refs = extra.pop("refs", True)
        return self.cycle([], refs=refs, builds=builds, sources=None, suffixes=None, recipe=None, report=False,
                          build=["once"], **extra)  # fmt: skip

    def test_build_runs_at_start_and_once_per_landed_ref(self):
        builds = Builds()
        cycle = self.loop(builds, run_at_start=True)
        self.assertEqual(cycle.watcher.directories, {})
        cycle.refs.land()
        runs = cycle.serve(max_runs=2)
        self.assertEqual([run["trigger"] for run in runs], [["start"], ["landed refs"]])
        self.assertEqual(len(builds.argvs), 2)
        self.assertEqual(builds.argvs[0][-4:], ["--profile", str(self.root / "seat.json"), "build", "once"])
        self.assertEqual(self.tools.calls, [])

    def test_a_failed_build_is_one_run_fail_with_the_finding(self):
        cycle = self.loop(Builds(ok=False))
        cycle.refs.land()
        run = cycle.serve(max_runs=1)[0]
        self.assertFalse(run["build"])
        self.assertEqual(
            [(e["kind"], e["message"]) for e in self.events], [("RUN-FAIL", "cycle build once failed: gate boot")]
        )

    def test_land_refs_append_land_but_trigger_nothing(self):
        cycle = self.loop(Builds(), land_refs={"repos": [], "state_path": "land.json"})
        cycle.land_refs.land()
        cycle.refs.land()
        runs = cycle.serve(max_runs=1)
        self.assertEqual(runs[0]["trigger"], ["landed refs"])
        cycle.land_refs.land()
        self.assertEqual(cycle.land_refs.scan(), 1)  # LAND appended by the source; no burst was opened for it

    def test_the_pull_timer_moves_refs_and_the_landing_triggers_one_build(self):
        builds = Builds()
        cycle = self.loop(builds, refs={"pull_seconds": 0.01})
        run = cycle.serve(max_runs=1)[0]
        self.assertGreaterEqual(cycle.refs.pulls, 1)
        self.assertEqual((run["trigger"], len(builds.argvs)), (["landed refs"], 1))
