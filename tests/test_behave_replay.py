"""`decomp behave --replay` (behave/replay.py): samples, native argument words, the plan, the comparison and the batch
with a fake oracle host and a fake port executable (no recompiled game runs)."""

import hashlib
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp import DecompError, tas, tas_coverage
from abu_decomp.behave import prototype, replay
from abu_decomp.config import load_profile


def sample(fn="f", movie="m", index=0, args=(0, 0, 0, 0), reads=(), stores=(), ret=(0, 0), **more):
    base = {
        "movie": movie,
        "pak": "on",
        "function": fn,
        "index": index,
        "frame": 10,
        "sp": 0x80010000,
        "args": [*args, 0, 0, 0, 0][:4],
        "f12": 0,
        "f14": 0,
        "stack": [0] * 8,
        "reads": [list(r) for r in reads],
        "stores": [list(s) for s in stores],
        "ret": list(ret),
        "f0": 0,
    }
    return replay.Sample(**{**base, **more})


def proto(text, name="f"):
    return prototype.parse_declaration(text, name, {"Obj": "struct"}, "t.h")


class SamplesTests(unittest.TestCase):
    def test_reading_counts_skips_and_functions_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "replay.jsonl"
            ok = {
                "fn": "f",
                "i": 0,
                "frame": 3,
                "thr": 1,
                "depth": 0,
                "sp": 5,
                "a": [1, 2, 3, 4],
                "f12": 0,
                "f14": 0,
                "stack": [0] * 8,
                "in": [[0x80001000, 4, 9]],
                "out": [],
                "ret": [1, 0],
                "f0": 0,
                "events": 1,
            }
            path.write_text(
                "\n".join([json.dumps(ok), json.dumps({"fn": "g", "i": 0, "skip": "interleaved"}), "not json"])
            )
            samples, skipped, seen = replay.read_samples(path, "m", "on")
        self.assertEqual([s.function for s in samples], ["f"])
        self.assertEqual(dict(skipped), {"interleaved": 1})
        self.assertEqual(seen, {"f", "g"})

    def test_pick_keeps_a_few_different_calls_taking_movies_in_turn(self):
        a = [sample(movie="a", index=i, args=(i,)) for i in range(4)]
        b = [sample(movie="b", index=0, args=(9,)), sample(movie="b", index=1, args=(0,))]  # (0,) repeats a's first
        chosen = replay.pick(a + b, 3)
        self.assertEqual([(s.movie, s.index) for s in chosen["f"]], [("a", 0), ("b", 0), ("a", 1)])
        self.assertEqual(len(replay.pick([sample(args=(1,)), sample(args=(1,), index=5)], 3)["f"]), 1)


class PointerTests(unittest.TestCase):
    def test_a_function_is_known_by_its_kseg0_and_its_physical_address(self):
        found = replay.function_pointers(
            {"f": 0x80206018, "d": 0x1234, "e": 0x80000400, "g": 0x80300000},
            {"f": 0x08100000, "d": 0x08200000, "e": 0x08300000},
        )
        # a boot function's physical form, 0x400, is more likely an integer
        self.assertEqual(found, {0x80206018: 0x08100000, 0x00206018: 0x08100000, 0x80000400: 0x08300000})


class PrototypeIndexTests(unittest.TestCase):
    def test_an_empty_old_style_declaration_does_not_hide_the_definition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "a.c").write_text("s32 f();\n")
            (root / "src" / "b.c").write_text("void f(int *p, int n) {\n}\n")
            index = replay.Prototypes(root, ("src/*.c",))
            self.assertEqual([p.name for p in index.get("f").params], ["p", "n"])
            self.assertFalse(index.get("missing").known)


