import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp import tas_coverage as cov

READELF = """Symbol table '.symtab' contains 4 entries:
   Num:    Value  Size Type    Bind   Vis      Ndx Name
     1: 80200400    32 FUNC    GLOBAL DEFAULT    1 func_80200400_us_rev1
     2: 80200500   100 FUNC    GLOBAL DEFAULT    1 func_80200500_de
     3: 80200600     0 FUNC    GLOBAL DEFAULT    1 empty
     4: 80200700    12 OBJECT  GLOBAL DEFAULT    2 data
"""


def row(name, size, versions=("us-rev1",), scope="game", file="src/game/A.CPP"):
    return {"name": name, "size": size, "versions": list(versions), "scope": scope, "file": file, "address": 0}


class ReadTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_gcovr_json_counts_add_up_for_same_named_statics(self):
        (self.root / "c.json").write_text(
            json.dumps(
                {
                    "files": [
                        {
                            "file": "a.c",
                            "functions": [{"name": "f", "execution_count": 2}, {"name": "g", "execution_count": 0}],
                        },
                        {"file": "b.c", "functions": [{"name": "f", "execution_count": 3}]},
                    ]
                }
            )
        )
        self.assertEqual(cov.read_gcovr(self.root / "c.json"), {"f": 5, "g": 0})
        self.assertIsNone(cov.read_gcovr(self.root / "missing.json"))

    def test_oracle_trace_maps_vi_to_the_poll_count(self):
        golden = self.root / cov.ORACLE_DIR
        golden.mkdir()
        (golden / "trace-final.tsv").write_text(
            "order\tsymbol\tfirst_vi\tfirst_thread\tcount\n1\tfunc_a\t10\t3\t7\n2\tfunc_b\t31\t3\t1\nbad\n"
        )
        (self.root / cov.tas_oracle.ORACLE_TRACE).write_text("1 5\n2 12\n3 30\n4 40\n")
        hits, polls = cov.read_oracle(self.root)
        self.assertEqual(hits, {"func_a": (1, 7), "func_b": (3, 1)})
        self.assertEqual(polls, 4)
        self.assertIsNone(cov.read_oracle(self.root / "none"))

    def test_oracle_run_is_the_tas_oracle_host_with_the_golden_dir(self):
        host = self.root / "n64host"
        host.write_text("")
        (self.root / "movie.raw").write_text("0000 0 0\n")
        profile = mock.Mock()
        profile.section.return_value = {"oracle": {"host": "n64host"}}
        profile.path.side_effect = lambda _name, value: self.root / value
        profile.source_file = "p"
        process = mock.Mock()

        def leave_trace(*_args, **_kw):
            (self.root / cov.ORACLE_DIR / "trace-final.tsv").write_text("order\n")
            return process

        with mock.patch.object(cov.tas_oracle, "start", side_effect=leave_trace) as start:
            self.assertTrue(cov.run_oracle(profile, {"exit_after": 7}, self.root, {"pak": "off"}))
        args, kwargs = start.call_args
        self.assertEqual((args[1], args[2], args[3]), (self.root, 7, "off"))
        self.assertEqual(kwargs["extra"], {"N64R_GOLDEN_DIR": str(self.root / cov.ORACLE_DIR)})
        profile.section.return_value = {}
        self.assertFalse(cov.run_oracle(profile, {"exit_after": 7}, self.root, {}))

    def test_elf_functions_keep_sized_functions_only(self):
        with mock.patch.object(cov.subprocess, "run", return_value=mock.Mock(stdout=READELF)):
            found = cov.elf_functions(Path("x.elf"))
        self.assertEqual(found, {"func_80200400_us_rev1": (0x80200400, 32), "func_80200500_de": (0x80200500, 100)})

    def test_source_files_find_c_bodies_and_include_asm(self):
        src = self.root / "src" / "game"
        src.mkdir(parents=True)
        (src / "A.CPP").write_text('void other(void);\nint func_1_de(int a)\n{\n}\nINCLUDE_ASM("x", func_2_de);\n')
        found = cov.source_files(self.root, {"func_1_de", "func_2_de", "other"})
        self.assertEqual(found, {"func_1_de": "src/game/A.CPP", "func_2_de": "src/game/A.CPP"})

    def test_settings_absent_without_the_section(self):
        profile = mock.Mock()
        profile.section.return_value = {}
        self.assertIsNone(cov.settings(profile, {}))


