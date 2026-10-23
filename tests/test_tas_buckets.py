import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp import tas_buckets, tas_findings

CRASH = {
    "movie": "a",
    "id": "run-a",
    "outcome": "crash",
    "subject": "host-fault-00000010",
    "polls_reached": 40,
    "frame": "func_8041F2A0_us_rev1+0x1c",
    "fault": {
        "at": "func_8041F2A0_us_rev1+0x1c",
        "callers": ["func_80402000_de+0x4", "thread_main+0x12", "_fini+0x1"],
        "si_addr": "0x00000010",
        "access": "write",
        "port": "/nonexistent",
    },
}


def project(tmp: Path) -> Path:
    (tmp / "versions" / "us-rev1").mkdir(parents=True)
    (tmp / "config.toml").write_text('names_from = "us-rev1"\n')
    (tmp / "versions" / "us-rev1" / "symbol_addrs.txt").write_text(
        "D_800E42D0 = 0x800E42D0; // rom:0x1\nD_800E4300 = 0x800E4300; //\nfunc_80200400 = 0x80200400; //\n"
    )
    (tmp / "src").mkdir()
    (tmp / "src" / "t.c").write_text("unsigned int D_800E42D0[35] = {\n 1,\n};\n")
    return tmp


class LocateTests(unittest.TestCase):
    def test_small_addresses_are_null_plus_offset(self):
        self.assertEqual(tas_findings.locate(0x10), {"base": "null", "offset": 0x10})

    def test_guest_addresses_name_the_datum_in_either_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = project(Path(tmp))
            self.assertEqual(tas_findings.locate(0x800E42D6, project=proj), {"datum": "D_800E42D0", "offset": 6})
            self.assertEqual(tas_findings.locate(0xA00E42D6, project=proj)["datum"], "D_800E42D0")
            self.assertEqual(tas_findings.locate(0x80700000, project=proj), {"region": "guest RAM"})
            self.assertEqual(tas_findings.locate(0x80800000, project=proj), {"region": "past the guest RAM end"})

    def test_host_addresses_name_a_data_symbol_of_the_run_executable(self):
        out = "080f4b00 00000100 D D_port_table\n080f4c00 00000010 b state\n"
        with tempfile.NamedTemporaryFile() as exe, mock.patch("subprocess.run") as run:
            run.return_value.stdout = out
            self.assertEqual(tas_findings.locate(0x080F4BAC, port=exe.name), {"datum": "D_port_table", "offset": 0xAC})
            self.assertEqual(tas_findings.locate(0x080F4D00, port=exe.name), {"region": "host"})