class ArgumentTests(unittest.TestCase):
    def words(self, text, **more):
        return replay.native_words(proto(text), sample(**more), {0x80257BB4: 0x080A0000})

    def test_integers_pointers_and_function_pointers(self):
        words, blind = self.words(
            "int f(int a, Obj *p, void (*cb)(void), s16 d);", args=(1, 0x80001000, 0x80257BB4, 0xFFFF)
        )
        self.assertEqual(words, [1, 0x80001000, 0x080A0000, 0xFFFF])
        self.assertFalse(blind)

    def test_leading_floats_come_from_the_fprs_and_later_ones_from_the_registers(self):
        words, _ = self.words(
            "void f(float x, float y, int n, float z);", args=(0, 0, 7, 0x3F800000), f12=0x3F000000, f14=0x40000000
        )
        self.assertEqual(words, [0x3F000000, 0x40000000, 7, 0x3F800000])

    def test_a_double_is_two_native_words_low_first(self):
        words, _ = self.words("void f(double x);", f12=0x3FF8000000000000)
        self.assertEqual(words, [0, 0x3FF80000])
        # after an integer it sits in a register pair, high word first, and starts on an even register
        words, _ = self.words("void f(int n, double x);", args=(5, 0, 0x3FF80000, 0))
        self.assertEqual(words, [5, 0, 0x3FF80000])

    def test_a_64_bit_integer_and_stack_arguments(self):
        words, _ = self.words("void f(s64 v, int a, int b);", args=(1, 2, 3, 4), stack=[9, 0, 0, 0, 0, 0, 0, 0])
        self.assertEqual(words, [2, 1, 3, 4])
        words, _ = self.words(
            "void f(int a, int b, int c, int d, int e);", args=(1, 2, 3, 4), stack=[77, 0, 0, 0, 0, 0, 0, 0]
        )
        self.assertEqual(words, [1, 2, 3, 4, 77])

    def test_unknown_prototype_is_four_blind_words(self):
        words, blind = replay.native_words(prototype.inferred("f"), sample(args=(1, 2, 3, 4)), {})
        self.assertEqual(words, [1, 2, 3, 4])
        self.assertTrue(blind)


class PlanTests(unittest.TestCase):
    def test_inputs_are_native_values_and_the_dead_frame_is_not_compared(self):
        native_of = {0x80257BB4: 0x080A0000}
        s = sample(
            args=(0x80001000,),
            reads=[(0x80001000, 4, 0x80257BB4), (0x80001004, 1, 0x7F), (0x80001008, 8, 0x3FF8000000000000)],
            stores=[(0x80001010, 2, 0x1234), (0x8000FF00, 4, 99), (0x80010004, 4, 77), (0x80010010, 4, 5)],
        )
        case = replay.make_case(3, s, proto("void f(int *p);"), 0x80000, native_of)
        self.assertEqual(case.id, "c3")
        self.assertEqual(case.reads, [(0x1000, 4, 0x080A0000), (0x1004, 1, 0x7F), (0x1008, 8, 0x3FF8000000000000)])
        # 0x8000FF00 is under the entry sp 0x80010000 within the window: the call's own frame; 0x80010004 is a home
        # word of an argument register the original spilled; 0x80010010 is above them
        self.assertEqual(case.wants, [(0x1010, 2, 0x1234), (0x10010, 4, 5)])
        text = replay.plan_text([case], Path("/r/out.txt"))
        self.assertEqual(
            text.splitlines(),
            [
                "out /r/out.txt",
                "timeout 5",
                "case c3",
                "fn 80000 v",
                "args 80001000",
                "in 1000 4 80a0000",
                "in 1004 1 7f",
                "in 1008 8 3ff8000000000000",
                "want 1010 2",
                "want 10010 4",
                "go",
            ],
        )

    def test_results_parse(self):
        got = replay.parse_results(
            "res c0 ok 5 0 0\ngot 1010 2 1234\nres c1 crash 11 0 80a0044\nres c2 timeout\nres c3 exit 78\n"
        )
        self.assertEqual((got["c0"].status, got["c0"].eax, got["c0"].got), ("ok", 5, {(0x1010, 2): 0x1234}))
        self.assertEqual((got["c1"].status, got["c1"].detail), ("crash", ["11", "0", "80a0044"]))
        self.assertEqual((got["c2"].status, got["c3"].detail), ("timeout", ["78"]))


