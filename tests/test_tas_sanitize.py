import hashlib
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from abu_decomp import DecompError, tas, tas_sanitize
from abu_decomp.config import load_profile

T0 = datetime(2026, 10, 22, 22, 0, tzinfo=UTC)
UBSAN = """boot noise
/home/abu/deps/rage_decomp_source-abc123/src/game/foo.c:123:17: runtime error: shift exponent 40 is too large
    #0 0x8123456 in func_80123456_de ragewars-decomp/src/game/foo.c:123
    #1 0x8124000 in func_80124000_de ragewars-decomp/src/game/bar.c:77
SUMMARY: UndefinedBehaviorSanitizer: undefined-behavior ragewars-decomp/src/game/foo.c:123:17 in
n64c-san: poll=412
game printed a line
"""
ASAN = """=================================================================
==4242==ERROR: AddressSanitizer: heap-buffer-overflow on address 0xf5a00054 at pc 0x08123456 bp 0xffb2 sp 0xffb2
WRITE of size 4 at 0xf5a00054 thread T0
    #0 0xf7a12345 in __interceptor_memcpy ../../../../src/libsanitizer/asan/asan_interceptors.cpp:827
    #1 0x08123456 in func_8020_de ragewars-decomp/src/game/baz.c:55
    #2 0x08124000 in func_8021_de ragewars-decomp/src/game/baz.c:90

0xf5a00054 is located 4 bytes after 64-byte region [0xf5a00010,0xf5a00050)
allocated by thread T0 here:
    #0 0xf7a99999 in malloc ../../../../src/libsanitizer/asan/asan_malloc_linux.cpp:69
    #1 0x08130000 in n64c_heap_alloc shim/n64c_alheap.c:40

Shadow bytes around the buggy address:
  0x3eb40000: fa fa fa fa
==4242==ABORTING
SUMMARY: AddressSanitizer: heap-buffer-overflow ragewars-decomp/src/game/baz.c:55 in func_8020_de
n64c-san: poll=900
"""


class ParseTests(unittest.TestCase):
    def test_a_ubsan_report_has_its_site_function_type_and_poll(self):
        (block,) = tas_sanitize.blocks(UBSAN)
        row = tas_sanitize.finding(block)
        self.assertEqual(
            (row["kind"], row["type"], row["site"], row["function"], row["poll"]),
            ("ubsan", "shift", "src/game/foo.c:123", "func_80123456_de", 412),
        )
        self.assertEqual(row["signature"], "ubsan:shift:src/game/foo.c:123")
        self.assertNotIn("game printed a line", "\n".join(block["text"]))

    def test_an_asan_report_skips_the_runtime_frames_and_reads_the_summary_poll(self):
        (block,) = tas_sanitize.blocks(ASAN)
        row = tas_sanitize.finding(block)
        self.assertEqual(
            (row["kind"], row["type"], row["site"], row["function"], row["poll"]),
            ("asan", "heap-buffer-overflow", "src/game/baz.c:55", "func_8020_de", 900),
        )

    def test_a_load_past_its_object_is_object_size(self):
        text = UBSAN.replace(
            "shift exponent 40 is too large",
            "load of address 0x800e5968 with insufficient space for an object of type 'char'",
        )
        self.assertEqual(tas_sanitize.finding(tas_sanitize.blocks(text)[0])["type"], "object-size")

    def test_two_reports_in_one_output_and_a_report_without_a_tag(self):
        blocks = tas_sanitize.blocks(UBSAN + ASAN)
        self.assertEqual([b["kind"] for b in blocks], ["ubsan", "asan"])
        untagged = tas_sanitize.blocks(UBSAN.replace("n64c-san: poll=412\n", "") + ASAN)
        self.assertEqual([b["poll"] for b in untagged], [None, 900])

    def test_output_without_a_report_has_none(self):
        self.assertEqual(tas_sanitize.blocks("n64c-tas: done polls=9\nwarning: foo.c:3:4: not a runtime error\n"), [])
        self.assertFalse(tas_sanitize.present("n64c-tas: done polls=9\n"))
        self.assertTrue(tas_sanitize.present(UBSAN))

    def test_paths_are_relative_to_the_repo(self):
        for path, want in (
            ("/x/ragewars-decomp/src/a/b.c", "src/a/b.c"),
            ("/x/rage_decomp_source-0f/src/a/b.c", "src/a/b.c"),
            ("/x/deps/abu_shim-0f/shim/n64c_tas.c", "shim/n64c_tas.c"),
            ("/elsewhere/dir/file.c", "dir/file.c"),
        ):
            self.assertEqual(tas_sanitize.relative(path), want)


