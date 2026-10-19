import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp import tools
from abu_decomp.behave import candidate as cand
from abu_decomp.behave import run
from abu_decomp.cli import describe, main

ROOT_VRAM = 0x80001000
SYMBOLS = {
    "f": [ROOT_VRAM, 0, 0x40, "func"],
    "g": [0x80002000, 0, 0x20, "func"],
    "D_state": [0x80100000, None, 4, "data"],
    "h": [0x80003000, 0, 0x20, "func"],
}


def fake_cases(request, delta):
    """What the harness would record for a function that stores a1 into the first word of its first argument."""
    rows = []
    for case in request["cases"]:
        regs = case["registers"]
        region = case["memory"][0]["addr"] if case.get("memory") else 0
        a0, a1 = regs.get("a0", 0), regs.get("a1", 0)
        ok = a0 != 0
        row = {
            "name": case["name"],
            "status": "ok" if ok else "unmapped",
            "detail": "" if ok else "unmapped 00000000 4 last 0",
            "events": [
                {"k": "call", "name": "f", "args": [a0, a1, 0, 0, 0, 0, 0, 0], "f": [0, 0]},
                {"k": "call", "name": "g", "args": [a1, 0, 0, 0, 0, 0, 0, 0], "f": [0, 0]},
                {"k": "st", "addr": region + 4, "w": 4, "value": a1},
            ],
            "reads": [[region + 8, 4]] if ok else [],
            "unseeded": [],
            "net": [[region + 4, (a1 + delta) & 0xFFFFFFFF]],
            "ret": {"v0": (a1 + 1) & 0xFFFFFFFF, "v1": 0, "f0": 0, "f2": 0},
            "saved": [],
            "inputs": {k: case[k] for k in ("registers", "stack", "memory") if k in case},
        }
        rows.append(row)
    return rows


class RunTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        project = self.root / "project"
        (project / "include").mkdir(parents=True)
        (project / "config.toml").write_text('names_from = "v1"\n')
        (project / "include" / "f.h").write_text(
            "extern s32 f(Actor *a, s32 n);\nextern void g(s32 x);\nextern void h(void);\n"
        )
        (self.root / "host.toml").write_text("")
        self.candidate = self.root / "f.c"
        self.candidate.write_text("s32 f(Actor *a, s32 n) { a->field = n; g(n); return n + 1; }\n")
        self.seat = self.root / "seat.json"
        self.seat.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "t",
                    "tools": {"project": str(project), "host": str(self.root / "host.toml"), "timeout_seconds": 5},
                    "events": {"path": str(self.root / "events.jsonl")},
                    "behave": {"work": str(self.root / "work"), "learn_rounds": 1},
                }
            )
        )
        self.probes = []
        self.delta = 0

    def fake_probe(self, profile, work, name, request, function):
        self.probes.append((name, request))
        delta = self.delta if request.get("candidate") else 0
        record = {
            "sp": 0x80400000,
            "stack_window": 0x4000,
            "closure": ["f", "g"],
            "stubs": {},
            "statuses": {"ok": 1},
            "wall_seconds": 0.1,
            "cached": name == "original",
            "lock_wait_seconds": 0.25,
        }
        return {"record": record, "cases": fake_cases(request, delta)}

    def invoke(self, *args, build=None):
        image = cand.Image(
            self.root / "c.bin", 0x80800000, ({"name": "f", "offset": 0, "size": 0x40},), 0x40, "ab" * 32
        )
        stdout = io.StringIO()
        with (
            mock.patch.object(run, "probe", self.fake_probe),
            mock.patch.object(run.build_candidate, "symbol_table", lambda *a, **k: SYMBOLS),
            mock.patch.object(run.build_candidate, "build", build or (lambda *a, **k: image)),
            contextlib.redirect_stdout(stdout),
        ):
            code = main(["--profile", str(self.seat), "behave", "--function", "f", *args])
        return code, json.loads(stdout.getvalue())

    def test_describe_without_a_candidate_reports_the_fields_and_the_prototype(self):
        code, envelope = self.invoke("--cases", "6")
        self.assertEqual((code, envelope["ok"]), (0, True))
        result = envelope["result"]
        self.assertEqual((result["mode"], result["verdict"]), ("describe", "described"))
        self.assertEqual(result["prototype"]["params"][0]["type"], "Actor *")
        fields = result["arguments"][0]["fields"]
        self.assertIn("0x8", [f["offset"] for f in fields])
        self.assertTrue(set(envelope) >= {"ok", "result", "findings", "stages", "invocation"})

    def test_a_candidate_that_behaves_the_same_is_same_and_the_original_is_probed_once_per_stage(self):
        code, envelope = self.invoke("--candidate", str(self.candidate), "--cases", "8")
        result = envelope["result"]
        self.assertEqual((code, result["mode"], result["verdict"]), (0, "compare", "same"))
        self.assertEqual(result["counts"]["differs"], 0)
        self.assertGreater(result["counts"]["untestable"], 0)  # the null-pointer case cannot run
        self.assertEqual([n for n, _ in self.probes], ["learn", "original", "candidate"])
        self.assertIn("candidate", self.probes[-1][1])
        self.assertEqual(self.probes[-1][1]["cases"], self.probes[1][1]["cases"])  # one input set for both

    def test_the_time_spent_queued_behind_another_isolated_repro_is_in_the_timing(self):
        _, envelope = self.invoke("--candidate", str(self.candidate), "--cases", "8")
        self.assertEqual([n for n, _ in self.probes], ["learn", "original", "candidate"])
        self.assertEqual(envelope["result"]["timing"]["lock_wait"], 0.75)

    def test_unbake_cache_hits_and_misses_ride_the_timing(self):
        image = cand.Image(
            self.root / "c.bin", 0x80800000, ({"name": "f", "offset": 0, "size": 0x40},), 0x40, "ab" * 32
        )

        def build(*a, **k):
            tools.CACHE_LOG.append({"call": "api build_context", "report": {"api build_context": {"build": [0, 3]}}})
            tools.CACHE_LOG.append({"call": "unbake symbols", "report": {"symbols": {"view": [2, 0]}}})
            return image

        _, envelope = self.invoke("--candidate", str(self.candidate), "--cases", "8", build=build)
        result = envelope["result"]
        self.assertEqual((result["timing"]["cache_hits"], result["timing"]["cache_misses"]), (2, 3))
        self.assertEqual([c["call"] for c in result["cache"]["calls"]], ["api build_context", "unbake symbols"])
        self.assertEqual(result["cache"]["unexposed"], [])
        self.assertEqual(tools.CACHE_LOG, [])  # drained: the next command starts from zero

    def test_a_function_without_arguments_is_still_probed_for_what_it_reads(self):
        stdout = io.StringIO()
        with (
            mock.patch.object(run, "probe", self.fake_probe),
            mock.patch.object(run.build_candidate, "symbol_table", lambda *a, **k: SYMBOLS),
            contextlib.redirect_stdout(stdout),
        ):
            main(["--profile", str(self.seat), "behave", "--function", "h"])
        self.assertEqual(self.probes[0][0], "learn")

    def test_a_stub_whose_return_value_the_original_dereferenced_answers_with_a_region(self):
        def probe(profile, work, name, request, function):
            self.probes.append((name, request))
            rows = []
            for case in request["cases"]:
                tag = case.get("stub_v0", {}).get("get", 0)
                faults = tag >= 0x60000000 and tag < 0x80000000
                rows.append(
                    {
                        "name": case["name"],
                        "status": "unmapped" if faults else "ok",
                        "detail": f"unmapped {tag + 0x10:08X} 4 last 0" if faults else "",
                        "events": [],
                        "reads": [],
                        "unseeded": [],
                        "net": [],
                        "ret": {"v0": 0, "v1": 0, "f0": 0, "f2": 0},
                        "saved": [],
                        "inputs": {k: case[k] for k in ("registers", "stack", "memory") if k in case},
                    }
                )
            record = {
                "sp": 0x80400000,
                "stack_window": 0x4000,
                "stubs": {"get": "boundary", "osX": "no generated body"},
            }
            return {"record": record, "cases": rows}

        seat = json.loads(self.seat.read_text())
        seat["behave"]["learn_rounds"] = 4
        self.seat.write_text(json.dumps(seat))
        stdout = io.StringIO()
        with (
            mock.patch.object(run, "probe", probe),
            mock.patch.object(run.build_candidate, "symbol_table", lambda *a, **k: SYMBOLS),
            contextlib.redirect_stdout(stdout),
        ):
            main(["--profile", str(self.seat), "behave", "--function", "h", "--cases", "6"])
        names = [n for n, _ in self.probes]
        self.assertEqual(names[-1], "original")
        self.assertGreaterEqual(names.count("learn"), 2)  # the stub is named in the first round, tagged in the second
        asked = self.probes[-1][1]["cases"]
        self.assertEqual({c["stub_v0"]["get"] for c in asked} - {0}, {0x80A00000 + 0x1000 * 8})  # one region of its own
        self.assertNotIn("osX", asked[0]["stub_v0"])  # stubs with no body are not tagged

    def test_a_candidate_that_stores_a_different_value_differs_with_the_first_difference_named(self):
        self.delta = 1
        _, envelope = self.invoke("--candidate", str(self.candidate), "--cases", "8")
        result = envelope["result"]
        self.assertEqual(result["verdict"], "differs")
        self.assertEqual(result["first_difference"]["kind"], "store")
        event = json.loads((self.root / "events.jsonl").read_text().splitlines()[-1])
        self.assertEqual(
            (event["kind"], event["payload"]["op"], event["payload"]["verdict"]), ("PROBE", "behave", "differs")
        )
        self.assertEqual(event["payload"]["function"], "f")
        self.assertIn("store 0x", result["reason"])
        self.assertIn("n=", result["reason"])  # the case inputs are in the reason
        self.assertLessEqual(len(result["cases_detail"]), 12)

    def test_a_candidate_that_does_not_build_is_untestable_with_the_reason(self):
        def broken(*args, **kwargs):
            raise cand.CandidateError("behave.compile", "the candidate does not compile: f.c:1 syntax error")

        code, envelope = self.invoke("--candidate", str(self.candidate), build=broken)
        self.assertEqual((code, envelope["result"]["verdict"]), (0, "untestable"))
        self.assertIn("syntax error", envelope["result"]["reason"])
        self.assertEqual(envelope["findings"][0]["key"], "behave.compile")
        self.assertEqual(set(envelope["result"]["cache"]), {"hits", "misses", "calls", "unexposed"})  # refused too

    def test_the_same_seed_gives_the_same_inputs_and_another_seed_other_memory(self):
        self.invoke("--cases", "6", "--seed", "3")
        first = self.probes[-1][1]["cases"]
        self.invoke("--cases", "6", "--seed", "3")
        self.assertEqual(self.probes[-1][1]["cases"], first)
        self.invoke("--cases", "6", "--seed", "4")
        self.assertNotEqual(self.probes[-1][1]["cases"], first)

    def test_cases_are_capped(self):
        _, envelope = self.invoke("--cases", "3")
        self.assertEqual(envelope["result"]["cases"], 3)

    def test_an_unknown_function_is_a_named_refusal(self):
        stdout = io.StringIO()
        with (
            mock.patch.object(run.build_candidate, "symbol_table", lambda *a, **k: {}),
            contextlib.redirect_stdout(stdout),
        ):
            code = main(["--profile", str(self.seat), "behave", "--function", "nope"])
        envelope = json.loads(stdout.getvalue())
        self.assertEqual((code, envelope["findings"][0]["key"]), (1, "behave.function"))

    def test_the_command_is_listed_with_its_arguments(self):
        row = next(c for c in describe()["commands"] if c["name"] == "behave")
        flags = {f for a in row["arguments"] for f in a["flags"]}
        self.assertTrue({"--function", "--candidate", "--version", "--cases", "--seed", "--project", "--host"} <= flags)


if __name__ == "__main__":
    unittest.main()
