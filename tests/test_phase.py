import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from abu_decomp import cli, nextcmd, phase
from abu_decomp.config import load_profile

FIXTURE = Path(__file__).parent / "fixtures" / "phase"


def git(tree: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(tree), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True
    )


class Fixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.dir = self.root / "tests" / "fixtures" / "phase"
        shutil.copytree(FIXTURE, self.dir)
        self.profile_file = self.dir / "profile.json"
        git(self.root, "init", "-q")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "fixture")

    def profile(self, edit=None, fresh=True):
        data = json.loads(self.profile_file.read_text())
        if fresh:
            table = self.dir / "runs" / "coverage-table.json"
            report = json.loads(table.read_text())
            report["generated"] = time.time()
            table.write_text(json.dumps(report))
        if edit:
            edit(data["phase"])
        self.profile_file.write_text(json.dumps(data))
        return load_profile(self.profile_file)

    def rows(self, result):
        return {(r["phase"], r["key"]): r for r in result["rows"]}


class PhaseTests(Fixture):
    def test_p1_numbers_fakematch_and_named(self):
        result, _ = phase.run(self.profile(), None, None, False)
        rows = self.rows(result)
        self.assertEqual(rows["p1", "mapped_pct"]["value"], 62.5)  # fns 0-32 with C + data 48-56 of 64
        self.assertEqual(rows["p1", "fuzzy_pct"]["value"], 66.67)
        self.assertEqual(rows["p1", "foundation.rng.exact_pct_b"]["value"], 100.0)
        self.assertEqual(rows["p1", "foundation.rng.named_pct_b"]["value"], 100.0)
        self.assertEqual(rows["p1", "foundation.input.exact_pct_b"]["value"], 0.0)  # score 50 and FAKEMATCH
        self.assertEqual(rows["p1", "foundation.input.named_pct_b"]["value"], 0.0)  # func_ call in the body
        self.assertFalse(rows["p1", "foundations_clean_bytes"]["met"])

    def test_a_fakematch_alone_is_not_exact(self):
        tree = self.root / "tests/fixtures/phase/src/a.c"
        cats = phase.foundations(
            self.root,
            "HEAD",
            "rng\tPak_Save\t16\t100.0\t1\ttests/fixtures/phase/src/a.c\n",
            {"Pak_Save": (100.0, str(tree.relative_to(self.root)))},
            "NOPE",
        )
        self.assertEqual(cats["rng"]["exact_bytes"], 0)
        self.assertEqual(cats["rng"]["named_bytes"], 16)

    def test_p2_absent_port_is_not_met(self):
        def edit(cfg):
            cfg["games"]["Alpha"]["port"] = None

        result, _ = phase.run(self.profile(edit), None, None, False)
        self.assertEqual(self.rows(result)["p2", "port"]["value"], "absent")
        self.assertFalse(self.rows(result)["p2", "port"]["met"])

    def test_p2_reads_the_newest_run(self):
        rows = self.rows(phase.run(self.profile(), None, None, False)[0])
        self.assertTrue(rows["p2", "port"]["met"])
        self.assertTrue(rows["p2", "oracle"]["met"])

    def test_p2_oracle_needs_a_done_ok_record(self):
        run = next((self.root / "tests/fixtures/phase/runs/plays").glob("*/run.json"))
        original = run.read_text()
        self.addCleanup(run.write_text, original)
        record = json.loads(original)
        record["oracle"] = {"status": "done", "outcome": "fail"}
        run.write_text(json.dumps(record))
        rows = self.rows(phase.run(self.profile(), None, None, False)[0])
        self.assertFalse(rows["p2", "oracle"]["met"])

    def test_mapped_counts_leftover_spans(self):
        data = {
            "rom_size": 100,
            "fns": [["f", 0, 40, 100.0, "a.c", 1]],
            "blobs": [],
            "spans": {"leftover": [[40, 100]]},
        }
        self.assertEqual(phase.mapped_bytes(data), (100, 100, 40))

    def test_p3_stale_report_finding(self):
        _, findings = phase.run(self.profile(fresh=False), None, None, False)
        self.assertIn("phase.stale", [f["key"] for f in findings])

    def test_p3_values(self):
        rows = self.rows(phase.run(self.profile(), None, None, False)[0])
        self.assertEqual(rows["p3", "fn_reachable"]["value"], 50.0)
        self.assertEqual(rows["p3", "differs_missing"]["value"], 2)
        self.assertTrue(rows["p3", "corpus_gate"]["met"])

    def test_p4_ladder_stage_between_r1_and_r2(self):
        ladder = [
            {"stage": "R1", "byte_reach": 50, "line": 40},
            {"stage": "R2", "byte_reach": 70, "line": 60, "oracle_only": 20},
        ]
        got = phase.p4(ladder, {"byte_reach": 55, "line": 45, "oracle_only": 30})
        self.assertEqual(got["stage"], "R1")
        self.assertEqual(got["next_stage"]["stage"], "R2")
        self.assertEqual([f["key"] for f in got["next_stage"]["failing"]], ["byte_reach", "line", "oracle_only"])

    def test_next_commands_are_runnable_and_name_fns(self):
        result, _ = phase.run(self.profile(), None, None, False)
        self.assertEqual(result["next"][0]["phase"], "p1")
        self.assertEqual(result["next"][0]["fn"], "Pak_Save")
        for item in result["next"]:
            self.assertEqual(nextcmd.problems(item["next"]), [])

    def test_p3_next_lists_oracle_only_first(self):
        profile = self.profile()
        cfg = phase.settings(profile)
        rows, _ = phase.p3(profile, cfg, "Alpha", "v1")
        items = phase.next_items(rows, {"foundations": {}}, profile, cfg)
        self.assertEqual([i["fn"] for i in items], ["Sound_Mix", "Menu_Draw"])
        for item in items:
            self.assertEqual(nextcmd.problems(item["next"]), [])

    def test_a_second_game_needs_only_profile_rows(self):
        other = self.root / "other"
        (other / "m").mkdir(parents=True)
        (other / "m" / "x.json").write_text(
            json.dumps(
                {
                    "rom_size": 100,
                    "fns": [["f_main", 0, 40, 100.0, "c/f.c", 1]],
                    "spans": {},
                    "blobs": [["b", 40, 20, "decoded"]],
                }
            )
        )
        git(other, "init", "-q")
        git(other, "add", ".")
        git(other, "commit", "-qm", "m")

        def edit(cfg):
            cfg["games"]["Beta"] = {
                "tree": str(other),
                "ref": "HEAD",
                "versions": ["x"],
                "map": "m/{version}.json",
                "foundations": None,
                "port": None,
            }

        rows = [r for r in phase.run(self.profile(edit), None, None, False)[0]["rows"] if r["game"] == "Beta"]
        by = {r["key"]: r for r in rows}
        self.assertEqual(by["mapped_pct"]["value"], 60.0)
        self.assertEqual(by["port"]["value"], "absent")

    def test_refresh_writes_out_and_history(self):
        result, _ = phase.run(self.profile(), None, None, True)
        out = self.dir / "out" / "phase.json"
        self.assertEqual(json.loads(out.read_text())["next"], result["next"])
        self.assertEqual(len((self.dir / "out" / "history.jsonl").read_text().splitlines()), 1)

    def test_cli_exit_zero(self):
        self.profile()
        self.assertEqual(cli.main(["--profile", str(self.profile_file), "phase"]), 0)


if __name__ == "__main__":
    unittest.main()
