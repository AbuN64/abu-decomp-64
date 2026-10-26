import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp import DecompError, tas, tas_corpus, tas_coverage
from abu_decomp.config import load_profile


def gcovr(functions: dict[str, int], branches: tuple[int, ...] = ()) -> dict:
    """A gcovr JSON report: one file with these function counts and one line with these branch counts."""
    lines = [{"line_number": 7, "count": 1, "branches": [{"count": c} for c in branches]}]
    funcs = [{"name": n, "execution_count": c} for n, c in functions.items()]
    return {"files": [{"file": "src/game/A.CPP", "lines": lines, "functions": funcs}]}


class Fixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "game.z64").write_bytes(b"rom")
        (self.root / "port").write_text("")
        seat = self.root / "seat.json"
        tas_section = {
            "movies": "movies",
            "runs": "runs",
            "port": "port",
            "rom": "game.z64",
            "args": [],
        }
        seat.write_text(json.dumps({"schema_version": 1, "name": "t", "tas": tas_section}))
        self.profile = load_profile(seat)
        self.cfg = tas.settings(self.profile)
        self.cfg["movies"].mkdir()
        self.n = 0

    def movie(self, name: str, polls: int = 10) -> None:
        sha = hashlib.sha1(b"rom").hexdigest()
        (self.cfg["movies"] / f"{name}.tas").write_text(f"# tas 1\n# rom-sha1 {sha}\n- *{polls}\n")

    def make_run(
        self,
        movie: str,
        *,
        functions: dict[str, int] | None = None,
        branches: tuple[int, ...] = (),
        outcome: str = "ok",
        reached: int = 10,
        hashes: list[str] | None = None,
        ram: dict[int, bytes] | None = None,
        dump_at: int | None = None,
        label: str | None = None,
        port: str | None = None,
        polls: int = 10,
    ) -> Path:
        self.n += 1
        run = self.cfg["runs"] / "plays" / f"2026{self.n:04d}-{movie}"
        run.mkdir(parents=True)
        record = {
            "id": run.name,
            "movie": movie,
            "status": "done",
            "outcome": outcome,
            "subject": None if outcome == "ok" else "host-fault-00000010",
            "polls": polls,
            "polls_reached": reached,
            "input": "movie",
            "label": label,
            "port": port or str(self.cfg["port"]),
            "build": tas.build_id(self.cfg["port"]),
            "ended": time.time(),
        }
        (run / tas.RECORD).write_text(json.dumps(record))
        (run / tas.HASH).write_text("".join(f"{i + 1} {h}\n" for i, h in enumerate(hashes or ["aa"] * reached)))
        (run / tas_coverage.COVERAGE).write_text(json.dumps(gcovr(functions or {"f1": 1}, branches)))
        if dump_at is not None:
            image = bytearray(0x200000)
            for address, data in (ram or {}).items():
                offset = address & 0x1FFFFFFF
                image[offset : offset + len(data)] = data
            (run / tas.RAM_DUMP.format(dump_at)).write_bytes(bytes(image))
        return run