class CompareTests(unittest.TestCase):
    def case(self, text, **more):
        return replay.make_case(0, sample(**more), proto(text), 0x80000, {0x80257BB4: 0x080A0000})

    def test_same_and_differing_returns(self):
        c = self.case("int f(int a);", ret=(7, 0))
        self.assertEqual(replay.compare_case(c, replay.Outcome("ok", eax=7), {}), [])
        found = replay.compare_case(c, replay.Outcome("ok", eax=8), {})
        self.assertEqual([(m.field, m.port, m.oracle) for m in found], [("return", "0x00000008", "0x00000007")])

    def test_narrow_returns_ignore_the_upper_bits_and_void_returns_are_not_compared(self):
        c = self.case("u8 f(void);", ret=(0x7F, 0))
        self.assertEqual(replay.compare_case(c, replay.Outcome("ok", eax=0xABCD007F), {}), [])
        c = self.case("void f(void);", ret=(1, 0))
        self.assertEqual(replay.compare_case(c, replay.Outcome("ok", eax=99), {}), [])
        c = self.case("MysteryType f(void);", ret=(1, 0))
        self.assertEqual(replay.compare_case(c, replay.Outcome("ok", eax=99), {}), [])

    def test_a_function_pointer_result_is_the_native_address(self):
        c = self.case("void *f(void);", ret=(0x80257BB4, 0))
        self.assertEqual(replay.compare_case(c, replay.Outcome("ok", eax=0x080A0000), {0x80257BB4: 0x080A0000}), [])

    def test_floats_are_rounding_inside_a_few_ulps_and_a_wrong_value_otherwise(self):
        one = 0x3F800000
        c = self.case("float f(void);", f0=one)
        self.assertEqual(replay.compare_case(c, replay.Outcome("ok", bits=one + 3), {}), [])
        self.assertEqual(len(replay.compare_case(c, replay.Outcome("ok", bits=one + 9), {})), 1)
        c = self.case("double f(void);", f0=0x3FF0000000000000)
        self.assertEqual(replay.compare_case(c, replay.Outcome("ok", bits=0x3FF0000000000002), {}), [])
        self.assertEqual(len(replay.compare_case(c, replay.Outcome("ok", bits=0x4000000000000000), {})), 1)

    def test_a_small_integer_in_memory_is_not_mistaken_for_a_float(self):
        c = self.case("void f(int *p);", stores=[(0x80001010, 4, 5)])
        found = replay.compare_case(c, replay.Outcome("ok", got={(0x1010, 4): 6}), {})
        self.assertEqual(
            [(m.field, m.address, m.port, m.oracle) for m in found],
            [("memory", 0x80001010, "0x00000006", "0x00000005")],
        )
        self.assertEqual(replay.compare_case(c, replay.Outcome("ok", got={(0x1010, 4): 5}), {}), [])

    def test_a_64_bit_result_is_the_register_pair(self):
        c = self.case("s64 f(void);", ret=(1, 2))
        self.assertEqual(replay.compare_case(c, replay.Outcome("ok", eax=2, edx=1), {}), [])
        self.assertEqual(len(replay.compare_case(c, replay.Outcome("ok", eax=1, edx=2), {})), 1)

    def test_judging_faults_by_where_they_happened(self):
        elf = mock.Mock()
        elf.holding = lambda address: ("func_game", 4) if address == 0x80A0004 else ("n64c_sched_wait", 9)
        c = self.case("int f(void);")
        verdict, found, _ = replay.judge(c, replay.Outcome("crash", ["11", "0", "80a0004"]), elf, {"func_game"}, {})
        self.assertEqual((verdict, found[0].field, found[0].port), ("differs", "crash", "SIGSEGV at func_game+0x4"))
        verdict, found, why = replay.judge(c, replay.Outcome("crash", ["11", "0", "1234"]), elf, {"func_game"}, {})
        self.assertEqual((verdict, found), ("untestable", []))
        self.assertIn("n64c_sched_wait", why)
        self.assertEqual(
            replay.judge(c, replay.Outcome("exit", ["78"]), elf, set(), {})[2], "stand-in for assembly (boot-missing)"
        )
        self.assertEqual(replay.judge(c, replay.Outcome("timeout"), elf, set(), {})[0], "untestable")
        self.assertEqual(replay.judge(c, None, elf, set(), {})[0], "untestable")


class FindingsTests(unittest.TestCase):
    def test_a_rerun_replaces_the_functions_lines_and_keeps_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "findings.jsonl"
            path.write_text(
                json.dumps({"kind": "crash", "function": "f"})
                + "\n"
                + json.dumps({"kind": "fn-differs", "function": "f", "field": "old"})
                + "\n"
                + json.dumps({"kind": "fn-differs", "function": "other"})
                + "\n"
            )
            replay.write_findings(path, [{"kind": "fn-differs", "function": "f", "field": "new"}], {"f"})
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(
                rows,
                [
                    {"kind": "crash", "function": "f"},
                    {"kind": "fn-differs", "function": "other"},
                    {"field": "new", "function": "f", "kind": "fn-differs"},
                ],
            )
            replay.write_findings(path, [{"kind": "fn-differs", "function": "g"}], {"g"})
            self.assertEqual(len(path.read_text().splitlines()), 4)  # nothing to replace: appended