class UnionTests(unittest.TestCase):
    def run_of(self, movie, port, oracle, port_polls=100, oracle_polls=100):
        return {"movie": movie, "port": port, "oracle": oracle, "port_polls": port_polls, "oracle_polls": oracle_polls}

    def test_states_percentages_and_the_common_horizon(self):
        rows = [
            row("both", 100),
            row("orc", 50),
            row("late", 10),
            row("port", 20),
            row("none", 30),
            row("snd", 8, scope="sdk", file="src/sdk/libultra/x.c"),
            row("other", 5, versions=("eu",)),
        ]
        runs = [
            self.run_of("m1", {"both": 3, "port": 1}, {"both": (5, 1), "orc": (40, 9), "late": (150, 2)}),
        ]
        runs[0]["oracle_polls"] = 160
        table = cov.union(rows, {"us-rev1": 6, "eu": 1}, "us-rev1", runs, {"both", "port"})
        states = {r["name"]: r["state"] for r in table["functions"]}
        self.assertEqual(
            states,
            {
                "both": "both",
                "orc": "oracle-only",
                "late": "oracle-late",
                "port": "port-only",
                "none": "neither",
                "snd": "neither",
                "other": "neither",
            },
        )
        game = table["summary"]["game"]
        self.assertEqual(game["functions"], 5)
        self.assertEqual(game["port"]["functions"], 2)
        self.assertEqual(game["port"]["bytes_pct"], round(100.0 * 120 / 210, 2))
        self.assertEqual(game["either"]["functions"], 4)
        self.assertEqual(table["summary"]["sdk"]["functions"], 1)
        self.assertEqual(table["summary"]["states"]["oracle-only"], 1)
        names = [r["name"] for r in table["functions"]]
        self.assertEqual([names[i] for i in table["covered"][0]], ["both", "orc", "late", "port"])  # per-movie sets
        orc = next(r for r in table["functions"] if r["name"] == "orc")
        self.assertEqual((orc["oracle"]["movie"], orc["oracle"]["poll"], orc["port_linked"]), ("m1", 40, False))

    def test_no_oracle_means_port_hits_are_just_port(self):
        runs = [self.run_of("m1", {"a": 1}, None)]
        table = cov.union([row("a", 4), row("b", 4)], {"us-rev1": 2}, "us-rev1", runs, None)
        self.assertEqual({r["name"]: r["state"] for r in table["functions"]}, {"a": "port", "b": "neither"})
        self.assertEqual(table["summary"]["oracle_movies"], 0)

    def test_lists_are_the_two_work_lists_biggest_first(self):
        runs = [self.run_of("m1", {}, {"x": (1, 1), "y": (1, 1)})]
        table = cov.union([row("x", 10), row("y", 90), row("z", 5)], {"us-rev1": 3}, "us-rev1", runs, set())
        view = cov.lists(table, 1)
        self.assertEqual([r["name"] for r in view["oracle_only"]], ["y"])
        self.assertEqual([r["name"] for r in view["neither"]], ["z"])
        self.assertIn("oracle-only 2", cov.line(table))


class RunReportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.build = self.root / "build"
        (self.build / "decomp" / "src").mkdir(parents=True)
        (self.build / "ragewars-decomp").mkdir()
        (self.build / "decomp" / "src" / "A.CPP.gcno").write_text("notes")
        self.run = self.root / "runs" / "plays" / "r1"
        self.run.mkdir(parents=True)
        self.profile = mock.Mock()
        self.profile.section.return_value = {"coverage": {"elf": "e"}}
        self.profile.path.side_effect = lambda _name, value: Path(value)
        self.cfg = {"port": self.build / "ragewars", "runs": self.root / "runs"}

    def counters(self):
        folder = self.run / "gcov" / str(self.build).lstrip("/") / "decomp" / "src"
        folder.mkdir(parents=True)
        (folder / "A.CPP.gcda").write_text("counts")
        return folder

    def test_a_run_without_counters_has_no_report(self):
        with mock.patch.object(cov, "gcovr") as gcovr:
            self.assertIsNone(cov.run_report(self.profile, self.cfg, self.run))
        gcovr.assert_not_called()

    def test_counters_get_their_notes_and_one_gcovr_json(self):
        folder = self.counters()
        with mock.patch.object(cov, "gcovr") as gcovr:
            out = cov.run_report(self.profile, self.cfg, self.run)
        self.assertEqual(out, self.run / "coverage.json")
        self.assertEqual((folder / "A.CPP.gcno").resolve(), self.build / "decomp" / "src" / "A.CPP.gcno")
        args, cwd = gcovr.call_args.args
        self.assertEqual(cwd, self.run)
        self.assertEqual(args[args.index("--json") + 1], str(out))
        self.assertEqual(args[args.index("--root") + 1], str(self.build))
        self.assertEqual(args[-1], str(self.run / "gcov" / str(self.build).lstrip("/")))

    def test_gcovr_runs_in_a_scratch_folder_that_does_not_outlive_it(self):
        seen = {}

        def fake(command, cwd, **_):
            seen["cwd"] = Path(cwd)
            (Path(cwd) / "A.CPP##0.gcov.json.gz").write_bytes(b"x")
            return mock.Mock(returncode=0, stderr="")

        with mock.patch.object(cov.subprocess, "run", side_effect=fake):
            cov.gcovr(["--root", "/x"], self.run)
        self.assertNotEqual(seen["cwd"], self.run)
        self.assertEqual(list(self.run.glob("*gcov*")) + list(self.run.glob(".gcovr-*")), [])

    def test_a_failed_gcovr_names_its_cause_not_only_the_trailing_paths(self):
        stderr = (
            "(INFO) Reading\n(ERROR) Got function f in a.c on multiple lines: 3, 4.\n"
            "GCOV source files:\n\t/r/1.json\n\t/r/2.json\n"
        )
        self.assertEqual(cov.failure(stderr), "(ERROR) Got function f in a.c on multiple lines: 3, 4. | /r/2.json")
        self.assertEqual(cov.failure(""), "no output")

    def test_an_older_build_counts_only_for_source_the_current_build_shares(self):
        def report(path, md5):
            files = [
                {"file": f, "lines": [{"line_number": 1, "gcovr/md5": m}], "functions": []} for f, m in md5.items()
            ]
            path.write_text(json.dumps({"files": files}))
            return path

        base = report(self.run / "base.json", {"a.c": "x", "b.c": "y"})
        old = report(self.run / "old.json", {"a.c": "x", "b.c": "z"})
        changed = {f for f, sig in cov.file_signatures(old).items() if cov.file_signatures(base)[f] != sig}
        self.assertEqual(changed, {"b.c"})
        kept = cov.comparable(old, changed, self.run / "old.on-x.json")
        self.assertEqual(kept, self.run / "old.on-x.json")
        self.assertEqual([e["file"] for e in json.loads(kept.read_text())["files"]], ["a.c"])
        self.assertEqual(cov.comparable(old, set(), self.run / "unused.json"), old)
        self.assertNotIn("--merge-mode-functions", " ".join((self.counters(), cov._gcovr_base(self.build))[1]))

    def test_a_tree_that_is_not_a_coverage_build_is_refused(self):
        self.counters()
        (self.build / "ragewars-decomp").rmdir()
        with self.assertRaises(cov.DecompError):
            cov.run_report(self.profile, self.cfg, self.run)

    def test_orphan_notes_of_vanished_units_are_dropped_with_their_counters(self):
        (self.build / "ragewars-decomp" / "src").mkdir()
        (self.build / "ragewars-decomp" / "src" / "A.CPP").write_text("code")
        gone = self.build / "decomp" / "src" / "Gone.CPP"
        gone.with_suffix(".CPP.gcno").write_text("n")
        gone.with_suffix(".CPP.gcda").write_text("c")
        self.assertEqual(cov.drop_orphan_notes(self.build), 1)
        self.assertTrue((self.build / "decomp" / "src" / "A.CPP.gcno").is_file())
        self.assertEqual(list((self.build / "decomp" / "src").glob("Gone*")), [])

    def test_the_html_merge_runs_once_per_set_of_tracefiles(self):
        """The --add-tracefile merge over every report cost ~358 CPU s; a re-run over the same tracefiles (no new run,
        a --rebaseline) reuses the page in place, and a changed tracefile merges again."""
        runs = self.cfg["runs"]
        run = runs / "plays" / "r2"
        run.mkdir()
        (run / "coverage.json").write_text("{}")
        (runs / "b.json").write_text("{}")
        html = []

        def fake_gcovr(args, _cwd):
            if "--html-details" in args:
                html.append(args)
                Path(args[args.index("--html-details") + 1]).write_text("new")
                Path(args[args.index("--json-summary") + 1]).write_text(json.dumps({"function_covered": 3}))

        cov_cfg = {"elf": "e", "version": "v", "versions": ["v"], "source": None}
        with (
            mock.patch.object(cov, "settings", return_value=cov_cfg),
            mock.patch.object(cov, "latest_runs", return_value={"m": run}),
            mock.patch.object(cov, "baseline", return_value=runs / "b.json"),
            mock.patch.object(cov, "gcovr", side_effect=fake_gcovr),
            mock.patch.object(cov, "universe", return_value=([], {})),
            mock.patch.object(cov, "union", side_effect=lambda *a, **k: {"covered": [[]], "summary": {}}),
            mock.patch.object(cov.tas_symbolize, "symbols", return_value=(None, [])),
            mock.patch.object(cov, "oracle_bracket", return_value=None),
            mock.patch.object(cov, "read_oracle", return_value=None),
            mock.patch("abu_decomp.tas.read_record", return_value={}),
        ):
            cov.build(self.profile, self.cfg, oracle=False, jobs=1)
            cov.build(self.profile, self.cfg, oracle=False, jobs=1)
            self.assertEqual(len(html), 1)
            self.assertEqual((runs / "coverage" / "index.html").read_text(), "new")
            (run / "coverage.json").write_text("{ }")
            cov.build(self.profile, self.cfg, oracle=False, jobs=1)
            self.assertEqual(len(html), 2)

    def test_a_union_covering_fewer_functions_is_written_beside_the_report(self):
        runs = self.cfg["runs"]
        (runs / "coverage").mkdir(parents=True)
        (runs / "coverage" / "index.html").write_text("old")
        (runs / "coverage-summary.json").write_text("{}")
        (runs / "coverage-table.json").write_text(json.dumps({"gcovr": {"function_covered": 50}}))
        run = runs / "plays" / "r2"
        run.mkdir()
        (run / "coverage.json").write_text("{}")

        def fake_gcovr(args, _cwd):
            Path(args[args.index("--html-details") + 1]).write_text("new")
            Path(args[args.index("--json-summary") + 1]).write_text(json.dumps({"function_covered": 3}))

        cov_cfg = {"elf": "e", "version": "v", "versions": ["v"], "source": None}
        with (
            mock.patch.object(cov, "settings", return_value=cov_cfg),
            mock.patch.object(cov, "latest_runs", return_value={"m": run}),
            mock.patch.object(cov, "baseline", return_value=runs / "b.json"),
            mock.patch.object(cov, "gcovr", side_effect=fake_gcovr),
            mock.patch.object(cov, "universe", return_value=([], {})),
            mock.patch.object(cov, "union", return_value={"covered": [[]], "summary": {}}),
            mock.patch.object(cov.tas_symbolize, "symbols", return_value=(None, [])),
            mock.patch.object(cov, "oracle_bracket", return_value=None),
            mock.patch.object(cov, "read_oracle", return_value=None),
            mock.patch("abu_decomp.tas.read_record", return_value={}),
        ):
            report = cov.build(self.profile, self.cfg, oracle=False, jobs=1)
        self.assertIn("covers 3 functions", report["refused"])
        # PF4: the refusal names the runs of another port build and the way out
        self.assertEqual(report["stale_runs"], [{"movie": "m", "run": "r2", "build": "unknown"}])
        self.assertIn("decomp tas play --all --resume", report["next"])
        self.assertIn("--rebaseline", report["next"])
        self.assertEqual(cov.lists(report)["stale_runs"], report["stale_runs"])
        self.assertEqual((runs / "coverage" / "index.html").read_text(), "old")
        self.assertEqual(cov.covered_functions(cov.read_table(self.cfg)), 50)
        self.assertTrue((runs / "coverage-table.refused.json").is_file())
        self.assertFalse((runs / "coverage.new").exists())
        # --rebaseline (TAS-COVER-3: the join key changed on purpose): the smaller union replaces it, the old stays
        cov_cfg = {"elf": "e", "version": "v", "versions": ["v"], "source": None}
        with (
            mock.patch.object(cov, "settings", return_value=cov_cfg),
            mock.patch.object(cov, "latest_runs", return_value={"m": run}),
            mock.patch.object(cov, "baseline", return_value=runs / "b.json"),
            mock.patch.object(cov, "gcovr", side_effect=fake_gcovr),
            mock.patch.object(cov, "universe", return_value=([], {})),
            mock.patch.object(cov, "union", return_value={"covered": [[]], "summary": {}}),
            mock.patch.object(cov.tas_symbolize, "symbols", return_value=(None, [])),
            mock.patch.object(cov, "oracle_bracket", return_value=None),
            mock.patch.object(cov, "read_oracle", return_value=None),
            mock.patch("abu_decomp.tas.read_record", return_value={}),
        ):
            report = cov.build(self.profile, self.cfg, oracle=False, jobs=1, rebaseline=True)
        self.assertNotIn("refused", report)
        self.assertEqual(report["rebaselined"], 50)
        self.assertEqual(cov.covered_functions(cov.read_table(self.cfg)), 3)
        self.assertEqual(cov.covered_functions(json.loads((runs / "coverage-table.prev.json").read_text())), 50)

    def test_functions_whose_source_moved_between_merged_builds_are_flagged(self):
        a = {"x.c:f": "1-9", "x.c:g": "10-20", "x.c:h": "30-40"}
        b = {"x.c:f": "1-9", "x.c:g": "10-22"}
        mixed = cov.mixed_builds({"aaaaaaaaaaaaaaaa": a, "bbbbbbbbbbbbbbbb": b})
        self.assertEqual([m["function"] for m in mixed], ["g", "h"])
        self.assertEqual(mixed[0]["spans"], {"aaaaaaaaaaaa": "10-20", "bbbbbbbbbbbb": "10-22"})
        self.assertEqual(mixed[1]["spans"]["bbbbbbbbbbbb"], "absent")
        self.assertEqual(cov.mixed_builds({"a": a}), [])

    def test_spans_reads_gcovr_function_positions(self):
        path = self.run / "c.json"
        path.write_text(
            json.dumps(
                {
                    "files": [
                        {
                            "file": "x.c",
                            "functions": [
                                {"name": "f", "lineno": 3, "pos": ["3:6", "9:1"]},
                                {"name": "g", "lineno": 12},
                            ],
                        }
                    ]
                }
            )
        )
        self.assertEqual(cov.spans(path), {"x.c:f": "3-9", "x.c:g": "12-12"})
        self.assertEqual(cov.spans(self.run / "missing.json"), {})

    def test_a_builds_runs_merge_once_into_the_cache(self):
        cache = self.run / "cache"
        cache.mkdir()
        runs = [self.run / "r1.json", self.run / "r2.json"]

        def fake(args, _cwd):
            Path(args[args.index("--json") + 1]).write_text("{}")

        with (
            mock.patch.object(cov, "gcovr", side_effect=fake) as gcovr,
            mock.patch.object(cov, "_gcovr_base", return_value=[]),
        ):
            first = cov.merge_build("abcdef0123456789", runs, cache, self.build)
            again = cov.merge_build("abcdef0123456789", runs, cache, self.build)
        self.assertEqual(first, again)
        self.assertEqual(gcovr.call_count, 1)
        self.assertEqual(cov.merge_build("x", runs[:1], cache, self.build), runs[0])

    def test_table_only_rebuilds_the_table_without_gcovr_and_keeps_the_page_fields(self):
        runs = self.run / "runs"
        runs.mkdir()
        (runs / "coverage-table.json").write_text(
            json.dumps({"gcovr": {"function_covered": 7}, "baseline": "b1", "changed_functions": 2, "mixed_build": []})
        )
        cfg = {"runs": runs, "port": self.build}
        fresh = {"functions": [{"name": "f"}], "covered": []}
        with (
            mock.patch.object(cov, "settings", return_value={"version": "v"}),
            mock.patch.object(cov, "latest_runs", return_value={}),
            mock.patch.object(cov, "drop_orphan_notes"),
            mock.patch.object(cov, "symbol_tables", return_value=({}, {})),
            mock.patch.object(cov, "universe", return_value=([], {})),
            mock.patch.object(cov, "resolver"),
            mock.patch.object(cov.tas_symbolize, "symbols", return_value=(None, [])),
            mock.patch.object(cov, "union", return_value=fresh),
            mock.patch.object(cov, "gcovr", side_effect=AssertionError("gcovr must not run")),
        ):
            report = cov.build(self.profile, cfg, oracle=False, table_only=True)
        self.assertTrue(report["table_only"])
        self.assertEqual((report["gcovr"], report["baseline"]), ({"function_covered": 7}, "b1"))
        self.assertEqual(json.loads((runs / "coverage-table.json").read_text())["functions"][0]["name"], "f")

    def test_a_smaller_union_passes_only_by_the_functions_of_changed_source(self):
        old = {"gcovr": {"function_covered": 100}, "baseline": "a"}
        same = {"gcovr": {"function_covered": 99}, "baseline": "a", "changed_functions": 50}
        self.assertIn("covers 99", cov.refusal(old, same))
        moved = {"gcovr": {"function_covered": 95}, "baseline": "b", "changed_functions": 5}
        self.assertIsNone(cov.refusal(old, moved))
        lost = {"gcovr": {"function_covered": 94}, "baseline": "b", "changed_functions": 5}
        self.assertIn("another port build", cov.refusal(old, lost))
        self.assertIsNone(cov.refusal(None, lost))

    def test_the_page_lists_the_dropped_functions_by_name(self):
        dropped = [{"file": "x.c", "function": "f"}]
        view = cov.lists({"functions": [], "dropped_functions": dropped})
        self.assertEqual(view["dropped_functions"], dropped)

    def test_function_counts_are_cached_beside_the_report(self):
        path = self.run / "coverage.json"
        path.write_text(json.dumps({"files": [{"file": "x.c", "functions": [{"name": "f", "execution_count": 2}]}]}))
        self.assertEqual(cov.read_gcovr(path), {"f": 2})
        self.assertEqual(json.loads((self.run / "coverage-fns.json").read_text()), {"f": 2})
        path.write_text("not json")
        os.utime(path, (1, 1))
        self.assertEqual(cov.read_gcovr(path), {"f": 2})

    def test_a_method_name_keeps_its_file_apart(self):
        mixed = cov.mixed_builds({"a": {"x.c:A::f": "1-2"}, "b": {"x.c:A::f": "1-3"}})
        self.assertEqual((mixed[0]["file"], mixed[0]["function"]), ("x.c", "A::f"))

    def test_no_section_no_report(self):
        self.profile.section.return_value = {}
        self.assertIsNone(cov.run_report(self.profile, self.cfg, self.run))


