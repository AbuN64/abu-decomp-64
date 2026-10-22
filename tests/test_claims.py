from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from abu_decomp import claims
from abu_decomp.behave import classify, compare
from abu_decomp.config import load_profile

STACK = (0x803F0000, 0x803F4010)


def make_profile(tmp_path):
    os.environ.pop("ABU_DECOMP_INVOCATIONS", None)
    seat = tmp_path / "seat.json"
    seat.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "t",
                "events": {"path": str(tmp_path / "events.jsonl")},
                "land": {
                    "check": ["make"],
                    "claims": {
                        "words": {"fixed": "pass", "behaves": "pass", "correct": "behaves", "artifact": "benign"},
                        "commands": ["behave", "make"],
                    },
                },
            }
        )
    )
    return load_profile(seat)


def ev(name, kind="call"):
    return {"k": kind, "name": name, "args": [0] * 8, "f": [0, 0]}


def store(address):
    return {"k": "st", "addr": address, "w": 4, "value": 1}


def case(events):
    return {"name": "c0", "status": "ok", "events": [ev("f"), *events], "net": [], "saved": [], "unseeded": []}


def classified(orig, cand, tmp_path):
    summary = {
        "first_difference": {
            "case": "c0",
            "inputs": {},
            "kind": "call",
            "index": 1,
            "expected": orig[0][0],
            "got": cand[0][0],
        }
    }

    def build(rows):
        events = []
        for name, address in rows:
            events.append(ev(name))
            if address:
                events.append(store(address))
        return case(events)

    src = tmp_path / "f.c"
    src.write_text("void f(void) {\n    b();\n    a();\n}\n")
    return classify.classify(
        summary, [build(orig)], [build(cand)], STACK, None, frozenset(), "f", src, Path("seat.json")
    )


class ClaimTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.profile = make_profile(self.tmp)

    def test_record_and_cite(self):
        claims.record(self.profile, "a" * 32, "behave", True, {"verdict": "same", "function": "f"}, ["behave"])
        claims.record(
            self.profile, "b" * 32, "behave", True, {"verdict": "differs", "classification": {"real": True}}, []
        )
        assert claims.check(self.profile, "refactor the loop")["problems"] == []
        assert claims.check(self.profile, "fixed func_80000000_de")["problems"][0]["word"] == "fixed"
        assert claims.check(self.profile, f"fixed it, run {'a' * 32}") == {"cited": ["a" * 32], "problems": []}
        bad = claims.check(self.profile, f"Fixed, see {'b' * 12}")
        assert bad["problems"] and "none of the cited runs" in bad["problems"][0]["why"]

    def test_benign_needs_a_benign_outcome(self):
        claims.record(
            self.profile, "c" * 32, "behave", True, {"verdict": "differs", "classification": {"real": False}}, []
        )
        assert claims.check(self.profile, f"a tool artifact, {'c' * 32}")["problems"] == []
        assert claims.check(self.profile, f"a tool artifact, {'d' * 32}")["problems"]

    def test_behaviour_claims_need_a_behave_run_and_point_at_behave(self):
        claims.record(self.profile, "1" * 32, "make", True, {}, ["make", "check"])
        problem = claims.check(self.profile, f"the body is correct, {'1' * 32}")["problems"][0]
        assert problem["word"] == "correct" and "behave --function SYM" in problem["next"]
        assert "make check" in claims.check(self.profile, "fixed it")["problems"][0]["next"]
        assert "behave --function SYM" in claims.check(self.profile, "an artifact")["problems"][0]["next"]
        claims.record(self.profile, "2" * 32, "behave", True, {"verdict": "same", "function": "f"}, ["behave"])
        assert claims.check(self.profile, f"the body is correct, {'2' * 32}")["problems"] == []

    def test_a_relative_events_path_keeps_records_beside_the_profile(self):
        seat = self.tmp / "seat" / "seat.json"
        seat.parent.mkdir()
        seat.write_text(json.dumps({"schema_version": 1, "name": "r", "events": {"path": "feed/events.jsonl"}}))
        assert claims.state_dir(load_profile(seat)) == (self.tmp / "seat" / "feed" / "invocations").resolve()

    def test_other_commands_are_not_evidence(self):
        claims.record(self.profile, "e" * 32, "tas", True, {}, [])
        assert claims.check(self.profile, f"fixed {'e' * 32}")["problems"]
        claims.record(self.profile, "f" * 32, "claims", True, {}, [])
        assert claims.lookup(self.profile, "f" * 12) is None

    def test_reordered_pure_calls_are_benign(self):
        out = classified([("a", 0), ("b", 0)], [("b", 0), ("a", 0)], self.tmp)
        assert out["kind"] == "call order" and out["real"] is False
        assert out["candidate_line"]["line"] == 2 and out["next"].startswith("decomp --profile seat.json behave")

    def test_reordered_call_that_writes_shared_memory_is_real(self):
        out = classified([("a", 0x80111D24), ("b", 0)], [("b", 0), ("a", 0x80111D24)], self.tmp)
        assert out["real"] is True and "0x80111D24" in out["why"]

    def test_a_call_count_change_and_stubs_are_real(self):
        out = classified([("a", 0), ("b", 0)], [("b", 0)], self.tmp)
        assert out["kind"] == "call count"
        stub = classify.writes([{"events": [ev("f"), ev("s", "stub")]}], "s", STACK, None)
        assert stub and "stub" in stub

    def test_other_kinds_are_real(self):
        summary = compare.overall(
            [
                {
                    "name": "c0",
                    "verdict": "differs",
                    "reason": "x",
                    "difference": {"kind": "store", "address": 0x80000010},
                }
            ],
            [{}],
            1,
        )
        out = classify.classify(summary, [case([])], [case([])], STACK, None, frozenset(), "f", None, Path("s.json"))
        assert out["real"] is True and out["kind"] == "store" and "0x80000010" in out["fix"]

    def test_land_refuses_an_uncited_claim_alone_and_appends_ids(self):
        from abu_decomp import land

        landing = land.Landing.__new__(land.Landing)
        landing.profile, landing.messages = self.profile, {"lane": "Fix func_80000000_de: fixed"}
        landing.config = {"claims": claims.settings(self.profile), "subject_limit": 72}
        landing._hook = None
        landing.git = lambda *a, **k: ""
        (problem,) = landing.message_problems("lane", landing.messages["lane"], None)
        assert problem[0] == "land.claim_uncited" and "behave --function func_80000000_de" in problem[1]
        assert land.next_command(
            self.profile, Path("T"), "main", {"key": "land.claim_uncited", "message": problem[1]}
        ).startswith("decomp --profile")
        claims.record(self.profile, "9" * 32, "behave", True, {"verdict": "same"}, [])
        message = f"Fix func_80000000_de: fixed ({'9' * 32})"
        landing.messages["lane"] = message
        assert landing.message_problems("lane", message, None) == []
        assert landing.input_message({"lane": "lane", "functions": None}, [])[1] == f"Evidence: {'9' * 32}"

    def test_ship_refuses_only_the_input_whose_function_failed_the_evidence_gate(self):
        from abu_decomp import ship

        shipper = ship.Ship.__new__(ship.Ship)
        result = {"lanes": [{"lane": "a", "files": ["x.c"]}, {"lane": "b", "files": ["y.c"]}]}
        bad = {"key": "land.evidence", "file": "y.c", "blocking": True, "message": "m"}
        assert [f["lane"] for f in shipper.evidence_refusals(result, [bad])] == ["b"]
        other = {"key": "land.check", "blocking": True, "message": "m"}
        assert shipper.evidence_refusals(result, [bad, other]) == []
