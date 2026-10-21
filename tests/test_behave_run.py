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
                    "behave": {"work": str(self.root / "work"), "learn_rounds": 1, "min_compared": 1},
                }
            )
        )
        self.probes = []
        self.delta = 0
        self.grab = None

    def fake_probe(self, profile, work, name, request, function):
        self.probes.append((name, request))
        self.targets = [*getattr(self, "targets", []), function]
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
            mock.patch.object(run, "capture_seed", self.grab or (lambda *a: (None, "absent", "config.schema"))),
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

    def seeded_probe(self, profile, work, name, request, function):
        """A function that only runs over the captured memory: unmapped without the seed, ok with it."""
        done = self.fake_probe(profile, work, name, request, function)
        if "seed" not in request:
            for row in done["cases"]:
                row.update(status="unmapped", detail="unmapped 00000000 4 last 0")
        return done

    def seed(self):
        return {
            "memory": [{"addr": "0x80000000", "file": "/ram.bin", "sha256": "ab" * 32}],
            "sp": "0x80123400",
            "case": {"name": "seed-f-hit1", "registers": {"r4": "0x80000100", "r5": "0x7"}},
            "capture": {
                "function": "f",
                "hit": 1,
                "rom_sha1": "c" * 40,
                "rdram_sha256": "d" * 64,
                "sp": "0x80123400",
                "rdram_path": "/ram.bin",
                "cached": False,
            },
        }

    def seeded_run(self, seed):
        stdout = io.StringIO()
        with (
            mock.patch.object(run, "probe", self.seeded_probe),
            mock.patch.object(
                run, "capture_seed", lambda *a: (seed, "", "") if seed else (None, "none", "config.schema")
            ),
            mock.patch.object(run.build_candidate, "symbol_table", lambda *a, **k: SYMBOLS),
            mock.patch.object(
                run.build_candidate,
                "build",
                lambda *a, **k: cand.Image(
                    self.root / "c.bin", 0x80800000, ({"name": "f", "offset": 0, "size": 0x40},), 0x40, "ab" * 32
                ),
            ),
            contextlib.redirect_stdout(stdout),
        ):
            main(
                [
                    "--profile",
                    str(self.seat),
                    "behave",
                    "--function",
                    "f",
                    "--candidate",
                    str(self.candidate),
                    "--cases",
                    "8",
                ]
            )
        return json.loads(stdout.getvalue())

    def test_strict_memory_that_completes_no_case_is_seeded_from_the_oracle_before_the_lenient_fallback(self):
        envelope = self.seeded_run(self.seed())
        result = envelope["result"]
        self.assertEqual(result["oracle_seed"]["function"], "f")
        self.assertEqual(result["oracle_seed"]["rom_sha1"], "c" * 40)
        self.assertNotIn("lenient", [f["key"] for f in envelope["findings"]])
        self.assertIn("behave.seed", [f["key"] for f in envelope["findings"]])
        final = [r for n, r in self.probes if n in ("original", "candidate")][-2:]
        for request in final:
            self.assertEqual(request["seed"]["memory"][0]["file"], "/ram.bin")
            self.assertEqual(request["sp"], "0x80123400")
            self.assertFalse(request.get("lenient"))
            entry = next(c for c in request["cases"] if c["name"] == "seed-f-hit1")  # the entry registers, one case
            self.assertEqual(entry["registers"]["r4"], "0x80000100")
            self.assertEqual(request["cases"][-1]["name"], "seed-f-hit1-n-plus1")  # and one around it (8 // 8)
        learn = [r for n, r in self.probes if n == "learn"]
        self.assertTrue(all(r["cases"][-1]["name"] != "seed-f-hit1" for r in learn))
        self.assertEqual(result["cases"], 10)
        self.assertEqual(result["verdict"], "same")

    def test_one_case_the_seed_cannot_finish_still_widens_the_memory_so_every_case_is_compared(self):
        def probe(profile, work, name, request, function):
            done = self.fake_probe(profile, work, name, request, function)
            if not request.get("lenient"):  # one generated case chases a garbage pointer outside RDRAM
                done["cases"][0].update(status="unmapped", detail="unmapped 0000B402 4 last 0")
            return done

        stdout = io.StringIO()
        with (
            mock.patch.object(run, "probe", probe),
            mock.patch.object(run, "capture_seed", lambda *a: (self.seed(), "", "")),
            mock.patch.object(run.build_candidate, "symbol_table", lambda *a, **k: SYMBOLS),
            mock.patch.object(
                run.build_candidate,
                "build",
                lambda *a, **k: cand.Image(
                    self.root / "c.bin", 0x80800000, ({"name": "f", "offset": 0, "size": 0x40},), 0x40, "ab" * 32
                ),
            ),
            contextlib.redirect_stdout(stdout),
        ):
            main(["--profile", str(self.seat), "behave", "--function", "f", "--candidate", str(self.candidate)])
        envelope = json.loads(stdout.getvalue())
        keys = [f["key"] for f in envelope["findings"]]
        self.assertIn("behave.seed", keys)
        self.assertIn("behave.lenient", keys)
        self.assertEqual(envelope["result"]["verdict"], "same")
        self.assertNotIn("0000B402", json.dumps(envelope["result"]["cases_detail"]))  # compared, not left unmapped
        final = [r for n, r in self.probes if n in ("original", "candidate")][-2:]
        for request in final:
            self.assertTrue(request["lenient"])
            self.assertEqual(request["seed"]["memory"][0]["file"], "/ram.bin")  # lenient over the captured memory

    def test_the_tools_are_asked_for_the_address_never_the_name_the_oracle_may_spell_otherwise(self):
        self.invoke("--candidate", str(self.candidate), "--cases", "4")
        self.assertTrue(self.targets)
        self.assertEqual(set(self.targets), {"0x80001000"})

    def test_without_a_capture_the_lenient_fallback_still_runs_and_says_why_there_is_no_seed(self):
        envelope = self.seeded_run(None)
        keys = [f["key"] for f in envelope["findings"]]
        self.assertIn("behave.seed", keys)
        self.assertIn("behave.lenient", keys)
        self.assertNotIn("oracle_seed", envelope["result"])
        self.assertTrue(any(r.get("lenient") for _, r in self.probes))

    def test_no_capture_flag_goes_straight_to_the_lenient_fallback(self):
        called = []
        stdout = io.StringIO()
        with (
            mock.patch.object(run, "probe", self.seeded_probe),
            mock.patch.object(run, "capture_seed", lambda *a: called.append(a) or (None, "", "")),
            mock.patch.object(run.build_candidate, "symbol_table", lambda *a, **k: SYMBOLS),
            contextlib.redirect_stdout(stdout),
        ):
            main(["--profile", str(self.seat), "behave", "--function", "f", "--cases", "6", "--no-capture"])
        self.assertEqual(called, [])
        self.assertIn("behave.lenient", [f["key"] for f in json.loads(stdout.getvalue())["findings"]])

    def test_a_probe_with_other_stubs_asks_for_its_own_capture(self):
        asked = []

        def probe(profile, work, name, request, function):
            done = self.seeded_probe(profile, work, name, request, function)
            done["record"]["stubs"] = {"g": "boundary"}
            return done

        def grab(*a):
            asked.append(a[-1])
            return {**self.seed(), "capture": {**self.seed()["capture"], "stubs": list(a[-1])}}, "", ""

        stdout = io.StringIO()
        with (
            mock.patch.object(run, "probe", probe),
            mock.patch.object(run, "capture_seed", grab),
            mock.patch.object(run.build_candidate, "symbol_table", lambda *a, **k: SYMBOLS),
            contextlib.redirect_stdout(stdout),
        ):
            main(["--profile", str(self.seat), "behave", "--function", "f", "--cases", "6"])
        self.assertEqual(asked, [["g"]])  # asked once with the stubs of the probe, not again for the same set

    def test_a_function_whose_strict_cases_all_ran_is_still_seeded_with_the_entry_and_cases_around_it(self):
        self.grab = lambda *a: (self.seed(), "", "")
        _, envelope = self.invoke("--candidate", str(self.candidate), "--cases", "16")
        self.assertEqual(envelope["result"]["oracle_seed"]["function"], "f")
        final = [r for n, r in self.probes if n in ("original", "candidate")][-2:]
        for request in final:
            self.assertEqual(request["seed"]["memory"][0]["file"], "/ram.bin")
            names = [c["name"] for c in request["cases"]]
            entry = names.index("seed-f-hit1")
            # n (a1 = r5 = 7) moved off its real value; a (a pointer) stays where the game aimed it
            self.assertEqual(names[entry + 1 :], ["seed-f-hit1-n-plus1", "seed-f-hit1-n-minus1"])
            self.assertEqual(request["cases"][entry + 1]["registers"]["r5"], "0x8")
            self.assertEqual(request["cases"][entry + 1]["registers"]["r4"], "0x80000100")
        self.assertNotIn("behave.lenient", [f["key"] for f in envelope["findings"]])

    def test_a_function_the_oracle_never_reaches_is_a_named_finding_not_a_silent_fallback(self):
        self.grab = lambda *a: (None, "f entry hit 1 was not reached within 120s", "repro.capture.unreached")
        _, envelope = self.invoke("--candidate", str(self.candidate), "--cases", "8")
        found = {f["key"]: f["message"] for f in envelope["findings"]}
        self.assertIn("behave.unreached", found)
        self.assertIn("not reached within 120s", found["behave.unreached"])
        self.assertNotIn("behave.seed", found)
        self.assertEqual(envelope["result"]["verdict"], "same")

    def test_a_stale_oracle_host_fails_the_command_instead_of_comparing_over_it(self):
        def stale(*a):
            raise run.BehaveError("behave.capture_setup", "repro.capture.stale_host: inputs changed")

        self.grab = stale
        code, envelope = self.invoke("--candidate", str(self.candidate), "--cases", "8")
        self.assertEqual((code, envelope["findings"][0]["key"]), (1, "behave.capture_setup"))

    def test_a_setup_finding_from_taste_is_raised_and_an_unreached_one_is_returned(self):
        class Called:
            def __init__(self, key):
                self.ok, self.returncode = False, 1
                self.document = {"findings": [{"key": key, "message": "why"}]}

        profile = mock.Mock()
        with (
            mock.patch.object(run, "invoke", lambda *a, **k: Called("repro.capture.stale_host")),
            self.assertRaises(run.BehaveError) as raised,
        ):
            run.capture_seed(profile, "v1", "0x80001000", 1, [])
        self.assertEqual(raised.exception.key, "behave.capture_setup")
        with mock.patch.object(run, "invoke", lambda *a, **k: Called("repro.capture.unreached")):
            self.assertEqual(
                run.capture_seed(profile, "v1", "0x80001000", 1, []), (None, "why", "repro.capture.unreached")
            )

    def test_each_process_writes_its_own_request_file_so_parallel_runs_never_swap_requests(self):
        class Called:
            ok, returncode = True, 0

            def __init__(self, path):
                self.document = {"result": {"probe": {"cases_path": str(path)}}}

        seen = []
        out = self.root / "cases.json"
        out.write_text("[]")

        def fake_invoke(tool, profile, argv):
            seen.append(argv[argv.index("--input") + 1])
            return Called(out)

        with mock.patch.object(run, "invoke", fake_invoke), mock.patch.object(run.os, "getpid", lambda: 4242):
            run.probe(mock.Mock(), self.root, "original", {"story": {"question": "q"}}, "0x80001000")
        self.assertEqual(Path(seen[0]).name, "original-4242.json")

    def test_a_candidate_run_holds_its_build_dir_lock_and_its_own_retries_reenter_it(self):
        import fcntl

        held = []

        def probe(profile, work, name, request, function):
            lock = next((self.root / "work" / "behave").glob("cand-*.lock"))
            with lock.open("a+b") as other:
                try:
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held.append(False)
                except BlockingIOError:
                    held.append(True)
            return self.seeded_probe(profile, work, name, request, function)

        self.grab = lambda *a: (self.seed(), "", "")  # the seeded retry runs run() again inside the first
        with mock.patch.object(self, "fake_probe", self.fake_probe), mock.patch.object(run, "probe", probe):
            stdout = io.StringIO()
            with (
                mock.patch.object(run, "capture_seed", self.grab),
                mock.patch.object(run.build_candidate, "symbol_table", lambda *a, **k: SYMBOLS),
                mock.patch.object(
                    run.build_candidate,
                    "build",
                    lambda *a, **k: cand.Image(
                        self.root / "c.bin", 0x80800000, ({"name": "f", "offset": 0, "size": 0x40},), 0x40, "ab" * 32
                    ),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                main(["--profile", str(self.seat), "behave", "--function", "f", "--candidate", str(self.candidate)])
        self.assertTrue(held and all(held))  # another process could not take it at any probe
        self.assertEqual(run._HELD, {})  # released at the end

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