class SpotTests(Fixture):
    def test_ram_expression_parses_addresses_names_and_ops(self):
        self.assertEqual(
            tas_corpus.parse_ram("u32@0x80111D24 == 0xE3F635E3"),
            {"type": "u32", "address": 0x80111D24, "op": "==", "value": 0xE3F635E3},
        )
        self.assertEqual(tas_corpus.parse_ram("s16@D_80111D28 < -3")["address"], 0x80111D28)
        for bad in ("u32 0x80111D24 == 1", "u64@0x80000000 == 1", "u8@0x00001000 == 1", "u8@0x80000000 ~ 1"):
            with self.assertRaises(DecompError):
                tas_corpus.parse_ram(bad)

    def test_ram_spot_reads_the_dump_in_host_byte_order(self):
        run = self.make_run("m", reached=10, dump_at=5, ram={0x80111D24: (0xE3F635E3).to_bytes(4, "little")})
        record = tas.read_record(run)
        good = tas_corpus.make_spot("m", "u32@0x80111D24 == 0xE3F635E3", 5, None, None, "")
        self.assertEqual(tas_corpus.evaluate(good, run, record), {"status": "pass", "value": 0xE3F635E3})
        wrong = tas_corpus.make_spot("m", "u16@0x80111D24 == 0xE3F6", 5, None, None, "")
        self.assertEqual(tas_corpus.evaluate(wrong, run, record)["status"], "fail")  # the low half on a LE port
        later = tas_corpus.make_spot("m", "u8@0x80111D24 & 0x1", 50, None, None, "")
        self.assertEqual(tas_corpus.evaluate(later, run, record), {"status": "unreached"})
        signed = tas_corpus.make_spot("m", "s8@0x80111D27 < 0", 5, None, None, "")
        self.assertEqual(tas_corpus.evaluate(signed, run, record)["status"], "pass")

    def test_function_spot_counts_from_gcov(self):
        run = self.make_run("m", functions={"f1": 3, "f2": 0})
        record = tas.read_record(run)
        check = lambda fn, expect: tas_corpus.evaluate(  # noqa: E731
            tas_corpus.make_spot("m", None, None, fn, expect, ""), run, record
        )["status"]
        self.assertEqual(check("f1", None), "pass")
        self.assertEqual(check("f2", "unreached"), "pass")
        self.assertEqual(check("f1", "4"), "fail")
        with self.assertRaises(DecompError):
            tas_corpus.make_spot("m", "u8@0x80000000 == 1", None, None, None, "")  # no --at
        with self.assertRaises(DecompError):
            tas_corpus.make_spot("m", "u8@0x80000000 == 1", 3, "f1", None, "")  # two checks

    def test_dump_polls_one_replay_per_ram_poll(self):
        spots = {
            "a": {"kind": "ram", "at": 30},
            "b": {"kind": "ram", "at": 10},
            "c": {"kind": "function"},
            "d": {"kind": "ram", "at": 30},
        }
        self.assertEqual(tas_corpus.dump_polls(spots), [10, 30])
        self.assertEqual(tas_corpus.dump_polls({"c": {"kind": "function"}}), [None])


class CorpusTests(Fixture):
    def test_features_are_functions_branches_and_a_crash_subject(self):
        run = self.make_run("m", functions={"f1": 1, "f2": 0}, branches=(1, 0, 2), outcome="crash")
        self.assertEqual(
            tas_corpus.features(run),
            {"f:f1", "b:src/game/A.CPP:7:0", "b:src/game/A.CPP:7:2", "o:crash:host-fault-00000010"},
        )

    def test_offer_keeps_only_new_coverage_from_plain_runs_on_the_profile_port(self):
        first = self.make_run("a", functions={"f1": 1})
        self.assertEqual(tas_corpus.offer(self.profile, self.cfg, first, tas.read_record(first))["kept"], "a")
        same = self.make_run("b", functions={"f1": 2})
        self.assertIsNone(tas_corpus.offer(self.profile, self.cfg, same, tas.read_record(same)))
        for kw in ({"label": tas_corpus.GATE_LABEL}, {"port": "/lane/build/ragewars"}):
            other = self.make_run("c", functions={"f9": 1}, **kw)
            self.assertIsNone(tas_corpus.offer(self.profile, self.cfg, other, tas.read_record(other)))
        new = self.make_run("d", functions={"f1": 1, "f2": 1}, branches=(1,))
        kept = tas_corpus.offer(self.profile, self.cfg, new, tas.read_record(new))
        self.assertEqual(kept, {"kept": "d", "why": "new coverage +1 fn +1 br"})
        entries = tas_corpus.load(self.cfg)["entries"]
        self.assertEqual(sorted(entries), ["a", "d"])
        self.assertEqual(entries["d"]["baseline"], new.name)

    def test_minimise_is_a_greedy_set_cover_that_keeps_pinned_entries(self):
        runs = {
            "big": self.make_run("big", functions={"f1": 1, "f2": 1, "f3": 1}, polls=900),
            "small": self.make_run("small", functions={"f1": 1}, polls=10),
            "twin": self.make_run("twin", functions={"f1": 1, "f2": 1, "f3": 1}, polls=100),
            "hand": self.make_run("hand", functions={"f1": 1}, polls=5),
        }
        entries = {m: {"why": ["seed"], "baseline": r.name} for m, r in runs.items()}
        entries["hand"]["why"] = ["hand"]
        dropped = tas_corpus.minimise(self.cfg, entries)
        self.assertEqual(sorted(entries), ["hand", "twin"])  # twin covers big's features in fewer polls
        self.assertEqual(dropped, ["big", "small"])

    def test_archive_keeps_what_the_corpus_names(self):
        old = self.make_run("a")
        newer = self.make_run("a")
        record = json.loads((old / tas.RECORD).read_text())
        (old / tas.RECORD).write_text(json.dumps({**record, "ended": 1}))
        tas_corpus.store(self.cfg, {"entries": {"a": {"why": ["seed"], "baseline": old.name}}, "spots": {}})
        self.assertEqual(tas.archive(self.cfg, 1, now=time.time()), [])
        tas_corpus.store(self.cfg, {"entries": {}, "spots": {}})
        self.assertEqual(tas.archive(self.cfg, 1, now=time.time()), [old.name])
        self.assertTrue(newer.is_dir())


