import json
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
        self.assertEqual((runs / "coverage" / "index.html").read_text(), "old")
        self.assertEqual(cov.covered_functions(cov.read_table(self.cfg)), 50)
        self.assertTrue((runs / "coverage-table.refused.json").is_file())
        self.assertFalse((runs / "coverage.new").exists())

    def test_no_section_no_report(self):
        self.profile.section.return_value = {}
        self.assertIsNone(cov.run_report(self.profile, self.cfg, self.run))


if __name__ == "__main__":
    unittest.main()