class MonkeyInputTests(unittest.TestCase):
    def test_a_seed_names_one_input_and_seeds_differ(self):
        a = tas_sanitize.random_polls(7, 500)
        self.assertEqual(a, tas_sanitize.random_polls(7, 500))
        self.assertNotEqual(a, tas_sanitize.random_polls(8, 500))
        self.assertEqual(len(a), 500)
        self.assertEqual(tas_sanitize.random_polls(7, 100), a[:100])  # a longer run extends the shorter

    def test_polls_stay_inside_the_controller(self):
        polls = tas_sanitize.random_polls(3, 4000, hold=10)
        self.assertTrue(all(-80 <= x <= 80 and -80 <= y <= 80 for _, x, y in polls))
        mask = sum(bit for bit, _ in tas_sanitize._PRESS)
        self.assertTrue(all(b & ~mask == 0 for b, _, _ in polls))
        self.assertGreater(len({b for b, _, _ in polls}), 10)
        longest = max(len(list(g)) for _, g in __import__("itertools").groupby(polls))
        self.assertGreater(longest, 1)

    def test_the_generator_is_splitmix64(self):
        rng = tas_sanitize.Splitmix(0)
        self.assertEqual([rng.next() for _ in range(2)], [0xE220A8397B1DCDAF, 0x6E789E6AA1B965F4])

    def test_seed_lists_and_limits(self):
        self.assertEqual(tas_sanitize.seeds_of("1,4,9-11"), [1, 4, 9, 10, 11])
        for bad in ("a", "5-2", "1-99999"):
            with self.assertRaises(DecompError):
                tas_sanitize.seeds_of(bad)
        with self.assertRaises(DecompError):
            tas_sanitize.check_args(1, 0, 24)


class SanitizerRunTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "game.z64").write_bytes(b"rom")
        (self.root / "port").write_text("")
        (self.root / "san").write_text("")
        (self.root / "san").chmod(0o755)
        (self.root / "port").chmod(0o755)
        seat = self.root / "seat.json"
        table = {
            "movies": "movies",
            "runs": "runs",
            "port": "port",
            "rom": "game.z64",
            "args": ["--user-dir", "{user_dir}"],
            "exit_after": 7,
            "timeout_seconds": 5,
            "sanitizer": {"port": "san"},
        }
        seat.write_text(json.dumps({"schema_version": 1, "name": "t", "tas": table}))
        self.profile = load_profile(seat)
        self.cfg = tas.settings(self.profile)
        sha = hashlib.sha1(b"rom").hexdigest()
        (self.root / "movies").mkdir()
        (self.root / "movies" / "m.tas").write_text(f"# tas 1\n# rom-sha1 {sha}\nA *2\n")
        patch = mock.patch.object(tas, "_wait", lambda process, timeout, run=None: (process.wait(timeout), 1.5))
        patch.start()
        self.addCleanup(patch.stop)

    def port(self, text, seen):
        class Port:
            pid = 1
            stdin = io.StringIO()

            def __init__(self, argv, env, stdout, **kwargs):
                seen.append((argv, env))
                Path(env["N64C_TAS_RECORD"]).write_text("8000 0 0\n8000 0 0\n")
                Path(env["N64C_TAS_HASH"]).write_text("1 aa\n2 bb\n")
                stdout.write(text + "n64c-tas: done polls=2\n")

            def wait(self, timeout=None):
                return 0

        return mock.patch.object(tas.subprocess, "Popen", Port)

    def test_the_profile_names_the_sanitizer_port_and_the_play_uses_it_with_recoverable_options(self):
        self.assertEqual(self.cfg["sanitizer_port"], (self.root / "san").resolve())
        seen = []
        with self.port(UBSAN + ASAN, seen):
            result = tas.play(self.profile, "m", now=T0, sanitize=True)
        argv, env = seen[0]
        self.assertIn(str((self.root / "san").resolve()), argv)
        self.assertIn("halt_on_error=0", env["ASAN_OPTIONS"])
        self.assertIn("halt_on_error=0", env["UBSAN_OPTIONS"])
        self.assertEqual((result["outcome"], result["sanitizer"]["findings"]), ("ok", 2))
        record = json.loads((Path(result["run"]) / "run.json").read_text())
        self.assertEqual(
            (record["label"], record["sanitizer"]["asan"], record["sanitizer"]["ubsan"]), ("sanitizer", 1, 1)
        )

    def test_each_report_is_a_findings_line_with_a_report_file(self):
        with self.port(UBSAN + ASAN + UBSAN, []):
            result = tas.play(self.profile, "m", now=T0, sanitize=True)
        rows = tas_sanitize.read(self.cfg)
        self.assertEqual([r["kind"] for r in rows], ["ubsan", "asan"])
        ub = rows[0]
        self.assertEqual((ub["movie"], ub["poll"], ub["site"], ub["count"]), ("m", 412, "src/game/foo.c:123", 2))
        self.assertEqual(ub["run"], Path(result["run"]).name)
        self.assertIn("runtime error: shift exponent", Path(ub["report"]).read_text())
        self.assertEqual(ub["evidence"], ub["report"])
        self.assertEqual(result["sanitizer"]["reports"], 3)

    def test_a_clean_run_adds_no_line_and_a_plain_run_has_no_sanitizer_record(self):
        with self.port("", []):
            self.assertEqual(tas.play(self.profile, "m", now=T0, sanitize=True)["sanitizer"]["findings"], 0)
        with self.port("plain output\n", []):
            self.assertNotIn("sanitizer", tas.play(self.profile, "m", now=T0))
        self.assertEqual(tas_sanitize.read(self.cfg), [])  # a plain run only clears its movie's findings

    def test_a_report_in_a_port_given_by_port_is_still_filed(self):
        with self.port(UBSAN, []):
            result = tas.play(self.profile, "m", now=T0, port=self.root / "san")
        self.assertEqual(result["sanitizer"]["findings"], 1)

    def test_no_sanitizer_port_is_refused_by_name(self):
        seat = self.root / "seat2.json"
        table = json.loads((self.root / "seat.json").read_text())
        del table["tas"]["sanitizer"]
        seat.write_text(json.dumps(table))
        with self.assertRaises(DecompError) as caught:
            tas.play(load_profile(seat), "m", sanitize=True)
        self.assertIn("tas.sanitizer.port", str(caught.exception))

    def test_grouping_counts_reports_and_movies_per_site(self):
        with self.port(UBSAN + ASAN, []):
            tas.play(self.profile, "m", now=T0, sanitize=True)
        with self.port(UBSAN, []):
            tas.play(self.profile, "m", now=T0, sanitize=True)
        groups = tas_sanitize.grouped(tas_sanitize.read(self.cfg))
        first = groups[0]
        self.assertEqual((first["site"], first["reports"], first["movies"]), ("src/game/foo.c:123", 2, ["m"]))
        self.assertEqual(len(groups), 2)

    def test_monkey_keeps_only_the_movies_that_find_something(self):
        hit = {"hit": False}

        def play(profile, name, **kwargs):
            clean = name.endswith("-2-30")
            return {
                "run": "r",
                "outcome": "ok",
                "polls_reached": 1,
                "polls": 1,
                "sanitizer": {"findings": 0 if clean else 1, "reports": 1},
            }

        with mock.patch.object(tas, "play", play):
            done = tas_sanitize.monkey(self.profile, [1, 2], polls=30, prefix="m", jobs=1)
        self.assertEqual(
            [(r["movie"], r["kept"]) for r in done["movies"]], [("monkey-m-1-30", True), ("monkey-m-2-30", False)]
        )
        self.assertTrue((self.root / "movies" / "monkey-m-1-30.tas").is_file())
        self.assertFalse((self.root / "movies" / "monkey-m-2-30.tas").exists())
        header, polls = tas.decode((self.root / "movies" / "monkey-m-1-30.tas").read_text())
        self.assertEqual(len(polls), 2 + 30)  # the prefix movie's two polls, then 30 random ones
        self.assertEqual(polls[2:], tas_sanitize.random_polls(1, 30))
        self.assertIn("monkey seed=1", header["note"])
        self.assertFalse(hit["hit"])

    def test_monkey_movie_is_the_same_for_the_same_seed(self):
        movies = []

        def play(profile, name, **kwargs):
            movies.append((self.root / "movies" / f"{name}.tas").read_text().split("\n", 3)[3])
            return {"run": "r", "outcome": "ok", "sanitizer": {"findings": 0}}

        with mock.patch.object(tas, "play", play):
            tas_sanitize.monkey(self.profile, [5, 5], polls=40, jobs=1, keep="all")
        self.assertEqual(movies[0], movies[1])


if __name__ == "__main__":
    unittest.main()