class CompareTests(Fixture):
    def entry(self, base, **kw):
        return {"why": ["seed"], "baseline": base.name, **kw}

    def test_same_run_passes_and_a_ram_change_is_only_changed(self):
        base = self.make_run("m", hashes=["aa"] * 10)
        same = self.make_run("m", hashes=["aa"] * 10)
        self.assertEqual(tas_corpus.compare(self.cfg, self.entry(base), base, same, {})["verdict"], "pass")
        moved = self.make_run("m", hashes=["aa"] * 4 + ["bb"] * 6)
        row = tas_corpus.compare(self.cfg, self.entry(base), base, moved, {})
        self.assertEqual((row["verdict"], row["first_change"]), ("changed", 5))

    def test_worse_outcome_and_a_lost_spot_fail_better_ones_improve(self):
        base = self.make_run("m", reached=10)
        worse = self.make_run("m", outcome="crash", reached=6)
        row = tas_corpus.compare(self.cfg, self.entry(base), base, worse, {})
        self.assertEqual(row["verdict"], "fail")
        self.assertIn("outcome worse", row["reasons"][0])
        now = self.make_run("m")
        lost = tas_corpus.compare(self.cfg, self.entry(base, spots={"s": "pass"}), base, now, {"s": {"status": "fail"}})
        self.assertEqual(lost["verdict"], "fail")
        fixed = tas_corpus.compare(
            self.cfg, self.entry(base, spots={"s": "fail"}), base, now, {"s": {"status": "pass"}}
        )
        self.assertEqual(fixed["verdict"], "improved")
        open_bug = tas_corpus.compare(
            self.cfg, self.entry(base, spots={"s": "fail"}), base, now, {"s": {"status": "fail"}}
        )
        self.assertEqual(open_bug["verdict"], "pass")  # a spot for a still open bug never blocks

    def test_non_ok_end_fails_even_when_unchanged_improved_or_without_a_baseline(self):
        for outcome in ("crash", "hang", "vi-stall", "build", None):
            for reached in (6, 10):
                with self.subTest(outcome=outcome, reached=reached):
                    base = self.make_run("m", outcome=outcome, reached=6)
                    now = self.make_run("m", outcome=outcome, reached=reached)
                    for baseline in (base, None):
                        row = tas_corpus.compare(self.cfg, self.entry(base), baseline, now, {})
                        self.assertEqual(row["verdict"], "fail")
                        self.assertIn(f"outcome must be ok: {outcome} at poll {reached}", row["reasons"])

    def test_recovered_crash_improves_and_a_clean_first_run_is_new(self):
        base = self.make_run("m", outcome="crash", reached=6)
        now = self.make_run("m")
        self.assertEqual(tas_corpus.compare(self.cfg, self.entry(base), base, now, {})["verdict"], "improved")
        self.assertEqual(tas_corpus.compare(self.cfg, {}, None, now, {})["verdict"], "new")

    def test_a_function_that_stops_behaving_like_the_original_fails(self):
        reference = self.make_run("m")
        golden = reference / tas_coverage.ORACLE_DIR
        golden.mkdir()
        head = "order\tsymbol\tfirst_vi\tfirst_thread\tcount\n"
        (golden / "trace-poll-0.tsv").write_text(head)
        (golden / "trace-poll-25.tsv").write_text(head + "1\tf1\t0\t1\t4\n2\tf2\t0\t1\t9\n")
        base = self.make_run("m", functions={"f1": 4, "f2": 9})
        now = self.make_run("m", functions={"f1": 4, "f2": 30})
        entry = self.entry(base, oracle=reference.name)
        row = tas_corpus.compare(self.cfg, entry, base, now, {})
        self.assertEqual(row["verdict"], "fail")
        self.assertEqual(row["behaviour"]["lost"], ["f2"])
        back = tas_corpus.compare(self.cfg, self.entry(now, oracle=reference.name), now, base, {})
        self.assertEqual((back["verdict"], back["behaviour"]["won"]), ("improved", ["f2"]))