class RunFindingTests(unittest.TestCase):
    def test_a_null_fault_roots_at_its_offset_in_the_function(self):
        (row,) = tas_findings.from_run(CRASH, Path("/r/run-a"), batch="b1")
        self.assertEqual(
            (row["base"], row["offset"], row["fn"], row["callers"]),
            ("null", 16, "func_8041F2A0_us_rev1", ["func_80402000_de", "thread_main"]),
        )
        self.assertEqual(row["signature"], "null+0x10 in func_8041F2A0_us_rev1")
        self.assertEqual((row["poll"], row["batch"], row["evidence"]), (40, "b1", "/r/run-a"))

    def test_a_clean_replay_clears_and_a_human_run_is_nothing(self):
        (clear,) = tas_findings.from_run({"movie": "a", "outcome": "ok", "id": "r"})
        self.assertEqual((clear["kind"], clear["clears"][0]), ("clear", "crash"))
        self.assertEqual(tas_findings.from_run({**CRASH, "input": "human"}), [])
        self.assertEqual(tas_findings.from_run({**CRASH, "stopped": True}), [])

    def test_a_stop_names_its_function(self):
        (row,) = tas_findings.from_run(
            {"movie": "m", "outcome": "stop", "subject": "boot-missing: func_8041CAD8_de", "polls_reached": 3}
        )
        self.assertEqual((row["fn"], row["signature"]), ("func_8041CAD8_de", "func_8041CAD8_de"))

    def test_a_guest_datum_fault(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = project(Path(tmp))
            rec = {**CRASH, "fault": {**CRASH["fault"], "si_addr": "0x800e42d6"}}
            (row,) = tas_findings.from_run(rec, None, project=proj)
        self.assertEqual((row["datum"], row["offset"], row["signature"]), ("D_800E42D0", 6, "datum D_800E42D0"))


class FeedTests(unittest.TestCase):
    def test_append_is_idempotent_and_read_skips_torn_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            row = {"kind": "crash", "movie": "a", "poll": 1, "run": "r1", "signature": "s"}
            self.assertEqual(tas_findings.append(runs, [row, dict(row)]), 1)
            self.assertEqual(tas_findings.append(runs, [row]), 0)
            with (runs / "findings.jsonl").open("a") as feed:
                feed.write(
                    "{torn\n[1]\n"
                    + json.dumps({"kind": "state-diff", "movie": "b", "signature": {"region": "actors"}})
                    + "\n"
                )
            rows = tas_findings.read(runs)
            self.assertEqual([r["kind"] for r in rows], ["crash", "state-diff"])
            self.assertEqual(rows[1]["signature"], '{"region": "actors"}')

    def test_a_movies_newest_run_replaces_and_a_clean_run_clears(self):
        rows = [
            {"kind": "crash", "movie": "a", "fn": "f", "id": "1"},
            {"kind": "stop", "movie": "a", "fn": "g", "id": "2"},
            {"kind": "crash", "movie": "b", "fn": "f", "id": "3"},
            {"kind": "clear", "movie": "b", "clears": ["crash", "stop"], "id": "4"},
            {"kind": "state-diff", "movie": "a", "signature": "actors", "id": "5"},
        ]
        self.assertEqual({r["id"] for r in tas_findings.active(rows)}, {"2", "5"})

    def test_backfill_takes_the_newest_run_of_each_movie_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            for name, started, rec in (
                ("r1", 1, {**CRASH, "id": "r1"}),
                ("r2", 2, {"movie": "a", "outcome": "ok", "id": "r2"}),
                ("r3", 3, {"movie": "b", "outcome": "hang", "id": "r3", "subject": "no exit", "polls_reached": 9}),
            ):
                (runs / "plays" / name).mkdir(parents=True)
                (runs / "plays" / name / "run.json").write_text(
                    json.dumps({**rec, "started": started, "status": "done"})
                )
            self.assertEqual(tas_findings.backfill(runs), 2)
            self.assertEqual(tas_findings.backfill(runs), 0)
            self.assertEqual(
                [(r["kind"], r["movie"]) for r in tas_findings.read(runs)], [("clear", "a"), ("hang", "b")]
            )


def sweep_row(**extra):
    return {
        "kind": "sweep",
        "movie": None,
        "poll": None,
        "signature": "sweep word-array-byte-use",
        "rule": "word-array-byte-use",
        **extra,
    }


class BucketTests(unittest.TestCase):
    def test_runs_and_sweep_hits_that_share_a_root_share_a_bucket(self):
        rows = [
            {
                "kind": "crash",
                "movie": "a",
                "poll": 5,
                "datum": "D_X",
                "offset": 2,
                "id": "1",
                "signature": "datum D_X",
            },
            {
                "kind": "crash",
                "movie": "b",
                "poll": 9,
                "datum": "D_X",
                "offset": 2,
                "id": "2",
                "signature": "datum D_X",
            },
            {"kind": "crash", "movie": "c", "poll": 1, "fn": "f", "callers": ["g"], "id": "3"},
            sweep_row(datum="D_X", evidence="/t/src/a.c:10", fp="1", id="4"),
            sweep_row(evidence="/t/src/b.c:11", fp="2", id="5", text="x"),
            sweep_row(evidence="/t/src/c.c:12", fp="3", id="6"),
            sweep_row(
                kind="sweep", signature="sweep dropped", fn="f", callers=["g"], evidence="/t/src/d.c:4", fp="4", id="7"
            ),
        ]
        buckets = tas_buckets.build(rows, "/p.json")
        self.assertEqual(
            [(b["key"], b["count"], b["movies"]) for b in buckets][:2],
            [("datum D_X", 3, ["a", "b"]), ("f <- g", 2, ["c"])],
        )
        self.assertEqual(buckets[0]["offsets"], [2])
        self.assertEqual(buckets[0]["next"], "decomp --profile /p.json tas play a --speed 1")
        rule = buckets[2]
        self.assertEqual(
            (rule["key"], rule["level"], rule["sites"], rule["movies"]), ("sweep word-array-byte-use", "sweep", 2, [])
        )
        self.assertEqual(rule["next"], "sed -n '8,17p' /t/src/b.c")
        self.assertEqual([b["rank"] for b in buckets], [1, 2, 3])

    def test_batch_filter_and_top(self):
        rows = [
            {"kind": "crash", "movie": "a", "fn": "f", "batch": "b1", "id": "1"},
            {"kind": "crash", "movie": "b", "fn": "g", "batch": "b2", "id": "2"},
        ]
        self.assertEqual([b["key"] for b in tas_buckets.build(rows, batch="b2")], ["g"])
        self.assertEqual(len(tas_buckets.build(rows, top=1)), 1)

    def _ambient_rows(self):
        rows = []
        for n in range(10):  # a boot-wide report in every sanitized movie, one more in two movies only
            rows.append({"kind": "ubsan", "movie": f"m{n}", "fn": "Boot", "fp": "a", "id": f"u{n}"})
            rows.append({"kind": "ubsan", "movie": f"m{n}", "signature": "ubsan:s.c:5", "fp": "b", "id": f"v{n}"})
        for n in range(2):
            rows.append({"kind": "ubsan", "movie": f"m{n}", "signature": "ubsan:rare.c:9", "fp": "c", "id": f"w{n}"})
        rows += [
            {"kind": "crash", "movie": "x", "fn": "Real", "base": "null", "offset": 16, "id": "c1"},
            {"kind": "hang", "movie": "y", "id": "h1"},
            {"kind": "state-diverge", "movie": "z", "fn": "Div", "id": "s1"},
            sweep_row(evidence="/t/src/b.c:11", fp="2", id="5", text="x"),
        ]
        return rows

    def test_crash_outranks_sanitizer_and_boot_wide_reports_collapse_into_one_ambient_line(self):
        buckets = tas_buckets.build(self._ambient_rows(), "/p.json")
        keys = [b["key"] for b in buckets]
        self.assertEqual(set(keys[:3]), {"null+0x10 in Real", "hang", "Div"})
        self.assertEqual(keys[3], "ubsan:rare.c:9")
        self.assertEqual(buckets[-1]["level"], "ambient")
        self.assertEqual(buckets[-1]["sites"], 2)
        self.assertNotIn("Boot", keys)
        self.assertEqual(keys[-2], "sweep word-array-byte-use")
        self.assertEqual([b["rank"] for b in buckets], list(range(1, len(buckets) + 1)))
        top = tas_buckets.build(self._ambient_rows(), "/p.json", top=1)
        self.assertEqual(len(top), 2)
        self.assertEqual(top[-1]["level"], "ambient")
        self.assertIn("[ambient]", tas_buckets.render(buckets))

    def test_kind_filter_and_sanitizer_kind_shows_the_ambient_reports(self):
        rows = self._ambient_rows()
        self.assertEqual([b["key"] for b in tas_buckets.build(rows, kinds=["crash"])], ["null+0x10 in Real"])
        got = tas_buckets.build(rows, kinds=["ubsan"])
        self.assertNotIn("ambient", [b["level"] for b in got])
        self.assertIn("Boot", [b["key"] for b in got])

    def test_few_sanitized_movies_are_never_ambient(self):
        rows = [{"kind": "ubsan", "movie": f"m{n}", "fn": "Boot", "id": str(n)} for n in range(3)]
        self.assertEqual([b["level"] for b in tas_buckets.build(rows)], ["function"])

    def test_enrich_names_the_declaration_and_render_lists_buckets(self):
        rows = [{"kind": "crash", "movie": "a", "poll": 5, "datum": "D_800E42D0", "offset": 2, "fn": "f", "id": "1"}]
        with tempfile.TemporaryDirectory() as tmp:
            buckets = tas_buckets.build(rows, "/p.json")
            tas_buckets.enrich(project(Path(tmp)), buckets)
        self.assertEqual(buckets[0]["declared"], "unsigned int D_800E42D0[35]")
        self.assertIn("datum D_800E42D0 [unsigned int D_800E42D0[35]] touched at +0x2", buckets[0]["title"])
        text = tas_buckets.render(buckets)
        self.assertIn("1. [datum]", text)
        self.assertTrue(text.endswith("next: decomp --profile /p.json tas play a --speed 1"))
        self.assertEqual(tas_buckets.render([]), "buckets: none\nnext: nothing open")


if __name__ == "__main__":
    unittest.main()