if __name__ == "__main__":
    unittest.main()


class AddressJoinTests(unittest.TestCase):
    """A rename in the source must not lose a function's coverage: the join is by ROM address."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.source = Path(directory.name)
        (self.source / "versions").mkdir()
        (self.source / "versions" / "symbols.txt").write_text(
            "Cleaned = 0x80200400; // type:func\nSecond = 0x80200500; // type:func\nD_1 = 0x800E0000;\n"
        )
        (self.source / "versions" / "aliases.txt").write_text("Alias = 0x80200400; // type:func\n")
        self.cfg = {
            "elf": "{version}.elf",
            "version": "us-rev1",
            "versions": ["eu", "us-rev1"],
            "source": self.source,
            "symbols": ["versions/symbols.txt", "versions/aliases.txt"],
        }

    def elfs(self, path):
        if str(path) == "us-rev1.elf":
            return {"func_80200400_us_rev1": (0x80200400, 32), "Second": (0x80200500, 100)}
        return {"func_80200400_us_rev1": (0x80200400, 40), "only_eu": (0x80300000, 8)}

    def test_symbol_tables_read_functions_only_and_the_first_name_at_an_address_wins(self):
        by_name, first = cov.symbol_tables(self.cfg)
        self.assertEqual(by_name, {"Cleaned": 0x80200400, "Second": 0x80200500, "Alias": 0x80200400})
        self.assertEqual(first, {0x80200400: "Cleaned", 0x80200500: "Second"})
        self.assertEqual(cov.symbol_tables({"source": self.source}), ({}, {}))

    def test_the_universe_names_a_function_by_what_the_source_calls_it_now_and_joins_other_versions(self):
        symbols = cov.symbol_tables(self.cfg)
        with (
            mock.patch.object(cov, "elf_functions", side_effect=self.elfs),
            mock.patch.object(cov, "source_files", return_value={}),
        ):
            rows, totals = cov.universe(self.cfg, symbols)
        by = {r["name"]: r for r in rows}
        self.assertEqual(sorted(by), ["Cleaned", "Second", "only_eu"])
        self.assertEqual(by["Cleaned"]["elf_name"], "func_80200400_us_rev1")
        self.assertEqual((by["Cleaned"]["address"], by["Cleaned"]["size"]), (0x80200400, 32))
        self.assertEqual(sorted(by["Cleaned"]["versions"]), ["eu", "us-rev1"])
        self.assertEqual(totals, {"eu": 2, "us-rev1": 2})

    def test_an_old_name_a_new_name_and_an_alias_all_land_on_the_one_row(self):
        symbols = cov.symbol_tables(self.cfg)
        with (
            mock.patch.object(cov, "elf_functions", side_effect=self.elfs),
            mock.patch.object(cov, "source_files", return_value={}),
        ):
            rows, _ = cov.universe(self.cfg, symbols)
        resolve = cov.resolver(rows, "us-rev1", symbols[0])
        self.assertEqual(
            [resolve(n) for n in ("Cleaned", "Alias", "func_80200400_us_rev1", "helper")],
            ["Cleaned", "Cleaned", "Cleaned", "helper"],
        )
        port = cov.fold({"Cleaned": 3, "Alias": 4, "helper": 1}, resolve, lambda a, b: a + b)
        self.assertEqual(port, {"Cleaned": 7, "helper": 1})
        oracle = cov.fold({"func_80200400_us_rev1": (9, 5), "Cleaned": (2, 1)}, resolve, cov._first_seen)
        self.assertEqual(oracle, {"Cleaned": (2, 6)})
        table = cov.union(
            rows,
            {"us-rev1": 2, "eu": 2},
            "us-rev1",
            [{"movie": "m", "port": port, "oracle": oracle, "port_polls": 10, "oracle_polls": 10}],
            None,
        )
        hit = {r["name"]: r["state"] for r in table["functions"]}
        self.assertEqual((hit["Cleaned"], hit["Second"]), ("both", "neither"))


def test_a_published_page_port_finds_its_build_tree_through_the_build_units(tmp_path):
    """PF7 (PORT-FIX-O1): the page profile runs a port published away from its build tree (live/port), and coverage
    refused it as "not a coverage build". The tree tas.build.units lies in (its CMakeCache.txt) is the build."""
    build = tmp_path / "build"
    (build / "decomp/src").mkdir(parents=True)
    (build / "CMakeCache.txt").write_text("")
    (build / "ragewars-decomp").mkdir()
    (tmp_path / "live/port").mkdir(parents=True)
    cfg = {"port": tmp_path / "live/port/ragewars", "build": {"units": build / "decomp/src"}}
    assert cov.build_dir(cfg) == build
    assert cov.build_dir({"port": build / "ragewars", "build": {"units": build / "decomp/src"}}) == build
    assert cov.build_dir({"port": tmp_path / "live/port/ragewars"}) == tmp_path / "live/port"