class BehaviourTests(unittest.TestCase):
    def test_count_brackets(self):
        bracket = ({"a": 2, "b": 5, "c": 1}, {"a": 4, "b": 5, "c": 1, "e": 3})
        port = {"a": 3, "b": 9, "c": 0, "d": 2, "e": 0, "z": 0}
        self.assertEqual(
            tas_coverage.behaviour(port, bracket),
            {"a": "behaves", "b": "differs", "c": "missing", "d": "port-only", "e": "behaves"},
        )
        self.assertEqual(tas_coverage.behaviour({"a": 7}, ({"a": 2}, None)), {"a": "behaves"})  # oracle ended earlier

    def test_bracket_reads_the_snapshots_around_the_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            golden = run / tas_coverage.ORACLE_DIR
            golden.mkdir()
            head = "order\tsymbol\tfirst_vi\tfirst_thread\tcount\n"
            for poll, count in ((25, 1), (50, 2), (75, 3)):
                (golden / f"trace-poll-{poll}.tsv").write_text(head + f"1\tf\t0\t1\t{count}\n")
            # the lower bound reaches one snapshot interval back: the port lags the oracle by up to one
            self.assertEqual(tas_coverage.oracle_bracket(run, 60), ({"f": 1}, {"f": 3}))
            self.assertEqual(tas_coverage.oracle_bracket(run, 50), ({"f": 1}, {"f": 2}))
            self.assertEqual(tas_coverage.oracle_bracket(run, 90), ({"f": 2}, None))
            self.assertEqual(tas_coverage.oracle_bracket(run, 10), ({}, {"f": 1}))
            self.assertEqual(tas_coverage.oracle_bracket(run, 30), ({}, {"f": 2}))
            self.assertIsNone(tas_coverage.oracle_bracket(run / "none", 10))