FAKE_ORACLE = """#!{python}
import json, os, pathlib
out = pathlib.Path(os.environ["N64R_REPLAY_DIR"]); out.mkdir(parents=True, exist_ok=True)
rows = json.loads(pathlib.Path(__file__).with_name("canned.json").read_text())
(out / "replay.jsonl").write_text("".join(json.dumps(r) + "\\n" for r in rows))
(pathlib.Path(__file__).with_name("calls.txt")).open("a").write(os.environ["N64R_REPLAY_CALLS"] + "\\n")
"""

FAKE_PORT = """#!{python}
import json, os, pathlib
behaviour = json.loads(pathlib.Path(__file__).with_name("behaviour.json").read_text())
cases, case = [], None
plan = pathlib.Path(os.environ["N64C_REPLAY"]).read_text().splitlines()
out = None
for line in plan:
    word, _, rest = line.partition(" ")
    if word == "out": out = open(rest, "a")
    elif word == "case": case = {{"id": rest, "wants": [], "ins": {{}}}}
    elif word == "fn": case["entry"] = rest.split()[0]
    elif word == "in": a, w, v = rest.split(); case["ins"][a] = v
    elif word == "want": a, w = rest.split(); case["wants"].append((a, w))
    elif word == "go":
        b = behaviour[case["entry"]]
        if b["status"] == "ok":
            out.write("res %s ok %x 0 0\\n" % (case["id"], b["eax"]))
            for a, w in case["wants"]:
                out.write("got %s %s %x\\n" % (a, w, b["got"].get(a, 0)))
        else:
            out.write("res %s %s\\n" % (case["id"], b["status"]))
        out.flush()
"""


class BatchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = root = Path(directory.name)
        rom = root / "game.z64"
        rom.write_bytes(b"rom")
        (root / "movies").mkdir()
        sha = hashlib.sha1(b"rom").hexdigest()
        (root / "movies" / "m.tas").write_text(f"# tas 1\n# rom-sha1 {sha}\nA *2\n")
        for name, body in (("oracle", FAKE_ORACLE), ("port", FAKE_PORT)):
            (root / name).mkdir()
            script = root / name / "run"
            script.write_text(body.format(python=sys.executable))
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
        src = root / "src"
        (src / "versions" / "us-rev1").mkdir(parents=True)
        (src / "src").mkdir()
        (src / "config.toml").write_text('names_from = "us-rev1"\n')
        names = ["fa", "fb", "fc", "fd", "fe", "ff", "fg"]
        (src / "versions" / "us-rev1" / "symbol_addrs.txt").write_text(
            "".join(f"{n} = 0x{0x80200000 + 0x100 * i:08X}; // type:func\n" for i, n in enumerate(names))
        )
        (src / "src" / "game.c").write_text(
            "int fa(int a, int *p) {\n}\nint fb(int a) {\n}\nvoid fc(int *p) {\n}\nint fd(int a) {\n}\n"
            "int fe(int a) {\n}\nint ff(int a) {\n}\nint fg(int a) {\n}\n"
        )
        seat = root / "seat.json"
        seat.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "t",
                    "tas": {
                        "movies": "movies",
                        "runs": "runs",
                        "port": "port/run",
                        "rom": "game.z64",
                        "args": ["--user-dir", "{user_dir}"],
                        "exit_after": 7,
                        "timeout_seconds": 5,
                        "oracle": {"host": "oracle/run", "env": {}},
                        "serve": {"listen": "x", "jobs": 1, "inbox": "i", "states": "s", "pin": "src", "lanes": "l"},
                    },
                    "behave": {"work": "work"},
                }
            )
        )
        self.profile = load_profile(seat)
        ok = lambda fn, a, reads, stores, ret: {  # noqa: E731
            "fn": fn, "i": 0, "frame": 5, "thr": 1, "depth": 0, "sp": 0x80010000, "a": a, "f12": 0, "f14": 0,
            "stack": [0] * 8, "in": reads, "out": stores, "ret": [ret, 0], "f0": 0, "events": 3,
        }  # fmt: skip
        self.canned = [
            ok("fa", [5, 0x80001000, 0, 0], [[0x80001000, 4, 7]], [[0x80001000, 4, 8]], 12),
            ok("fb", [1, 0, 0, 0], [], [], 3),
            ok("fc", [0x80001000, 0, 0, 0], [], [[0x80001000, 4, 1]], 0),
            ok("fd", [1, 0, 0, 0], [], [], 1),
            ok("fe", [1, 0, 0, 0], [], [], 1),
            ok("ff", [1, 0, 0, 0], [], [], 1),
            {"fn": "fg", "i": 0, "skip": "interleaved"},
        ]
        (root / "oracle" / "canned.json").write_text(json.dumps(self.canned))
        self.symbols = {  # the port's functions: name -> (address, size); fe is not linked
            "fa": (0x08100000, 64),
            "fb": (0x08100100, 64),
            "fc": (0x08100200, 64),
            "fd": (0x08100300, 64),
            "ff": (0x08100500, 64),
            "n64c_wait": (0x08200000, 64),
        }
        behaviour = {
            f"{0x08100000:x}": {"status": "ok", "eax": 12, "got": {"1000": 8}},  # fa: the same
            f"{0x08100100:x}": {"status": "ok", "eax": 4, "got": {}},  # fb: another return value
            f"{0x08100200:x}": {"status": "ok", "eax": 0, "got": {"1000": 2}},  # fc: another store
            f"{0x08100300:x}": {"status": "crash 11 0 8100304", "eax": 0, "got": {}},  # fd: faults in itself
            f"{0x08100500:x}": {"status": "exit 78", "eax": 0, "got": {}},  # ff: stands in for assembly
        }
        (root / "port" / "behaviour.json").write_text(json.dumps(behaviour))

    def run_batch(self, **more):
        patcher = mock.patch.object(tas_coverage, "elf_functions", lambda path: dict(self.symbols))
        patcher.start()
        self.addCleanup(patcher.stop)
        waits = mock.patch.object(tas, "_wait", lambda process, timeout, run=None: (process.wait(), 0.5))
        waits.start()
        self.addCleanup(waits.stop)
        return replay.run(self.profile, replay.Options(movies=["m"], **more))

    def test_batch_records_replays_and_writes_the_findings(self):
        result, code, findings = self.run_batch()
        self.assertEqual((code, findings), (0, []))
        counts = result["functions"]
        self.assertEqual(
            (counts["reached"], counts["sampled"], counts["replayed"], counts["not_in_port"]), (7, 6, 5, 1)
        )  # fg was written off by the recorder, fe is not in the port
        self.assertEqual((counts["same"], counts["differs"], counts["untestable"]), (1, 3, 1))
        self.assertEqual(result["written_off"], {"interleaved": 1})
        self.assertEqual(result["untestable_reasons"], {"stand-in for assembly (boot-missing)": 1})
        self.assertEqual(result["differing"], {"fb": ["return"], "fc": ["memory"], "fd": ["crash"]})
        self.assertEqual(result["not_in_port"], ["fe"])
        rows = [json.loads(line) for line in (self.root / "runs" / "findings.jsonl").read_text().splitlines()]
        by = {r["function"]: r for r in rows}
        self.assertEqual({r["kind"] for r in rows}, {"fn-differs"})
        self.assertEqual(
            (by["fb"]["field"], by["fb"]["port"], by["fb"]["oracle"]), ("return", "0x00000004", "0x00000003")
        )
        self.assertEqual(
            (by["fc"]["addr"], by["fc"]["port"], by["fc"]["oracle"]), ("0x80001000", "0x00000002", "0x00000001")
        )
        self.assertIn("SIGSEGV at fd+0x4", by["fd"]["port"])
        self.assertEqual(by["fb"]["movie"], "m")
        self.assertTrue(by["fb"]["evidence"].endswith("#c1"))
        self.assertEqual(by["fc"]["signature"], "fn-differs:fc:memory@0x80001000")
        self.assertGreater(result["timing"]["cpu_seconds"], 0)

    def test_the_recording_is_cached_and_a_rerun_replaces_findings(self):
        self.run_batch()
        self.run_batch()
        self.assertEqual((self.root / "oracle" / "calls.txt").read_text().split(), ["3"])  # one recording
        self.assertEqual(len((self.root / "runs" / "findings.jsonl").read_text().splitlines()), 3)
        self.run_batch(refresh=True, calls=2)
        self.assertEqual((self.root / "oracle" / "calls.txt").read_text().split(), ["3", "2"])

    def test_functions_limit_the_batch_and_findings_can_be_left_out(self):
        result, _, _ = self.run_batch(functions=["fb"], findings=False)
        self.assertEqual(result["functions"]["replayed"], 1)
        self.assertEqual(result["findings"], None)
        self.assertFalse((self.root / "runs" / "findings.jsonl").exists())

    def test_an_oracle_without_the_recorder_is_named(self):
        (self.root / "oracle" / "run").write_text("#!/bin/sh\nexit 0\n")
        with self.assertRaises(DecompError) as caught:
            self.run_batch()
        self.assertIn("replay.cpp", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