class GateTests(Fixture):
    """The gate end to end over a stand-in tas.play that writes run folders from a switchable world."""

    def setUp(self):
        super().setUp()
        self.movie("boot", 10)
        self.movie("menu", 20)
        self.broken = False
        self.plays = []

        def play(profile, movie, *, label=None, dump_ram_at=None, health=None, port=None, **kw):
            self.plays.append((movie, label, dump_ram_at, port))
            value = 0 if self.broken else 0xE3F635E3
            run = self.make_run(
                movie,
                functions={"f1": 1, movie: 1},
                hashes=["aa"] * 10 if not self.broken else ["aa"] * 3 + ["bb"] * 7,
                dump_at=dump_ram_at,
                ram={0x80111D24: value.to_bytes(4, "little")},
                label=label,
                polls=10 if movie == "boot" else 20,
            )
            return {"run": str(run), "id": run.name, "movie": movie}

        patch = mock.patch.object(tas_corpus.tas, "play", play)
        patch.start()
        self.addCleanup(patch.stop)

    def test_seed_spot_gate_green_then_a_break_fails_and_accept_moves_it(self):
        seeded = tas_corpus.seed(self.profile, jobs=1)
        self.assertEqual(seeded["kept"], ["boot", "menu"])
        spot = tas_corpus.add_spot(self.profile, "rng-seeded", "boot", ram="u32@D_80111D24 == 0xE3F635E3", at=5)
        self.assertEqual((spot["first"], spot["verdict"]["value"]), ("pass", 0xE3F635E3))
        self.assertFalse(list((self.cfg["runs"] / "plays").glob("*/ram-*.bin")))  # dumps go once read

        green, code = tas_corpus.gate(self.profile, jobs=2, oracle=False)
        self.assertEqual((green["verdict"], code), ("pass", 0))
        self.assertEqual(green["counts"], {"pass": 2})
        boot_plays = [p for p in self.plays if p[0] == "boot" and p[1] == tas_corpus.GATE_LABEL]
        self.assertEqual(boot_plays[-1][2], 5)  # the spot's poll is dumped
        baseline = tas_corpus.load(self.cfg)["entries"]["boot"]["baseline"]
        self.assertEqual(baseline, next(r for r in green["rows"] if r["movie"] == "boot")["run"])

        self.broken = True
        red, code = tas_corpus.gate(self.profile, jobs=2, oracle=False)
        self.assertEqual((red["verdict"], code, red["failed"]), ("fail", 1, ["boot"]))
        row = next(r for r in red["rows"] if r["movie"] == "boot")
        self.assertIn("spot rng-seeded: pass -> fail (value 0)", row["reasons"])
        self.assertEqual(row["first_change"], 4)
        self.assertEqual(tas_corpus.load(self.cfg)["entries"]["boot"]["baseline"], baseline)  # a fail never moves it
        self.assertIn("decomp tas corpus accept boot", tas_corpus.render(red))
        self.assertEqual(tas_corpus.view(self.cfg)["gate"]["failed"][0]["movie"], "boot")

        tas_corpus.accept(self.profile, [])
        self.assertEqual(tas_corpus.load(self.cfg)["entries"]["boot"]["baseline"], row["run"])
        self.assertEqual(tas_corpus.read_gate(self.cfg)["verdict"], "pass")
        again, code = tas_corpus.gate(self.profile, jobs=2, oracle=False)
        self.assertEqual(code, 0)
        self.assertEqual(next(r for r in again["rows"] if r["movie"] == "boot")["spots"], {"rng-seeded": "fail"})

    def test_unchanged_crash_or_hang_fails_gate_and_cannot_be_accepted(self):
        for outcome in ("crash", "hang"):
            with self.subTest(outcome=outcome):
                base = self.make_run("boot", outcome=outcome)
                now = self.make_run("boot", outcome=outcome)
                entry = {"why": ["hand"], "baseline": base.name}
                tas_corpus.store(self.cfg, {"entries": {"boot": entry}, "spots": {}})
                with mock.patch.object(tas_corpus.tas, "play", return_value={"run": str(now)}):
                    result, code = tas_corpus.gate(self.profile, jobs=1, oracle=False)
                self.assertEqual((result["verdict"], code, result["failed"]), ("fail", 1, ["boot"]))
                self.assertEqual(result["moved"], [])
                before = tas_corpus.load(self.cfg)
                self.assertEqual(before["entries"]["boot"]["baseline"], base.name)
                self.assertNotIn("corpus accept", tas_corpus.render(result))
                for movies in ([], ["boot"]):
                    with self.assertRaisesRegex(DecompError, "cannot accept non-ok outcomes"):
                        tas_corpus.accept(self.profile, movies)
                self.assertEqual(tas_corpus.load(self.cfg), before)
                self.assertEqual(tas_corpus.read_gate(self.cfg)["verdict"], "fail")

    def test_a_lane_port_gate_moves_nothing_and_writes_no_gate_file(self):
        tas_corpus.seed(self.profile, jobs=1)
        before = tas_corpus.load(self.cfg)
        lane = self.root / "lane-port"
        lane.write_text("")
        lane.chmod(0o755)
        self.broken = True
        result, _ = tas_corpus.gate(self.profile, port=lane, jobs=1, oracle=False)
        self.assertFalse(result["main"])
        self.assertEqual(tas_corpus.load(self.cfg), before)
        self.assertIsNone(tas_corpus.read_gate(self.cfg))

    def test_gate_is_due_on_a_new_port_build_only(self):
        self.assertFalse(tas_corpus.gate_due(self.cfg))  # empty corpus
        tas_corpus.seed(self.profile, jobs=1)
        self.assertTrue(tas_corpus.gate_due(self.cfg))
        tas_corpus.gate(self.profile, jobs=1, oracle=False)
        self.assertFalse(tas_corpus.gate_due(self.cfg))
        (self.root / "port").write_text("rebuilt")
        self.assertTrue(tas_corpus.gate_due(self.cfg))

    def test_drop_refuses_a_movie_with_spots(self):
        tas_corpus.seed(self.profile, jobs=1)
        tas_corpus.add_spot(self.profile, "menu-reached", "menu", function="menu")
        with self.assertRaises(DecompError):
            tas_corpus.drop(self.profile, "menu")
        tas_corpus.drop_spot(self.profile, "menu-reached")
        self.assertEqual(tas_corpus.drop(self.profile, "menu"), {"dropped": "menu"})


if __name__ == "__main__":
    unittest.main()
